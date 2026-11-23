"""Manual live check of the roadmap parser against the real model (never run by tests; costs a few cents).

    backend/venv/bin/python -m backend.tutor.tools.check_roadmap_parse path/to/roadmap.txt

Prints the parsed spec, the token usage and cost of the call, and every change the URL rule made.
"""
from __future__ import annotations

import sys
from pathlib import Path

from backend.core.trace import start_trace
from backend.tutor.roadmap.parse import extract_urls, parse_roadmap


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = Path(argv[1]).read_text(encoding="utf-8")
    trace = start_trace("check_roadmap_parse")
    spec = parse_roadmap(source)
    print(spec.model_dump_json(indent=2))

    summary = trace.summary()
    print(f"\ntokens: in={summary['input_tokens']} out={summary['output_tokens']} cost=${summary['cost_usd']}")

    resources = [r for stage in spec.stages for r in stage.resources]
    source_urls = extract_urls(source)
    print(f"\nsource URLs: {len(source_urls)}")
    for res in resources:
        if res.url_hint:
            print(f"  moved to url_hint (not in source): {res.url_hint}  [{res.title}]")
        if res.url and res.kind == "other" and res.title == res.url:
            print(f"  appended (model dropped it): {res.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
