"""Turn a live ingestion run into eval fixtures: copies the labelled pages and videos out of the platform database
into fixture-net layouts (ingest/net.py::fixture_net) that `ingestion_eval` reads.

  pages    each labelled page URL's body, status and headers from the `http_cache` table (the crawl's cache).
           kubernetes.io pages (CC BY 4.0, credited in fixtures/NOTICE.md) go to the committed
           `eval/fixtures/ingestion/`; every other host goes to the git-ignored `eval/cache/`.
  videos   each labelled video's `Document(kind=video)` of the roadmap: title/channel/duration from its columns and
           `meta`, and description, creator chapters, top comments and raw cues from `meta["raw"]`, which the
           `video` handler stores (bounded: 12000 cues, 20 comments). Always written to `eval/cache/` (transcripts
           and YouTube data are not redistributed). A video ingested before `meta["raw"]` existed cannot be
           rebuilt (the Markdown document has no cue timings): it is reported, and re-ingesting it fixes that.

    python -m backend.tutor.eval.capture_fixtures --roadmap kubernetes            # uses DATABASE_URL

Items not found are reported, never fatal. Re-running overwrites earlier captures of the same item."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import Engine, select

from backend.tutor.db import make_engine, session_scope
from backend.tutor.eval.ingestion_eval import CACHE_DIR, COMMITTED_DIR, LABELS_FILE
from backend.tutor.models import Document, HttpCache, Roadmap

COMMITTED_HOST = "kubernetes.io"


def is_committable(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == COMMITTED_HOST or host.endswith("." + COMMITTED_HOST)


def _page_file(url: str) -> str:
    parts = urlsplit(url)
    slug = "".join(ch if ch.isalnum() else "-" for ch in f"{parts.hostname}{parts.path}").strip("-")[:60]
    return f"{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.html"


def _write_page(root: Path, url: str, row: HttpCache) -> None:
    (root / "pages").mkdir(parents=True, exist_ok=True)
    name = _page_file(url)
    (root / "pages" / name).write_bytes(bytes(row.body))
    table = root / "pages.json"
    pages = json.loads(table.read_text()) if table.is_file() else {}
    headers = {k.lower(): v for k, v in (row.headers or {}).items()}
    pages[url] = {"status": row.status, "content_type": headers.get("content-type", "text/html"),
                  "file": name, "headers": {}}
    table.write_text(json.dumps(pages, indent=2) + "\n")


def video_fixture(doc: Document) -> dict | None:
    raw = (doc.meta or {}).get("raw")
    if raw is None:
        return None
    meta = doc.meta
    return {
        "meta": {"title": doc.title, "channel": meta.get("channel", ""), "description": raw.get("description", ""),
                 "duration_s": meta.get("duration_s") or 0.0, "chapters": raw.get("chapters", [])},
        "cues": [{"start": s, "dur": d, "text": t} for s, d, t in raw.get("cues", [])],
        "comments": raw.get("comments", []),
        "captions_error": None if raw.get("cues") else "none",
    }


def capture(engine: Engine, slug: str, labels: dict, fixtures_dir: Path, cache_dir: Path) -> dict:
    report: dict = {"pages": [], "videos": [], "missing": []}
    with session_scope(engine) as s:
        rm = s.scalar(select(Roadmap).where(Roadmap.slug == slug))
        if rm is None:
            raise SystemExit(f"roadmap {slug!r} not found")
        for item in labels.get("pages", []):
            url = item["url"]
            row = s.get(HttpCache, url)
            if row is None or row.status != 200:
                report["missing"].append(f"page {url}: " + ("not in http_cache" if row is None else f"status {row.status}"))
                continue
            root = fixtures_dir if is_committable(url) else cache_dir
            _write_page(root, url, row)
            report["pages"].append({"url": url, "to": str(root)})
        for item in labels.get("videos", []):
            vid = item["video_id"]
            doc = s.scalar(select(Document).where(
                Document.roadmap_id == rm.id, Document.kind == "video",
                Document.url == f"https://www.youtube.com/watch?v={vid}"))
            fx = video_fixture(doc) if doc else None
            if fx is None:
                report["missing"].append(f"video {vid}: " + ("no document" if doc is None else
                                         "no raw data (ingested before capture support); re-ingest it"))
                continue
            (cache_dir / "youtube").mkdir(parents=True, exist_ok=True)
            (cache_dir / "youtube" / f"{vid}.json").write_text(json.dumps(fx, indent=1) + "\n")
            report["videos"].append({"video_id": vid, "cues": len(fx["cues"]), "to": str(cache_dir)})
    return report


def main(argv: list[str] | None = None) -> int:
    from backend.tutor.settings import load_settings

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--roadmap", required=True, help="roadmap slug, e.g. kubernetes")
    p.add_argument("--labels", type=Path, default=LABELS_FILE)
    p.add_argument("--fixtures", type=Path, default=COMMITTED_DIR)
    p.add_argument("--cache", type=Path, default=CACHE_DIR)
    args = p.parse_args(argv)
    engine = make_engine(load_settings().database_url)
    try:
        report = capture(engine, args.roadmap, json.loads(args.labels.read_text()), args.fixtures, args.cache)
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
