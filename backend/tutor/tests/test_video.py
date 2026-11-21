from pathlib import Path

import pytest

from backend.core.llm import LLMUnavailable
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.net import (CaptionsBlocked, Chapter, Cue, NoCaptions, VideoMeta,
                                      fixture_net)
from backend.tutor.ingest.segment import choose_segments, parse_timestamps, topic_shift_split
from backend.tutor.ingest.video import CircuitBreaker, Throttle, TranscriptRouter, video_document
from backend.tutor.settings import Settings

FIX = Path(__file__).parent / "fixtures" / "net"


@pytest.fixture
def yt():
    return fixture_net(FIX).youtube


def _router(yt, routes=("captions",), gemini_fn=None, ytdlp_fn=None, sleeps=None, clock=None):
    sleeps = sleeps if sleeps is not None else []
    now = [0.0]
    return TranscriptRouter(
        Settings(transcript_routes=routes, ytdlp_cookies_browser="firefox"), yt,
        gemini_fn=gemini_fn or (lambda *a, **k: pytest.fail("gemini called")),
        ytdlp_fn=ytdlp_fn or (lambda *a, **k: pytest.fail("ytdlp called")),
        breaker=CircuitBreaker(1800, clock=lambda: now[0]), throttle=Throttle(3.0, clock=lambda: now[0],
                                                                              sleep=sleeps.append),
        sleep=sleeps.append)


def _meta(yt, vid):
    return yt.video_meta(vid)


def test_parse_timestamps_valid_and_invalid():
    assert parse_timestamps("0:00 Intro\n1:00 Two", 100) == []
    assert parse_timestamps("0:00 A\n2:00 B\n1:00 C", 1000) == []
    assert parse_timestamps("1:00 A\n2:00 B\n3:00 C", 1000) == []  # first not at 0:00
    assert parse_timestamps("0:00 A\n1:00 B\n9:00 C", 200) == []  # beyond duration
    text = "intro\n0:00 Intro\n(12:30) Services\n1:02:03 - Helm\nbye"
    assert parse_timestamps(text, 4000) == [Chapter(0, "Intro"), Chapter(750, "Services"), Chapter(3723, "Helm")]
    assert parse_timestamps("0:00 A\n1:00 B\n2:00 C", None)[2].start_s == 120


def test_route_order_creator_comments_split_whole(yt):
    emb = HashEmbedder()

    def run(vid):
        meta = _meta(yt, vid)
        cues = yt.captions(vid) if vid != "nocaps00001" else []
        return choose_segments(meta, yt.top_comments(vid), cues, emb)

    route, ch = run("kube000001a")
    assert route == "creator" and [c.title for c in ch] == ["Intro", "Pods", "Services"]
    route, ch = run("comments0001")
    assert route == "comments" and [c.start_s for c in ch] == [0, 100, 200]
    route, ch = run("split0000001")
    assert route == "transcript_split" and len(ch) >= 2
    route, ch = run("kube000002b")
    assert route == "whole" and ch == [Chapter(0, "Kubernetes Deployments explained")]
    assert run("nocaps00001")[0] == "whole"


def test_creator_route_from_description_when_no_structured_chapters():
    meta = VideoMeta("v", "T", "c", "0:00 A\n1:00 B\n2:00 C", 300.0)
    assert choose_segments(meta, [], [], HashEmbedder())[0] == "creator"


def test_topic_shift_split_finds_planted_boundary(yt):
    cues = yt.captions("split0000001")
    chapters = topic_shift_split(cues, HashEmbedder())
    assert chapters[0].start_s == 0
    assert any(abs(c.start_s - 300) <= 60 for c in chapters[1:])
    assert all(len(c.title) <= 60 for c in chapters)
    assert len(chapters) <= 30


def test_topic_shift_split_respects_min_segment_and_empty():
    assert topic_shift_split([], HashEmbedder()) == []
    cues = [Cue(float(i * 10), 10.0, "alpha beta gamma." if i < 6 else "zebra quartz mango.") for i in range(12)]
    ch = topic_shift_split(cues, HashEmbedder(), window_s=10, min_seg_s=120)
    assert all(b.start_s - a.start_s >= 120 for a, b in zip(ch, ch[1:]))


def test_hash_embedder_deterministic_normalized():
    e = HashEmbedder()
    v1, v2 = e.embed(["hello world hello"]), e.embed(["hello world hello"])
    assert v1 == v2 and len(v1[0]) == 64
    assert abs(sum(x * x for x in v1[0]) - 1.0) < 1e-9
    assert e.embed([""])[0] == [0.0] * 64


def test_captions_route_returns_cues(yt):
    route, cues, reason = _router(yt).get("kube000001a", _meta(yt, "kube000001a"))
    assert (route, reason) == ("captions", None) and len(cues) == 3


def test_caption_block_trips_breaker(yt):
    r = _router(yt)
    assert r.get("blocked0001", _meta(yt, "blocked0001")) == (None, [], "captions_blocked")
    calls = []
    orig = yt.captions
    yt.captions = lambda v: calls.append(v) or orig(v)
    assert r.get("kube000001a", _meta(yt, "kube000001a")) == (None, [], "captions_blocked")
    assert calls == []


def test_breaker_blocked_falls_to_gemini_and_expires(yt):
    seen = []

    def gem(contents, **kw):
        seen.append(contents)
        return "[00:00] hello there\n[01:05] second part\nnoise line"

    r = _router(yt, routes=("captions", "gemini"), gemini_fn=gem)
    route, cues, reason = r.get("blocked0001", _meta(yt, "blocked0001"))
    assert route == "gemini" and reason is None
    assert [(c.start, c.text) for c in cues] == [(0.0, "hello there"), (65.0, "second part")]
    assert cues[0].dur == 65.0
    assert "blocked0001" in str(seen[0])


