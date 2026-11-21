"""Ingestion eval: scores the pipeline's pure stages on labelled pages and videos. No LLM, no network.

  discovery        precision / recall of the video and playlist ids `discover` finds, per page (macro mean;
                   an empty prediction has precision 1, an empty expectation recall 1)
  extraction       `must_contain_found`: share of the labelled phrases present in `extract_page`'s Markdown;
                   `must_not_contain_leakage`: share of the must-not phrases that are present anyway
  route_accuracy   `choose_segments` on the video as ingested picks `expected_route`
  boundary_f1      for videos with creator chapters: hide them (empty `chapters`, drop timestamp lines from the
                   description), run `choose_segments`, and compare the predicted chapter starts to the creator's
                   at a +-30 s tolerance (F1; the 0:00 start is shared by construction and ignored). Reported per
                   route that fired: `comments` and `transcript_split` (`whole`/`creator` outcomes are counted
                   under `other_routes` and not scored).

Data comes from fixture-net layouts (see ingest/net.py::fixture_net): the committed `eval/fixtures/ingestion/`
(kubernetes.io pages only, CC BY 4.0, see NOTICE.md) and the git-ignored `eval/cache/` (third-party pages,
transcripts and YouTube data; written by `capture_fixtures`). An item whose data is in neither is reported as
`skipped: not cached`, so `--ci` (committed fixtures only, HashEmbedder, no model download) passes on a clean
checkout. Without `--ci` the embedder is the configured one (`make_embedder(settings)`).

    python -m backend.tutor.eval.ingestion_eval --ci
    python -m backend.tutor.eval.ingestion_eval

Labels: `eval/data/ingestion_labels.json`. Results: `eval/results/ingestion_<ts>.json` and `ingestion_latest.md`."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.tutor.ingest.discover import discover
from backend.tutor.ingest.extract import extract_page
from backend.tutor.ingest.net import Chapter, Cue, VideoMeta
from backend.tutor.ingest.segment import _TS_LINE, choose_segments

HERE = Path(__file__).parent
LABELS_FILE = HERE / "data" / "ingestion_labels.json"
COMMITTED_DIR = HERE / "fixtures" / "ingestion"
CACHE_DIR = HERE / "cache"
RESULTS_DIR = HERE / "results"
TOLERANCE_S = 30.0
NOT_CACHED = "skipped: not cached"
SCORED_ROUTES = ("comments", "transcript_split")


class Store:
    """Reads fixture-net layouts (`pages.json` + `pages/`, `youtube/<id>.json`) from several roots; first hit wins."""

    def __init__(self, roots: list[Path]):
        self.roots = [Path(r) for r in roots]

    def page_html(self, url: str) -> str | None:
        for root in self.roots:
            table = root / "pages.json"
            if not table.is_file():
                continue
            entry = json.loads(table.read_text()).get(url)
            if entry and entry.get("file") and (root / "pages" / entry["file"]).is_file():
                return (root / "pages" / entry["file"]).read_text(errors="replace")
        return None

    def video(self, video_id: str) -> dict | None:
        for root in self.roots:
            path = root / "youtube" / f"{video_id}.json"
            if path.is_file():
                data = json.loads(path.read_text())
                if data.get("meta"):
                    return data
        return None


def _meta(video_id: str, raw: dict) -> VideoMeta:
    m = dict(raw)
    chapters = [Chapter(**c) for c in m.pop("chapters", [])]
    return VideoMeta(video_id=video_id, chapters=chapters, **m)


def hide_creator_chapters(meta: VideoMeta) -> VideoMeta:
    """The same video as if its creator had written no chapters: no structured chapters and no timestamp lines
    in the description."""
    kept = [ln for ln in meta.description.splitlines() if not (_TS_LINE.match(ln) and _TS_LINE.match(ln).group(2))]
    return VideoMeta(meta.video_id, meta.title, meta.channel, "\n".join(kept), meta.duration_s, [])


def prf(predicted: list[float], truth: list[float], tol: float = TOLERANCE_S) -> tuple[float, float, float]:
    """Precision, recall, F1 of boundary starts at +-`tol` seconds (greedy one-to-one matching); the 0:00 start
    is ignored. Two empty sets agree perfectly."""
    pred = sorted(p for p in predicted if p > 0)
    true = sorted(t for t in truth if t > 0)
    if not pred and not true:
        return 1.0, 1.0, 1.0
    unmatched, hits = list(true), 0
    for p in pred:
        near = [t for t in unmatched if abs(t - p) <= tol]
        if near:
            unmatched.remove(min(near, key=lambda t: abs(t - p)))
            hits += 1
    precision = hits / len(pred) if pred else 0.0
    recall = hits / len(true) if true else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _set_prf(found: list[str], expected: list[str]) -> tuple[float, float]:
    f, e = set(found), set(expected)
    precision = len(f & e) / len(f) if f else 1.0
    recall = len(f & e) / len(e) if e else 1.0
    return precision, recall


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def run_eval(labels: dict, roots: list[Path], embedder) -> dict:
    store = Store(roots)
    skipped: list[dict] = []
    pages: list[dict] = []
    v_p, v_r, p_p, p_r, found_rate, leak_rate = [], [], [], [], [], []
    for item in labels.get("pages", []):
        url = item["url"]
        html = store.page_html(url)
        if html is None:
            skipped.append({"item": url, "kind": "page", "reason": NOT_CACHED})
            continue
        found = discover(html, url)
        vp, vr = _set_prf(found.videos, item.get("expected_videos", []))
        pp, pr = _set_prf(found.playlists, item.get("expected_playlists", []))
        v_p.append(vp), v_r.append(vr), p_p.append(pp), p_r.append(pr)
        md = extract_page(html, url).markdown
        must, must_not = item.get("must_contain", []), item.get("must_not_contain", [])
        if must:
            found_rate.append(sum(s in md for s in must) / len(must))
        if must_not:
            leak_rate.append(sum(s in md for s in must_not) / len(must_not))
        pages.append({"url": url, "video_precision": vp, "video_recall": vr, "playlist_precision": pp,
                      "playlist_recall": pr,
                      "missing": [s for s in must if s not in md], "leaked": [s for s in must_not if s in md]})

    videos: list[dict] = []
    route_hits: list[float] = []
    per_route: dict[str, list[float]] = {}
    per_route_pr: dict[str, list[tuple[float, float]]] = {}
    other_routes: dict[str, int] = {}
    for item in labels.get("videos", []):
        vid = item["video_id"]
        raw = store.video(vid)
        if raw is None:
            skipped.append({"item": vid, "kind": "video", "reason": NOT_CACHED})
            continue
        meta = _meta(vid, raw["meta"])
        cues = [Cue(**c) for c in raw.get("cues", [])]
        comments = list(raw.get("comments", []))
        route, _ = choose_segments(meta, comments, cues, embedder)
        row = {"video_id": vid, "route": route, "expected_route": item.get("expected_route")}
        if item.get("expected_route"):
            route_hits.append(1.0 if route == item["expected_route"] else 0.0)
        truth = [c["start_s"] for c in item.get("creator_chapters", [])]
        if truth:
            hidden_route, chapters = choose_segments(hide_creator_chapters(meta), comments, cues, embedder)
            row["hidden_route"] = hidden_route
            if hidden_route in SCORED_ROUTES:
                p, r, f = prf([c.start_s for c in chapters], truth)
                per_route.setdefault(hidden_route, []).append(f)
                per_route_pr.setdefault(hidden_route, []).append((p, r))
                row.update(boundary_precision=round(p, 4), boundary_recall=round(r, 4), boundary_f1=round(f, 4))
            else:
                other_routes[hidden_route] = other_routes.get(hidden_route, 0) + 1
        videos.append(row)

    boundary = {r: {"f1": _mean(fs), "precision": _mean([p for p, _ in per_route_pr[r]]),
                    "recall": _mean([x for _, x in per_route_pr[r]]), "videos": len(fs)}
                for r, fs in per_route.items()}
    return {
        "tolerance_s": TOLERANCE_S,
        "metrics": {
            "discovery": {"video_precision": _mean(v_p), "video_recall": _mean(v_r),
                          "playlist_precision": _mean(p_p), "playlist_recall": _mean(p_r), "pages": len(pages)},
            "extraction": {"must_contain_found": _mean(found_rate), "must_not_contain_leakage": _mean(leak_rate)},
            "route_accuracy": {"accuracy": _mean(route_hits), "videos": len(route_hits)},
            "boundary_f1": boundary,
            "other_routes": other_routes,
            "skipped": skipped,
        },
        "pages": pages,
        "videos": videos,
    }


def format_markdown(result: dict) -> str:
    m = result["metrics"]
    d, x, r = m["discovery"], m["extraction"], m["route_accuracy"]
    lines = [
        "# Ingestion eval", "",
        f"Run {result['timestamp']} ({result['mode']}; embedder {result['embedder']}; no LLM calls). "
        f"{d['pages']} pages and {r['videos']} videos scored, {len(m['skipped'])} skipped.", "",
        "| Metric | Value |", "|---|---|",
        f"| Video discovery precision / recall | {d['video_precision']} / {d['video_recall']} |",
        f"| Playlist discovery precision / recall | {d['playlist_precision']} / {d['playlist_recall']} |",
        f"| Extraction: must_contain found | {x['must_contain_found']} |",
        f"| Extraction: must_not_contain leakage | {x['must_not_contain_leakage']} |",
        f"| Segment route accuracy | {r['accuracy']} ({r['videos']} videos) |", "",
        f"Boundary F1 at +-{int(result['tolerance_s'])} s, creator chapters hidden, per route that fired:", "",
        "| Route | F1 | Precision | Recall | Videos |", "|---|---|---|---|---|",
    ]
    for route, b in sorted(m["boundary_f1"].items()):
        lines.append(f"| {route} | {b['f1']} | {b['precision']} | {b['recall']} | {b['videos']} |")
    if not m["boundary_f1"]:
        lines.append("| (none fired) | | | | 0 |")
    if m["other_routes"]:
        lines += ["", f"Not scored (route after hiding): {m['other_routes']}"]
    if m["skipped"]:
        lines += ["", "Skipped:", ""] + [f"- {s['kind']} {s['item']}: {s['reason']}" for s in m["skipped"]]
    return "\n".join(lines) + "\n"


def write_results(result: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result["timestamp"]
    js = out_dir / f"ingestion_{stamp}.json"
    md = out_dir / "ingestion_latest.md"
    js.write_text(json.dumps(result, indent=2) + "\n")
    md.write_text(format_markdown(result))
    return js, md


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ci", action="store_true",
                   help="committed fixtures only (no cache), HashEmbedder: runs with no network or model download")
    p.add_argument("--labels", type=Path, default=LABELS_FILE)
    p.add_argument("--fixtures", type=Path, default=COMMITTED_DIR, help="committed fixture-net root")
    p.add_argument("--cache", type=Path, default=CACHE_DIR, help="git-ignored cache root (ignored with --ci)")
    p.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    args = p.parse_args(argv)
    if args.ci:
        from backend.tutor.ingest.embed import HashEmbedder

        embedder, name, roots = HashEmbedder(), "hash", [args.fixtures]
    else:
        from backend.tutor.ingest.embed import make_embedder
        from backend.tutor.settings import load_settings

        settings = load_settings()
        embedder, name, roots = make_embedder(settings), settings.embedder, [args.fixtures, args.cache]
    result = run_eval(json.loads(args.labels.read_text()), roots, embedder)
    result.update(timestamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                  mode="ci" if args.ci else "full", embedder=name, labels=str(args.labels))
    js, md = write_results(result, args.out_dir)
    print(format_markdown(result))
    print(f"wrote {js} and {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
