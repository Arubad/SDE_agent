import json
from pathlib import Path

import httpx
import pytest

from backend.tutor.ingest.fetch import USER_AGENT, Fetcher, FetchResult, HttpCacheStore
from backend.tutor.ingest.net import (
    RealYouTube,
    CaptionsBlocked,
    Cue,
    DataApiYouTube,
    NoCaptions,
    SearchUnavailable,
    SearxngSearch,
    fixture_net,
    make_net,
)
from backend.tutor.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "net"
LONG = "<p>" + "Kubernetes explained in plain words. " * 12 + "</p>"


class FakeTime:
    def __init__(self):
        self.t = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_fetcher(handler, *, interval=1.0, cache=None, time=None):
    time = time or FakeTime()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(client, domain_interval_s=interval, user_agent=USER_AGENT, cache=cache,
                   clock=time.clock, sleep=time.sleep), time


def html(body: str, status=200, headers=None):
    return httpx.Response(status, text=f"<html><body>{body}</body></html>",
                          headers={"content-type": "text/html; charset=utf-8", **(headers or {})})


def test_user_agent_is_pinned():
    assert USER_AGENT == "RoadmapTutor/0.2 (+local; personal study tool)"


def test_robots_denied():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        assert req.headers["user-agent"] == USER_AGENT
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return html(LONG)

    f, _ = make_fetcher(handler)
    denied = f.get("https://a.test/private/x")
    assert denied.outcome == "robots_denied" and denied.reason
    assert "https://a.test/private/x" not in seen  # never requested
    assert f.get("https://a.test/public").outcome == "ok"
    assert seen.count("https://a.test/robots.txt") == 1  # fetched once per host


def test_missing_robots_allows_everything():
    f, _ = make_fetcher(lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else html(LONG))
    r = f.get("https://a.test/x")
    assert r.outcome == "ok" and r.status == 200 and r.content_type == "text/html"


def test_domain_interval_is_respected():
    f, t = make_fetcher(lambda req: httpx.Response(404) if req.url.path == "/robots.txt" else html(LONG))
    f.get("https://a.test/1")  # robots + page
    f.get("https://a.test/2")
    assert t.sleeps and all(s == pytest.approx(1.0) for s in t.sleeps)
    assert len(t.sleeps) == 2  # robots->page, page->page: 3 requests, first one free
    before = len(t.sleeps)
    f.get("https://b.test/1")  # a different host: its own first requests do not wait on a.test
    assert len(t.sleeps) == before + 1  # only b's robots->page gap


def test_zero_interval_never_sleeps():
    f, t = make_fetcher(lambda req: html(LONG), interval=0)
    f.get("https://a.test/1")
    f.get("https://a.test/2")
    assert t.sleeps == []


def test_too_large_is_cut():
    big = b"x" * (6 * 1024 * 1024)

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=big, headers={"content-type": "text/html"})

    f, _ = make_fetcher(handler)
    r = f.get("https://a.test/big")
    assert r.outcome == "too_large" and len(r.body) == 5 * 1024 * 1024


def test_follows_redirects_and_caps_them():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html(LONG)

    f, _ = make_fetcher(handler, interval=0)
    r = f.get("https://a.test/old")
    assert r.outcome == "ok" and r.url == "https://a.test/new"
    loop = f.get("https://a.test/loop")
    assert loop.outcome == "error" and "redirect" in loop.reason


@pytest.mark.parametrize(
    "status, body, headers, outcome",
    [
        (200, '<form action="/login"><input type="password" name="p"><input name="u"></form>', {}, "login_wall"),
        (401, "<p>Unauthorized</p>", {}, "login_wall"),
        (402, "<p>Payment required</p>", {}, "paywall"),
        (200, f'<div class="paywall-overlay">Subscribe to continue reading</div>{LONG}', {}, "paywall"),
        (403, "<title>Just a moment...</title><div id='cf-chl-widget'></div>", {}, "bot_protection"),
        (429, '<div class="g-recaptcha"></div>', {}, "bot_protection"),
        (503, "<p>Checking your browser</p>", {"cf-mitigated": "challenge"}, "bot_protection"),
        (200, '<div id="root"></div><script src="/app.js"></script>', {}, "needs_browser"),
        (200, "<noscript>Enable JavaScript to run this app.</noscript>", {}, "needs_browser"),
        (404, "<p>nope</p>", {}, "not_found"),
        (500, "<p>boom</p>", {}, "error"),
        (200, LONG, {}, "ok"),
    ],
)
def test_classifies_login_paywall_bot_needs_browser(status, body, headers, outcome):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return html(body, status, headers)

    f, _ = make_fetcher(handler, interval=0)
    r = f.get("https://a.test/page")
    assert r.outcome == outcome, r
    assert (r.reason is None) == (outcome == "ok")