def test_breaker_resets_after_window():
    now = [0.0]
    b = CircuitBreaker(1800, clock=lambda: now[0])
    b.trip()
    assert b.is_open()
    now[0] = 1801
    assert not b.is_open()
    b.trip()
    b.reset()
    assert not b.is_open()


def test_throttle_enforces_gap():
    now, sleeps = [0.0], []
    t = Throttle(3.0, clock=lambda: now[0], sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    t.wait()
    now[0] += 1.0
    t.wait()
    assert sleeps == [2.0]


def test_429_retries_twice_then_trips(yt):
    class Flaky:
        n = 0

        def captions(self, vid):
            Flaky.n += 1
            raise CaptionsBlocked("429 Too Many Requests")

    sleeps = []
    r = _router(Flaky(), sleeps=sleeps)
    meta = VideoMeta("x", "t", "c", "", 10.0)
    assert r.get("x", meta)[2] == "captions_blocked"
    assert Flaky.n == 3
    assert len([s for s in sleeps if s and s != 3.0]) >= 2


def test_429_recovers(yt):
    class Once:
        n = 0

        def captions(self, vid):
            Once.n += 1
            if Once.n == 1:
                raise CaptionsBlocked("429 Too Many Requests")
            return [Cue(0.0, 1.0, "hi")]

    r = _router(Once())
    assert r.get("x", VideoMeta("x", "t", "c", "", 10.0))[0] == "captions"


def test_no_captions_reason(yt):
    assert _router(yt).get("nocaps00001", _meta(yt, "nocaps00001")) == (None, [], "no_captions")


def test_unexpected_error_does_not_escape(yt):
    class Bad:
        def captions(self, vid):
            raise ValueError("malformed")

    def boom(*a, **k):
        raise RuntimeError("x")

    r = _router(Bad(), routes=("captions", "gemini"), gemini_fn=boom)
    meta = VideoMeta("x", "t", "c", "", 10.0)
    assert r.get("x", meta) == (None, [], "gemini_error")
    assert _router(Bad()).get("x", meta) == (None, [], "captions_error")


def test_gemini_quota(yt):
    def quota(*a, **k):
        raise LLMUnavailable("quota")

    r = _router(yt, routes=("gemini",), gemini_fn=quota)
    assert r.get("kube000001a", _meta(yt, "kube000001a")) == (None, [], "gemini_quota")


def test_ytdlp_route_opt_in(yt):
    got = []
    r = _router(yt, routes=("ytdlp_cookies",),
                ytdlp_fn=lambda vid, browser: got.append((vid, browser)) or [Cue(0.0, 1.0, "x")])
    assert r.get("v1", VideoMeta("v1", "t", "c", "", 5.0))[0] == "ytdlp_cookies"
    assert got == [("v1", "firefox")]


def test_gray_routes_off_by_default(yt):
    r = TranscriptRouter(Settings(), yt, gemini_fn=lambda *a, **k: pytest.fail("gemini"),
                         ytdlp_fn=lambda *a, **k: pytest.fail("ytdlp"),
                         breaker=CircuitBreaker(), throttle=Throttle(0, sleep=lambda s: None))
    assert r.get("blocked0001", _meta(yt, "blocked0001"))[0] is None
    assert r.get("nocaps00001", _meta(yt, "nocaps00001"))[0] is None


def test_video_document_with_chapters(yt):
    meta = _meta(yt, "kube000001a")
    cues = yt.captions("kube000001a")
    route, chapters = choose_segments(meta, [], cues, HashEmbedder())
    doc = video_document(meta, "captions", cues, route, chapters)
    assert doc.startswith("# Kubernetes in 10 minutes\n")
    assert "## [00:00] Intro\n\nWelcome, today we look at Kubernetes." in doc
    assert "## [00:30] Pods" in doc and "## [01:00] Services" in doc
    assert "0:00 Intro" not in doc  # description omitted when a transcript exists


def test_video_document_hour_format_and_no_transcript():
    meta = VideoMeta("v", "T", "c", "About this video", 8000.0)
    cues = [Cue(3700.0, 5.0, "late words")]
    doc = video_document(meta, "captions", cues, "creator", [Chapter(0, "A"), Chapter(3661, "B")])
    assert "## [0:00] A" not in doc and "## [00:00] A" in doc and "## [1:01:01] B" in doc
    assert "late words" in doc.split("## [1:01:01] B")[1]
    nodoc = video_document(meta, None, [], "whole", [Chapter(0, "T")])
    assert "About this video" in nodoc and nodoc.startswith("# T")


def test_empty_route_results_carry_a_reason(yt):
    class Empty:
        def captions(self, vid):
            return []

    meta = VideoMeta("x", "t", "c", "", 10.0)
    assert _router(Empty()).get("x", meta) == (None, [], "no_captions")
    r = _router(Empty(), routes=("captions", "ytdlp_cookies"), ytdlp_fn=lambda vid, browser: [])
    assert r.get("x", meta) == (None, [], "ytdlp_cookies_empty")


def test_breaker_rechecked_after_throttle_wait(yt):
    calls = []

    class Counting:
        def captions(self, vid):
            calls.append(vid)
            return [Cue(0.0, 1.0, "x")]

    breaker = CircuitBreaker(1800, clock=lambda: 0.0)
    throttle = Throttle(3.0, clock=lambda: 0.0, sleep=lambda s: None)
    throttle.wait = breaker.trip  # another worker trips the breaker while this one waits its turn
    r = TranscriptRouter(Settings(), Counting(), breaker=breaker, throttle=throttle, sleep=lambda s: None)
    assert r.get("x", VideoMeta("x", "t", "c", "", 10.0)) == (None, [], "captions_blocked")
    assert calls == []
