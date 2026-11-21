"""The ingestion eval (no LLM, no network): discovery, extraction, route accuracy and boundary agreement, on the
Task 10 fixture net plus a tiny labels file. Missing data is reported `skipped: not cached`, never a failure."""
import json
import shutil
from pathlib import Path

from backend.tutor.eval import ingestion_eval as E
from backend.tutor.ingest.embed import HashEmbedder

NET = Path(__file__).parent / "fixtures" / "net"
COURSE = "https://example.test/course"
COURSE_HTML = ('<html><body><main><h1>Course</h1><p>Watch the intro.</p>'
               '<iframe src="https://www.youtube.com/embed/kube000001a"></iframe>'
               '<a href="https://www.youtube.com/playlist?list=PLkube0001">list</a>'
               '<p>The pod is the smallest unit.</p></main></body></html>')


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "net"
    shutil.copytree(NET, root)
    pages = json.loads((root / "pages.json").read_text())
    pages[COURSE] = {"status": 200, "content_type": "text/html", "file": "course.html", "headers": {}}
    (root / "pages.json").write_text(json.dumps(pages))
    (root / "pages" / "course.html").write_text(COURSE_HTML)
    return root


LABELS = {
    "pages": [
        {"url": COURSE, "expected_videos": ["kube000001a"], "expected_playlists": ["PLkube0001"],
         "must_contain": ["smallest unit"], "must_not_contain": ["Watch the intro", "NOT-THERE"]},
        {"url": "https://example.test/gone", "expected_videos": [], "expected_playlists": [],
         "must_contain": [], "must_not_contain": []},
    ],
    "videos": [
        {"video_id": "comments0001", "expected_route": "comments",
         "creator_chapters": [{"start_s": 0, "title": "Intro"}, {"start_s": 100, "title": "Bridge networks"},
                              {"start_s": 200, "title": "Overlay networks"}]},
        {"video_id": "kube000001a", "expected_route": "creator",
         "creator_chapters": [{"start_s": 0, "title": "Intro"}, {"start_s": 30, "title": "Pods"},
                              {"start_s": 60, "title": "Services"}]},
        {"video_id": "uncached001", "expected_route": "whole", "creator_chapters": []},
    ],
}


def test_ingestion_eval_ci_mode_runs(tmp_path):
    result = E.run_eval(LABELS, [make_root(tmp_path)], HashEmbedder())
    for key in ("discovery", "extraction", "route_accuracy", "boundary_f1", "skipped"):
        assert key in result["metrics"]
    d = result["metrics"]["discovery"]
    assert d["video_recall"] == 1.0 and d["playlist_recall"] == 1.0 and d["video_precision"] == 1.0
    ex = result["metrics"]["extraction"]
    assert ex["must_contain_found"] == 1.0
    assert ex["must_not_contain_leakage"] == 0.5  # "Watch the intro" is in the page, NOT-THERE is not
    assert result["metrics"]["route_accuracy"]["accuracy"] == 1.0  # comments0001 and kube000001a
    f1 = result["metrics"]["boundary_f1"]
    assert f1["comments"]["f1"] == 1.0 and f1["comments"]["videos"] == 1
    skipped = {s["item"]: s["reason"] for s in result["metrics"]["skipped"]}
    assert skipped == {"https://example.test/gone": "skipped: not cached",
                       "uncached001": "skipped: not cached"}


def test_boundary_f1_tolerance_and_hiding():
    # zero is trivially shared and ignored; one of two predicted within 30 s, one of two true found
    p, r, f = E.prf([100, 400], [100, 200], tol=30)
    assert (p, r) == (0.5, 0.5) and f == 0.5
    assert E.prf([130], [100], tol=30)[2] == 1.0 and E.prf([131], [100], tol=30)[2] == 0.0


def test_main_ci_writes_results(tmp_path, monkeypatch):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps(LABELS))
    out = tmp_path / "out"
    rc = E.main(["--ci", "--labels", str(labels), "--fixtures", str(make_root(tmp_path)),
                 "--cache", str(tmp_path / "absent"), "--out-dir", str(out)])
    assert rc == 0
    assert (out / "ingestion_latest.md").read_text().startswith("# Ingestion eval")
    assert len(list(out.glob("ingestion_2*.json"))) == 1


def test_capture_routes_pages_by_licence_and_rebuilds_video_fixtures(engine, tmp_path):
    from sqlalchemy import text

    from backend.tutor.db import session_scope
    from backend.tutor.eval import capture_fixtures as C
    from backend.tutor.models import Document, HttpCache, Roadmap

    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
        rm = Roadmap(slug="cap-test", title="t", source_text="x")
        s.add(rm)
        s.flush()
        s.add_all([
            HttpCache(url="https://kubernetes.io/docs/concepts/", status=200, body=b"<html>k8s</html>",
                      headers={"Content-Type": "text/html; charset=utf-8"}),
            HttpCache(url="https://example.test/blog", status=200, body=b"<html>blog</html>", headers={}),
            Document(roadmap_id=rm.id, url="https://www.youtube.com/watch?v=abcdefghijk", kind="video", title="T",
                     meta={"channel": "C", "duration_s": 90.0,
                           "raw": {"description": "d", "chapters": [{"start_s": 0, "title": "a"}],
                                   "comments": ["c"], "cues": [[0.0, 4.0, "hello"]]}}),
            Document(roadmap_id=rm.id, url="https://www.youtube.com/watch?v=oldoldoldol", kind="video", title="O",
                     meta={"channel": "C"}),
        ])
    labels = {"pages": [{"url": "https://kubernetes.io/docs/concepts/"}, {"url": "https://example.test/blog"},
                        {"url": "https://example.test/absent"}],
              "videos": [{"video_id": "abcdefghijk"}, {"video_id": "oldoldoldol"}, {"video_id": "nodoc0000000"}]}
    fixtures, cache = tmp_path / "fx", tmp_path / "cache"
    report = C.capture(engine, "cap-test", labels, fixtures, cache)
    assert [p["url"] for p in report["pages"]] == ["https://kubernetes.io/docs/concepts/", "https://example.test/blog"]
    assert "https://kubernetes.io/docs/concepts/" in json.loads((fixtures / "pages.json").read_text())
    assert "https://example.test/blog" not in json.loads((fixtures / "pages.json").read_text())
    assert "https://example.test/blog" in json.loads((cache / "pages.json").read_text())
    assert not (fixtures / "youtube").exists()  # transcripts never go to the committed dir
    fx = json.loads((cache / "youtube" / "abcdefghijk.json").read_text())
    assert fx["meta"]["description"] == "d" and fx["cues"] == [{"start": 0.0, "dur": 4.0, "text": "hello"}]
    assert len(report["missing"]) == 3  # absent page, old video without raw data, video without a document
    # and what it wrote is readable by the eval
    assert E.Store([fixtures, cache]).video("abcdefghijk")["meta"]["title"] == "T"
    assert E.Store([fixtures, cache]).page_html("https://example.test/blog") == "<html>blog</html>"