def test_unsupported_type_and_non_http_scheme():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    f, _ = make_fetcher(handler, interval=0)
    assert f.get("https://a.test/i.png").outcome == "unsupported_type"
    assert f.get("ftp://a.test/x").outcome == "error"


def test_network_error_is_an_error_outcome():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("down")

    f, _ = make_fetcher(handler, interval=0)
    r = f.get("https://a.test/x")
    assert r.outcome == "error" and "down" in r.reason


def test_conditional_get_uses_cache(engine):
    store = HttpCacheStore(engine)
    url = "https://cache.test/doc"
    assert store.get(url) is None
    store.put(FetchResult(url, 200, "text/html", b"<html>cached body</html>", "ok", None),
              {"ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
    row = store.get(url)
    assert row.etag == '"v1"' and row.body == b"<html>cached body</html>"

    seen = {}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        seen["inm"] = req.headers.get("if-none-match")
        seen["ims"] = req.headers.get("if-modified-since")
        return httpx.Response(304)

    f, _ = make_fetcher(handler, interval=0, cache=store)
    r = f.get(url)
    assert seen == {"inm": '"v1"', "ims": "Wed, 01 Jan 2025 00:00:00 GMT"}
    assert r.outcome == "ok" and r.body == b"<html>cached body</html>" and r.content_type == "text/html"


def test_fresh_ok_response_with_validator_is_cached(engine):
    store = HttpCacheStore(engine)
    url = "https://cache.test/fresh"

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return html(LONG, headers={"etag": '"abc"'})

    f, _ = make_fetcher(handler, interval=0, cache=store)
    f.get(url)
    assert store.get(url).etag == '"abc"'


def test_fixture_net_serves_pages_and_404s():
    net = fixture_net(FIXTURES)
    ok = net.fetcher.get("https://example.test/kubernetes-intro")
    assert ok.outcome == "ok" and b"Kubernetes" in ok.body
    assert net.fetcher.get("https://example.test/nope").outcome == "not_found"
    assert net.fetcher.get("https://example.test/login").outcome == "login_wall"

    hits = net.search.search("kubernetes tutorial")
    assert hits and hits[0].url.startswith("https://") and hits[0].engine
    assert net.search.search("zzz nothing", category="videos") == []

    meta = net.youtube.video_meta("kube000001a")
    assert meta.title and meta.duration_s > 0 and meta.chapters[0].start_s == 0
    assert net.youtube.video_meta("missing00000") is None
    assert net.youtube.captions("kube000001a")[0].text
    assert net.youtube.top_comments("kube000001a", limit=1) == ["Great intro to pods."]
    assert net.youtube.playlist_items("PLkube0001", 2) == ["kube000001a", "kube000002b"]
    assert net.youtube.playlist_items("unknown", 5) == []


def test_fixture_net_caption_errors():
    net = fixture_net(FIXTURES)
    with pytest.raises(CaptionsBlocked):
        net.youtube.captions("blocked0001")
    with pytest.raises(NoCaptions):
        net.youtube.captions("nocaps00001")
    with pytest.raises(NoCaptions):
        net.youtube.captions("missing00000")


def test_searxng_down_raises_search_unavailable():
    def down(req):
        raise httpx.ConnectError("refused")

    s = SearxngSearch(httpx.Client(transport=httpx.MockTransport(down)), "http://127.0.0.1:8888")
    with pytest.raises(SearchUnavailable):
        s.search("kubernetes")


def test_searxng_parses_results_and_sends_category():
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"results": [
            {"title": "T", "url": "https://x.test/", "content": "snip", "engine": "duckduckgo"}]})

    s = SearxngSearch(httpx.Client(transport=httpx.MockTransport(handler)), "http://s.test/")
    out = s.search("k8s", category="videos")
    assert seen == {"q": "k8s", "format": "json", "categories": "videos"}
    assert (out[0].title, out[0].url, out[0].snippet, out[0].engine) == ("T", "https://x.test/", "snip", "duckduckgo")


