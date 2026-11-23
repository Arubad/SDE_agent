"""End-to-end ingestion over a fixture net: handlers, crawl limits, retries, final status, report, re-check, CLI."""
import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.tutor.db import session_scope
from backend.tutor.ingest import handlers as H
from backend.tutor.ingest import report as R
from backend.tutor.ingest import service, video
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.handlers import HANDLERS
from backend.tutor.ingest.index import PlatformIndex
from backend.tutor.ingest.net import fixture_net
from backend.tutor.ingest.report import build_report, finalize_roadmap, video_summary
from backend.tutor.jobs import queue
from backend.tutor.jobs.worker import JobContext, WorkerPool, run_until_idle
from backend.tutor.models import Document, Job, Resource, Roadmap
from backend.tutor.repo import create_roadmap
from backend.tutor.roadmap.spec import ResourceSpec, RoadmapSpec, SkillSpec, StageSpec
from backend.tutor.settings import Settings

SHARED = Path(__file__).parent / "fixtures" / "net"
# 11-character ids (what discovery accepts) for copies of the shared video fixtures
VIDEO_COPIES = {"kube000001a": "kube000001a", "comment0001": "comments0001", "tsplit00001": "split0000001",
                "plwhole0001": "kube000002b", "nocaps00001": "nocaps00001"}


def _prose(topic: str, n: int = 6) -> str:
    return " ".join(f"{topic} detail number {i} is explained here in plain words for learners." for i in range(n))


def _page(title: str, body: str, links: str = "") -> str:
    return (f"<!doctype html><html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
            f"<p>{body}</p>{links}</main></body></html>")


SPA = ('<!doctype html><html><head><title>App</title></head><body><div id="root"></div>'
       "<noscript>Enable JavaScript</noscript></body></html>")
INSTALL_SENTENCE = "Install the kubectl binary and verify the client version before you create a cluster."


def _site(root: Path, pages: dict, playlists: dict | None = None) -> Path:
    """Write a fixture-net directory. pages: url -> html str | dict(status, content_type, body)."""
    (root / "pages").mkdir(parents=True, exist_ok=True)
    (root / "youtube").mkdir(exist_ok=True)
    (root / "playlists").mkdir(exist_ok=True)
    table = {}
    for i, (url, spec) in enumerate(pages.items()):
        spec = {"body": spec} if isinstance(spec, (str, bytes)) else dict(spec)
        body = spec.pop("body", "")
        name = f"p{i}"
        (root / "pages" / name).write_bytes(body if isinstance(body, bytes) else body.encode())
        table[url] = {"status": spec.get("status", 200), "content_type": spec.get("content_type", "text/html"),
                      "file": name, "headers": spec.get("headers", {})}
    (root / "pages.json").write_text(json.dumps(table))
    for new, old in VIDEO_COPIES.items():
        shutil.copy(SHARED / "youtube" / f"{old}.json", root / "youtube" / f"{new}.json")
    for lid, ids in (playlists or {}).items():
        (root / "playlists" / f"{lid}.json").write_text(json.dumps(ids))
    return root


@pytest.fixture(autouse=True)
def _clean(engine, monkeypatch):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
    monkeypatch.setattr(video, "CAPTIONS_THROTTLE", video.Throttle(0, sleep=lambda s: None))
    monkeypatch.setattr(video, "CAPTIONS_BREAKER", video.CircuitBreaker())
    yield


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(queue, "clock", lambda: now[0])
    return now


def _ctx(engine, site: Path, **kw) -> JobContext:
    kw.setdefault("ingest_agent", False)  # the pure pipeline; the agent has its own tests (test_ingest_agent.py)
    settings = Settings(net_fixtures_dir=site, qdrant_url=":memory:", embedder="hash", **kw)
    index = PlatformIndex(QdrantClient(":memory:"), "flow_chunks", HashEmbedder())
    return JobContext(engine=engine, net=fixture_net(site), settings=settings, index=index)


def _roadmap(engine, slug: str, resources: list[tuple[str, str, str | None]]):
    spec = RoadmapSpec(title=slug, goals=[], weekly_hours=None, stages=[StageSpec(
        id="s1", title="Stage", timeframe=None, summary="",
        skills=[SkillSpec(id="k1", title="K", objectives=[], prerequisites=[])],
        resources=[ResourceSpec(id=i, title=f"Res {i}", kind=k, url=u, url_hint=None, query=i, skill_ids=["k1"])
                   for i, k, u in resources],
        practice_goals=[])])
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title=slug, source_text="x")
        rm.status = "parsed"
        s.flush()
        service.approve(s, rm, spec, {}, agent=False)
        return rm.id


def _drain(ctx, rid, clock, handlers=HANDLERS):
    """Run inline until settled, jumping the fake clock past retry backoffs."""
    for _ in range(40):
        run_until_idle(ctx, handlers, roadmap_id=rid, on_settled=lambda r: finalize_roadmap(ctx.engine, r))
        with Session(ctx.engine) as s:
            if queue.is_roadmap_settled(s, rid):
                return
        clock[0] += timedelta(seconds=queue.BACKOFF_CAP_S * 2)
    pytest.fail("roadmap never settled")


def _status(engine, rid) -> str:
    with Session(engine) as s:
        return s.get(Roadmap, rid).status


def _resources(engine, rid) -> dict[str, Resource]:
    with Session(engine) as s:
        return {r.key: r for r in s.scalars(select(Resource).where(Resource.roadmap_id == rid))}


def _docs(engine, rid) -> list[Document]:
    with Session(engine) as s:
        return list(s.scalars(select(Document).where(Document.roadmap_id == rid)))


def _report(engine, rid) -> dict:
    with Session(engine) as s:
        return build_report(s, s.get(Roadmap, rid))


def _e2e_site(tmp_path):
    return _site(tmp_path / "net", {
        "https://flow.test/docs/": _page("Docs home", _prose("Cluster architecture"),
                                         '<a href="/docs/install">Install</a> <a href="/docs/spa">App</a>'),
        "https://flow.test/docs/install": _page("Install", INSTALL_SENTENCE + " Kubectl reads its kubeconfig file."),
        "https://flow.test/docs/spa": SPA,
        "https://flow.test/course/intro": _page("Course", _prose("Course overview"), "".join(
            f'<iframe src="https://www.youtube.com/embed/{v}"></iframe>'
            for v in ("kube000001a", "comment0001", "tsplit00001"))),
    }, playlists={"PLflow0001": ["plwhole0001", "nocaps00001"]})


def test_end_to_end_fixture_roadmap(engine, tmp_path, clock):
    ctx = _ctx(engine, _e2e_site(tmp_path))
    rid = _roadmap(engine, "e2e", [
        ("docs", "doc", "https://flow.test/docs/"),
        ("course", "course_site", "https://flow.test/course/intro"),
        ("pl", "playlist", "https://www.youtube.com/playlist?list=PLflow0001"),
        ("lab", "interactive", "https://killercoda.com/playgrounds"),
        ("gone", "article", "https://flow.test/gone"),
    ])
    _drain(ctx, rid, clock)

    res = _resources(engine, rid)
    assert {k: r.status for k, r in res.items()} == {
        "docs": "partial", "course": "done", "pl": "partial", "lab": "linked_only", "gone": "skipped"}
    assert res["gone"].reason == "not_found"
    assert res["docs"].stats["pages"] == 2 and res["course"].stats["videos"] == 3
    assert all(r.stats.get("chunks", 0) > 0 for k, r in res.items() if r.status in ("done", "partial"))
    videos = [d for d in _docs(engine, rid) if d.kind == "video"]
    assert len(videos) == 5
    assert all(d.status == "ok" and d.meta["segment_route"] for d in videos)
    assert all("cues" in d.meta["raw"] and "description" in d.meta["raw"] for d in videos)  # eval capture reads these
    nocaps = next(d for d in videos if d.meta["video_id"] == "nocaps00001")
    assert nocaps.reason == "no_captions" and nocaps.meta["transcript_route"] is None
    with Session(engine) as s:
        assert {j.status for j in s.scalars(select(Job).where(Job.roadmap_id == rid))} == {"done"}
    assert _status(engine, rid) == "ready_with_gaps"

    rep = _report(engine, rid)
    assert rep["summary"] == ("5 videos: 1 creator chapters, 1 comment timestamps, 1 transcript-split, 2 whole; "
                              "transcripts 4/5 (captions 4)")
    assert rep["videos"] == {"total": 5, "segments": {"creator": 1, "comments": 1, "transcript_split": 1, "whole": 2},
                             "transcripts": {"ok": 4, "by_route": {"captions": 4}}}
    assert sorted((x["url"], x["reason"]) for x in rep["skipped"]) == [
        ("https://flow.test/docs/spa", "needs_browser"), ("https://flow.test/gone", "not_found")]
    assert rep["status"] == "ready_with_gaps"
    entry = next(x for x in rep["resources"] if x["id"] == "course")
    assert set(entry) == {"id", "title", "kind", "url", "status", "reason", "pages", "videos", "chunks"}

    hits = ctx.index.search(INSTALL_SENTENCE, roadmap_id=str(rid), top_k=3)
    assert hits and hits[0].payload["url"] == "https://flow.test/docs/install"
    assert INSTALL_SENTENCE in hits[0].payload["text"]