def test_data_api_video_meta_comments_and_playlist():
    def handler(req):
        p = req.url.path
        if p.endswith("/videos"):
            return httpx.Response(200, json={"items": [{
                "id": "abcdefghijk",
                "snippet": {"title": "Pods", "channelTitle": "Chan",
                            "description": "0:00 Intro\n1:30 Pods\n4:05 Services\nthanks"},
                "contentDetails": {"duration": "PT1H2M3S"}}]})
        if p.endswith("/commentThreads"):
            return httpx.Response(200, json={"items": [{"snippet": {"topLevelComment": {
                "snippet": {"textOriginal": "nice"}}}}]})
        if p.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [
                {"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})
        return httpx.Response(404)

    yt = DataApiYouTube(httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")
    meta = yt.video_meta("abcdefghijk")
    assert (meta.title, meta.channel, meta.duration_s) == ("Pods", "Chan", 3723)
    assert meta.chapters == []  # the API gives no structured chapters; timestamp parsing is not done here
    assert yt.top_comments("abcdefghijk") == ["nice"]
    assert yt.playlist_items("PLx", 1) == ["v1"]


def test_make_net_selects_adapters(tmp_path):
    fx = make_net(Settings(net_fixtures_dir=FIXTURES), None)
    assert fx.search is not None and fx.fetcher.get("https://example.test/nope").outcome == "not_found"
    off = make_net(Settings(searxng_url=None), None)
    assert off.search is None and off.youtube is not None
    on = make_net(Settings(youtube_api_key="k"), None)
    assert isinstance(on.search, SearxngSearch)


def test_fixture_files_are_valid_json():
    for p in FIXTURES.rglob("*.json"):
        json.loads(p.read_text())


@pytest.mark.parametrize("url", ["http://[abc", "https://a.test:notaport/x", "http://", "https:///x", "ht!tp://a.test"])
def test_malformed_urls_are_an_error_outcome_not_an_exception(url):
    f, _ = make_fetcher(lambda req: html(LONG), interval=0)
    r = f.get(url)
    assert r.outcome == "error" and r.reason


def test_malformed_redirect_location_is_an_error_outcome():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://[abc"})

    f, _ = make_fetcher(handler, interval=0)
    r = f.get("https://a.test/x")
    assert r.outcome == "error" and r.reason


class _FakeApi:
    """Stands in for youtube_transcript_api.YouTubeTranscriptApi (new-style, instance fetch)."""

    exc: Exception | None = None

    def fetch(self, video_id):
        if self.exc:
            raise self.exc
        return [type("S", (), {"start": 1.0, "duration": 2.0, "text": "hi\nthere"})()]


@pytest.fixture
def yt_api(monkeypatch):
    import youtube_transcript_api as mod

    api = type("Api", (_FakeApi,), {"exc": None})
    monkeypatch.setattr(mod, "YouTubeTranscriptApi", api)
    return api


def _errors():
    from youtube_transcript_api import (
        CouldNotRetrieveTranscript, IpBlocked, NoTranscriptFound, RequestBlocked, TranscriptsDisabled,
        VideoUnavailable,
    )
    return IpBlocked, RequestBlocked, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, CouldNotRetrieveTranscript


def test_captions_success_maps_cues(yt_api):
    cues = RealYouTube(httpx.Client()).captions("abcdefghijk")
    assert [(c.start, c.dur, c.text) for c in cues] == [(1.0, 2.0, "hi there")]


@pytest.mark.parametrize(
    "make, expected",
    [
        (lambda: _errors()[0]("v"), CaptionsBlocked),
        (lambda: _errors()[1]("v"), CaptionsBlocked),
        (lambda: _errors()[2]("v"), NoCaptions),
        (lambda: _errors()[3]("v", ["en"], None), NoCaptions),
        (lambda: _errors()[4]("v"), NoCaptions),
        (lambda: RuntimeError("HTTP 429 Too Many Requests"), CaptionsBlocked),
        (lambda: RuntimeError("too many requests"), CaptionsBlocked),
        (lambda: httpx.ConnectError("connection refused"), NoCaptions),
        (lambda: KeyError("x"), NoCaptions),
    ],
)
def test_captions_errors_map_to_the_two_promised_exceptions(yt_api, make, expected):
    yt_api.exc = make()
    with pytest.raises(expected) as info:
        RealYouTube(httpx.Client()).captions("abcdefghijk")
    assert type(info.value) is expected
    assert str(info.value)


def test_captions_old_style_get_transcript(monkeypatch):
    import youtube_transcript_api as mod

    class Old:
        @staticmethod
        def get_transcript(video_id):
            return [{"start": 0.5, "duration": 1.5, "text": "old"}]

    monkeypatch.setattr(mod, "YouTubeTranscriptApi", Old)
    assert RealYouTube(httpx.Client()).captions("abcdefghijk") == [Cue(0.5, 1.5, "old")]


def test_extra_headers_robots_opt_out_and_auth_dropped_cross_host():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        if req.url.host == "api.example.test" and req.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://other.example.test/x"})
        return httpx.Response(200, json={"ok": True})

    f, _ = make_fetcher(handler, interval=0)
    assert f.get("https://api.example.test/a").outcome == "robots_denied"
    r = f.get("https://api.example.test/a", headers={"Authorization": "Bearer t"}, robots=False)
    assert r.outcome == "ok" and ("https://api.example.test/a", "Bearer t") in seen
    f.get("https://api.example.test/moved", headers={"Authorization": "Bearer t"}, robots=False)
    assert ("https://other.example.test/x", None) in seen