def test_video_chunks_end_at_duration(engine, tmp_path, clock):
    ctx = _ctx(engine, _site(tmp_path / "net", {}))
    rid = _roadmap(engine, "vid", [("v", "video", "https://youtu.be/kube000001a")])
    _drain(ctx, rid, clock)
    with Session(engine) as s:
        ends = s.execute(text("SELECT max(end_s) FROM sections WHERE roadmap_id = :r"), {"r": rid}).scalar()
    assert ends == 90.0  # the fixture video's duration
    assert _resources(engine, rid)["v"].status == "done" and _status(engine, rid) == "ready"


def _chain_site(tmp_path):
    hub_links = "".join(f'<a href="/hub/p{i}">P{i}</a>' for i in range(1, 6))
    pages = {
        "https://flow.test/deep/": _page("Deep", _prose("Level zero"), '<a href="/deep/a">A</a>'),
        "https://flow.test/deep/a": _page("A", _prose("Level one"), '<a href="/deep/a/b">B</a>'),
        "https://flow.test/deep/a/b": _page("B", _prose("Level two"), '<a href="/deep/a/b/c">C</a>'),
        "https://flow.test/deep/a/b/c": _page("C", _prose("Level three")),
        "https://flow.test/hub/": _page("Hub", _prose("Hub page"), hub_links),
    }
    for i in range(1, 6):
        pages[f"https://flow.test/hub/p{i}"] = _page(f"P{i}", _prose(f"Hub child {i}"))
    return _site(tmp_path / "net", pages)


def test_crawl_limits(engine, tmp_path, clock):
    site = _chain_site(tmp_path)
    ctx = _ctx(engine, site, crawl_max_depth=2)
    rid = _roadmap(engine, "deep", [("deep", "doc", "https://flow.test/deep/")])
    _drain(ctx, rid, clock)
    assert sorted(d.url for d in _docs(engine, rid)) == [
        "https://flow.test/deep/", "https://flow.test/deep/a", "https://flow.test/deep/a/b"]

    ctx = _ctx(engine, site, crawl_max_pages=2)
    rid = _roadmap(engine, "hub", [("hub", "doc", "https://flow.test/hub/")])
    _drain(ctx, rid, clock)
    assert sorted(d.url for d in _docs(engine, rid)) == ["https://flow.test/hub/", "https://flow.test/hub/p1"]
    assert _resources(engine, rid)["hub"].stats["pages"] == 2


def test_resource_url_redirecting_to_another_host_crawls_that_host(engine, tmp_path, clock):
    """apex -> www (and any move of the resource's own page): links are followed on the host and under the
    root the resource actually lives at, recorded as Resource.meta.final_url."""
    links = '<a href="/docs/a">A</a><a href="/docs/b">B</a><a href="https://apex.test/docs/c">apex</a>'
    site = _site(tmp_path / "net", {
        "https://apex.test/docs/": {"status": 301, "body": "", "headers": {"location": "https://www.apex.test/docs/"}},
        "https://www.apex.test/docs/": _page("Docs", _prose("Docs root"), links + '<a href="/docs/">self</a>'),
        "https://www.apex.test/docs/a": _page("A", _prose("Page A"), '<a href="/docs/a/deeper">deeper</a>'),
        "https://www.apex.test/docs/a/deeper": _page("Deeper", _prose("Deeper page")),
        "https://www.apex.test/docs/b": _page("B", _prose("Page B")),
        "https://www.apex.test/other/x": _page("X", _prose("Outside the root")),
    })
    ctx = _ctx(engine, site)
    rid = _roadmap(engine, "redir", [("docs", "doc", "https://apex.test/docs/")])
    _drain(ctx, rid, clock)
    assert sorted(d.url for d in _docs(engine, rid)) == [
        "https://apex.test/docs/", "https://www.apex.test/docs/a", "https://www.apex.test/docs/a/deeper",
        "https://www.apex.test/docs/b"]
    r = _resources(engine, rid)["docs"]
    assert r.meta["final_url"] == "https://www.apex.test/docs/"
    assert (r.status, r.stats["pages"]) == ("done", 4) and _status(engine, rid) == "ready"


def test_redirect_hops_of_a_moved_resource_are_still_address_checked(engine, tmp_path, clock):
    """The crawl follows the resource to its new host, but every fetch there still passes the private-address
    guard: a sub-page link that redirects to a metadata address is refused, never requested."""
    site = _site(tmp_path / "net", {
        "https://apex.test/docs/": {"status": 301, "body": "", "headers": {"location": "https://www.apex.test/docs/"}},
        "https://www.apex.test/docs/": _page("Docs", _prose("Docs root"), '<a href="/docs/evil">E</a>'),
        "https://www.apex.test/docs/evil": {"status": 302, "body": "",
                                            "headers": {"location": "http://169.254.169.254/latest/"}},
    })
    ctx = _ctx(engine, site)
    rid = _roadmap(engine, "redir2", [("docs", "doc", "https://apex.test/docs/")])
    _drain(ctx, rid, clock)
    evil = next(d for d in _docs(engine, rid) if d.url.endswith("/evil"))
    assert (evil.status, evil.meta["detail"]) == ("failed", "private_address")


def test_page_cap_holds_with_concurrent_workers(engine, tmp_path):
    pages = {"https://flow.test/wide/": _page("Wide", _prose("Wide root"), "".join(
        f'<a href="/wide/c{i}">C{i}</a>' for i in range(8)))}
    for i in range(8):
        pages[f"https://flow.test/wide/c{i}"] = _page(f"C{i}", _prose(f"Wide child {i}"), "".join(
            f'<a href="/wide/c{i}/g{j}">G{j}</a>' for j in range(3)))
        for j in range(3):
            pages[f"https://flow.test/wide/c{i}/g{j}"] = _page(f"G{i}{j}", _prose(f"Grandchild {i} {j}"))
    ctx = _ctx(engine, _site(tmp_path / "net", pages), crawl_max_pages=4, crawl_max_depth=3)
    rid = _roadmap(engine, "wide", [("wide", "doc", "https://flow.test/wide/")])
    # a burst of reservations from several threads never passes the cap
    with Session(engine) as s:
        res_id = s.scalars(select(Resource.id).where(Resource.roadmap_id == rid)).one()
    got: list[bool] = []
    threads = [threading.Thread(target=lambda: got.append(H.reserve_page(engine, res_id, 4))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 4
    with session_scope(engine) as s:  # give the slots back and crawl for real with 3 workers
        s.execute(text("UPDATE resources SET stats = stats - 'pages' WHERE id = :id"), {"id": res_id})
    pool = WorkerPool(ctx, HANDLERS, 3, on_settled=lambda r: finalize_roadmap(engine, r))
    pool.start()
    try:
        deadline = time.monotonic() + 60
        while _status(engine, rid) not in service.TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        pool.stop()
    assert _status(engine, rid) == "ready"
    assert len([d for d in _docs(engine, rid) if d.kind == "page"]) == 4


def test_handler_crash_is_retried_then_reported(engine, tmp_path, clock):
    site = _site(tmp_path / "net", {"https://flow.test/one": _page("One", _prose("Flaky page"))})
    ctx = _ctx(engine, site)
    calls = []

    def flaky(c, job):
        calls.append(job.id)
        if len(calls) <= 2:
            raise RuntimeError("injected")
        return HANDLERS["fetch_page"](c, job)

    rid = _roadmap(engine, "flaky", [("one", "doc", "https://flow.test/one")])
    _drain(ctx, rid, clock, {**HANDLERS, "fetch_page": flaky})
    assert _resources(engine, rid)["one"].status == "done"
    with Session(engine) as s:
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")).one()
        assert (job.status, job.attempts) == ("done", 3)

    def broken(c, job):
        raise RuntimeError("always")

    rid = _roadmap(engine, "broken", [("one", "doc", "https://flow.test/one")])
    _drain(ctx, rid, clock, {**HANDLERS, "ingest_resource": broken})
    res = _resources(engine, rid)["one"]
    assert (res.status, res.reason) == ("skipped", "error")
    with Session(engine) as s:
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "ingest_resource")).one()
        assert (job.status, job.attempts) == ("dead", 5)
    assert _status(engine, rid) == "failed"
    assert _report(engine, rid)["skipped"] == [{"url": "https://flow.test/one", "reason": "error"}]


def test_fetch_error_retried_then_permanent(engine, tmp_path, clock):
    site = _site(tmp_path / "net", {"https://flow.test/err": {"status": 500, "body": "boom"}})
    ctx = _ctx(engine, site)
    rid = _roadmap(engine, "err", [("err", "doc", "https://flow.test/err")])
    _drain(ctx, rid, clock)
    with Session(engine) as s:
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")).one()
        assert (job.status, job.attempts) == ("dead", 5) and "PermanentError" in job.error
    doc = _docs(engine, rid)[0]
    assert (doc.status, doc.reason) == ("failed", "error")
    assert (_resources(engine, rid)["err"].reason, _status(engine, rid)) == ("error", "failed")
    assert _report(engine, rid)["skipped"] == [{"url": "https://flow.test/err", "reason": "error"}]


def test_robots_blip_is_retried_through_the_queue(engine, tmp_path, clock):
    """A robots.txt that times out once is not taken as allow-all (nor cached): the page job retries with
    the normal backoff, and the retry fetches the real robots.txt and the page."""
    import httpx

    site = _site(tmp_path / "net", {"https://flow.test/robots.txt": {"body": "User-agent: *\nAllow: /\n",
                                                                     "content_type": "text/plain"},
                                    "https://flow.test/page": _page("Page", _prose("Robots blip"))})
    ctx = _ctx(engine, site)
    real, calls = ctx.net.fetcher._client, []

    def handler(req):
        if req.url.path == "/robots.txt":
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out", request=req)
        return real.send(req)
    ctx.net.fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
    rid = _roadmap(engine, "blip", [("p", "doc", "https://flow.test/page")])
    _drain(ctx, rid, clock)
    with Session(engine) as s:
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")).one()
        assert (job.status, job.attempts) == ("done", 2)
    assert len(calls) == 2 and _resources(engine, rid)["p"].status == "done" and _status(engine, rid) == "ready"


def test_robots_denied_page_reported(engine, tmp_path, clock):
    site = _site(tmp_path / "net", {
        "https://robots.test/robots.txt": {"content_type": "text/plain", "body": "User-agent: *\nDisallow: /private"},
        "https://robots.test/private/doc": _page("Secret", _prose("Private")),
    })
    ctx = _ctx(engine, site)
    rid = _roadmap(engine, "robots", [("priv", "doc", "https://robots.test/private/doc")])
    _drain(ctx, rid, clock)
    res = _resources(engine, rid)["priv"]
    assert (res.status, res.reason) == ("skipped", "robots_denied")
    with Session(engine) as s:
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")).one()
        assert (job.status, job.attempts) == ("done", 1)  # reported, never retried
    assert _report(engine, rid)["skipped"] == [{"url": "https://robots.test/private/doc", "reason": "robots_denied"}]


def test_recheck_marks_dead_and_changed(engine, tmp_path, clock):
    site = _site(tmp_path / "net", {
        "https://flow.test/same": _page("Same", _prose("Stable page")),
        "https://flow.test/edit": _page("Edit", _prose("Edited page")),
        "https://flow.test/drop": _page("Drop", _prose("Dropped page")),
    })
    ctx = _ctx(engine, site)
    rid = _roadmap(engine, "re", [("same", "doc", "https://flow.test/same"), ("edit", "doc", "https://flow.test/edit"),
                                  ("drop", "doc", "https://flow.test/drop")])
    _drain(ctx, rid, clock)
    table = json.loads((site / "pages.json").read_text())
    (site / "pages" / table["https://flow.test/edit"]["file"]).write_text(_page("Edit", _prose("Rewritten page")))
    del table["https://flow.test/drop"]
    (site / "pages.json").write_text(json.dumps(table))
    ctx.net = fixture_net(site)

    out = H.recheck_roadmap(ctx, rid)
    assert {k: v["status"] for k, v in out.items()} == {"same": "ok", "edit": "changed", "drop": "dead"}
    docs = {d.url: d for d in _docs(engine, rid)}
    assert docs["https://flow.test/drop"].meta["recheck"]["status"] == "dead"
    assert docs["https://flow.test/edit"].meta["recheck"]["status"] == "changed"
    assert "at" in docs["https://flow.test/same"].meta["recheck"]
    assert _status(engine, rid) == "ready"  # a re-check never changes the roadmap status
    assert H.recheck_roadmap(ctx, rid)["drop"]["status"] == "dead"  # re-runnable


def test_reingest_and_empty_roadmap_reach_terminal(engine, tmp_path, clock):
    ctx = _ctx(engine, _site(tmp_path / "net", {"https://flow.test/one": _page("One", _prose("Again"))}))
    rid = _roadmap(engine, "again", [("one", "doc", "https://flow.test/one")])
    _drain(ctx, rid, clock)
    assert _status(engine, rid) == "ready"
    with session_scope(engine) as s:  # approve again: every job already exists and is done
        rm = s.get(Roadmap, rid)
        rm.status = "parsed"
        s.flush()
        service.approve(s, rm, RoadmapSpec(**rm.spec), {}, agent=False)
    assert _status(engine, rid) == "ingesting"
    H.ingest_roadmap(ctx, rid)
    assert _status(engine, rid) == "ready"

    rid = _roadmap(engine, "labs", [("lab", "interactive", "https://killercoda.com/playgrounds")])
    H.ingest_roadmap(ctx, rid)  # nothing was ever enqueued; nothing failed either
    assert _status(engine, rid) == "ready_with_gaps"
    assert _resources(engine, rid)["lab"].status == "linked_only"


def test_ingest_roadmap_with_worker_pool(engine, tmp_path):
    ctx = _ctx(engine, _site(tmp_path / "net", {"https://flow.test/one": _page("One", _prose("Pooled"))}))
    rid = _roadmap(engine, "pool", [("one", "doc", "https://flow.test/one")])
    assert H.ingest_roadmap(ctx, rid, until_idle=False, workers=2, timeout_s=60) == "ready"


def test_finalize_failure_marks_roadmap_failed(engine, tmp_path, clock, monkeypatch):
    ctx = _ctx(engine, _site(tmp_path / "net", {"https://flow.test/one": _page("One", _prose("Finalize"))}))
    rid = _roadmap(engine, "fin", [("one", "doc", "https://flow.test/one")])

    def boom(session, roadmap_id):
        session.execute(text("UPDATE resources SET status = 'half' WHERE roadmap_id = :r"), {"r": roadmap_id})
        raise RuntimeError("finalize crashed")

    monkeypatch.setattr(R, "_finalize", boom)
    _drain(ctx, rid, clock)
    assert _status(engine, rid) == "failed"
    assert _resources(engine, rid)["one"].status == "ingesting"  # the half-done update was rolled back


def _pdf(pages: list[str]) -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for txt in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({txt}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_and_github_repo(engine, tmp_path, clock, monkeypatch):
    from backend.tutor.settings import load_settings

    assert load_settings({"GITHUB_TOKEN": "secret-token"}).github_token == "secret-token"
    assert "secret-token" not in repr(load_settings({"GITHUB_TOKEN": "secret-token"}))
    api = "https://api.github.com/repos/acme/kit/contents"
    raw = "https://raw.githubusercontent.com/acme/kit/HEAD"
    site = _site(tmp_path / "net", {
        "https://flow.test/guide.pdf": {"content_type": "application/pdf",
                                        "body": _pdf(["Pods are the smallest deployable units.",
                                                      "Services route traffic to pods."])},
        f"{raw}/README.md": {"content_type": "text/plain", "body": "# Kit\n\n" + _prose("Readme intro")},
        f"{api}/docs": {"content_type": "application/json", "body": json.dumps([
            {"type": "file", "name": "a.md", "path": "docs/a.md", "download_url": f"{raw}/docs/a.md"},
            {"type": "file", "name": "logo.png", "path": "docs/logo.png", "download_url": f"{raw}/docs/logo.png"},
            {"type": "dir", "name": "deep", "path": "docs/deep", "download_url": None}])},
        f"{api}/docs/deep": {"content_type": "application/json", "body": json.dumps([
            {"type": "file", "name": "b.md", "path": "docs/deep/b.md", "download_url": f"{raw}/docs/deep/b.md"}])},
        f"{raw}/docs/a.md": {"content_type": "text/plain", "body": "# A\n\n" + _prose("Docs page a")},
        f"{raw}/docs/deep/b.md": {"content_type": "text/plain", "body": "# B\n\n" + _prose("Docs page b")},
    })
    ctx = _ctx(engine, site, github_token="secret-token")
    seen = []
    real_get = ctx.net.fetcher.get

    def spy(url, **kw):
        seen.append((url, (kw.get("headers") or {}).get("Authorization")))
        return real_get(url, **kw)

    ctx.net.fetcher.get = spy
    rid = _roadmap(engine, "files", [("pdf", "pdf", "https://flow.test/guide.pdf"),
                                     ("repo", "repo", "https://github.com/acme/kit")])
    _drain(ctx, rid, clock)
    docs = {d.url: d for d in _docs(engine, rid)}
    pdf = docs["https://flow.test/guide.pdf"]
    assert pdf.kind == "pdf" and "## Page 1" in pdf.markdown and "## Page 2" in pdf.markdown
    assert "Services route traffic" in pdf.markdown
    assert sorted(u for u, d in docs.items() if d.kind == "repo_file") == [
        "https://github.com/acme/kit/blob/HEAD/README.md", "https://github.com/acme/kit/blob/HEAD/docs/a.md",
        "https://github.com/acme/kit/blob/HEAD/docs/deep/b.md"]
    assert {r.status for r in _resources(engine, rid).values()} == {"done"}
    assert all(auth == "Bearer secret-token" for u, auth in seen if "github" in u)
    assert all("github" in u or auth is None for u, auth in seen)


def _repo_files(site: dict, repo: str, with_docs: bool = True) -> None:
    raw = f"https://raw.githubusercontent.com/{repo}/HEAD"
    site[f"{raw}/README.md"] = {"content_type": "text/plain", "body": f"# {repo}\n\n" + _prose(f"Readme of {repo}")}
    if with_docs:
        site[f"https://api.github.com/repos/{repo}/contents/docs"] = {
            "content_type": "application/json", "body": json.dumps([
                {"type": "file", "name": "a.md", "path": "docs/a.md", "download_url": f"{raw}/docs/a.md"}])}
        site[f"{raw}/docs/a.md"] = {"content_type": "text/plain", "body": "# A\n\n" + _prose(f"Docs of {repo}")}


def _repo_crawl_site(tmp_path, n: int):
    pages = {"https://flow.test/docs/": _page("Docs", _prose("Repo hub"), "".join(
        f'<a href="https://github.com/acme/r{i}">R{i}</a>' for i in range(n)))}
    for i in range(n):
        _repo_files(pages, f"acme/r{i}")
    return _site(tmp_path / "net", pages)


def test_discovered_repos_take_page_slots(engine, tmp_path, clock):
    ctx = _ctx(engine, _repo_crawl_site(tmp_path, 5), crawl_max_pages=3)
    rid = _roadmap(engine, "repos", [("docs", "doc", "https://flow.test/docs/")])
    _drain(ctx, rid, clock)
    docs = _docs(engine, rid)
    repos = {d.meta["repo"] for d in docs if d.kind == "repo_file"}
    assert len(repos) == 2  # the root page holds one of the 3 slots
    assert _resources(engine, rid)["docs"].stats["pages"] == 3


def test_repo_heavy_page_still_crawls_links(engine, tmp_path, clock):
    pages = {"https://flow.test/docs/": _page("Docs", _prose("Hub"), '<a href="/docs/x">X</a><a href="/docs/y">Y</a>'
                                              + "".join(f'<a href="https://github.com/acme/r{i}">R</a>' for i in range(4)))}
    for u in ("x", "y"):
        pages[f"https://flow.test/docs/{u}"] = _page(u.upper(), _prose(f"Child {u}"))
    for i in range(4):
        _repo_files(pages, f"acme/r{i}")
    ctx = _ctx(engine, _site(tmp_path / "net", pages), crawl_max_pages=4)
    rid = _roadmap(engine, "heavy", [("docs", "doc", "https://flow.test/docs/")])
    _drain(ctx, rid, clock)
    docs = _docs(engine, rid)
    assert len([d for d in docs if d.kind == "page"]) == 3
    assert len([d for d in docs if d.kind == "repo_file"]) == 1
    assert _resources(engine, rid)["docs"].stats["pages"] == 4


def test_discovered_repos_capped_per_resource(engine, tmp_path, clock):
    ctx = _ctx(engine, _repo_crawl_site(tmp_path, 8))
    rid = _roadmap(engine, "repocap", [("docs", "doc", "https://flow.test/docs/")])
    _drain(ctx, rid, clock)
    assert len({d.meta["repo"] for d in _docs(engine, rid) if d.kind == "repo_file"}) == H.GITHUB_DISCOVERED_MAX == 5
    assert _resources(engine, rid)["docs"].stats["repos"] == 5


def test_discovered_repo_stores_readme_only(engine, tmp_path, clock):
    ctx = _ctx(engine, _repo_crawl_site(tmp_path, 1))
    seen = []
    real_get = ctx.net.fetcher.get
    ctx.net.fetcher.get = lambda url, **kw: (seen.append(url), real_get(url, **kw))[1]
    rid = _roadmap(engine, "readme", [("docs", "doc", "https://flow.test/docs/")])
    _drain(ctx, rid, clock)
    assert [d.url for d in _docs(engine, rid) if d.kind == "repo_file"] == [
        "https://github.com/acme/r0/blob/HEAD/README.md"]
    assert not any("api.github.com" in u for u in seen)


def test_owner_repo_stores_readme_and_docs(engine, tmp_path, clock):
    pages: dict = {}
    _repo_files(pages, "acme/kit")
    ctx = _ctx(engine, _site(tmp_path / "net", pages))
    rid = _roadmap(engine, "owner", [("repo", "repo", "https://github.com/acme/kit")])
    _drain(ctx, rid, clock)
    assert sorted(d.url for d in _docs(engine, rid) if d.kind == "repo_file") == [
        "https://github.com/acme/kit/blob/HEAD/README.md", "https://github.com/acme/kit/blob/HEAD/docs/a.md"]


def test_video_summary_format():
    assert video_summary(0, {}, 0, {}) == "0 videos"
    segs = {"creator": 18, "comments": 9, "transcript_split": 6, "whole": 9}
    assert video_summary(42, segs, 30, {"captions": 24, "gemini": 6}) == (
        "42 videos: 18 creator chapters, 9 comment timestamps, 6 transcript-split, 9 whole; "
        "transcripts 30/42 (captions 24, Gemini 6)")
    assert video_summary(3, {"creator": 0, "comments": 0, "transcript_split": 0, "whole": 3}, 1,
                         {"captions": 0, "ytdlp_cookies": 1}) == (
        "3 videos: 0 creator chapters, 0 comment timestamps, 0 transcript-split, 3 whole; transcripts 1/3 (yt-dlp 1)")
    assert video_summary(2, {"whole": 2}, 0, {}) == (
        "2 videos: 0 creator chapters, 0 comment timestamps, 0 transcript-split, 2 whole; transcripts 0/2")


def test_cli_ingest_report_recheck(engine, db_url, tmp_path, monkeypatch, capsys):
    from backend.tutor.__main__ import main

    site = _site(tmp_path / "net", {"https://flow.test/one": _page("One", _prose("Command line"))})
    for k, v in {"DATABASE_URL": db_url, "NET_FIXTURES_DIR": str(site), "EMBEDDER": "hash",
                 "PLATFORM_QDRANT_URL": ":memory:", "TRANSCRIPT_ROUTES": "captions", "INGEST_AGENT": "false"}.items():
        monkeypatch.setenv(k, v)
    _roadmap(engine, "cli", [("one", "doc", "https://flow.test/one")])
    main(["ingest", "cli", "--until-idle"])
    capsys.readouterr()
    main(["report", "cli"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "ready" and rep["summary"] == "0 videos"
    assert rep["resources"][0]["status"] == "done"
    main(["recheck", "cli"])
    assert json.loads(capsys.readouterr().out) == {"one": "ok"}


def test_pipeline_redirect_to_metadata_address_is_refused(engine, tmp_path, clock):
    import httpx

    from backend.tutor.ingest.fetch import USER_AGENT, Fetcher, public_resolver

    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    ctx = _ctx(engine, _site(tmp_path / "net", {}))
    ctx.net.fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), domain_interval_s=0,
                              user_agent=USER_AGENT, resolver=public_resolver)
    rid = _roadmap(engine, "ssrf", [("m", "doc", "https://flow.test/moved")])
    _drain(ctx, rid, clock)
    doc = _docs(engine, rid)[0]
    assert (doc.status, doc.reason, doc.meta["detail"]) == ("failed", "error", "private_address")
    assert not any("169.254.169.254" in u for u in seen)
    with Session(engine) as s:  # refused, not retried: a private address does not become public
        job = s.scalars(select(Job).where(Job.roadmap_id == rid, Job.type == "fetch_page")).one()
        assert (job.status, job.attempts) == ("dead", 1)


def test_pipeline_hostname_resolving_privately_is_refused(engine, tmp_path, clock):
    site = _site(tmp_path / "net", {"https://internal.test/x": _page("X", _prose("Internal"))})
    ctx = _ctx(engine, site)
    ctx.net.fetcher._resolver = lambda host: ["10.0.0.7"]
    rid = _roadmap(engine, "dns", [("x", "doc", "https://internal.test/x")])
    _drain(ctx, rid, clock)
    doc = _docs(engine, rid)[0]
    assert (doc.status, doc.meta["detail"]) == ("failed", "private_address")
    assert _resources(engine, rid)["x"].status == "skipped"
