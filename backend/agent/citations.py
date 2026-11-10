"""Claude search_result blocks and API-native citation rendering (shared by the pipeline and the loop)."""
from __future__ import annotations


def source_label(p: dict) -> str:
    label = p["title"] + (f" › {p['section']}" if p.get("section") else "")
    if p.get("timestamp"):
        label += f" @ {p['timestamp']}"
    return label


def search_result_block(p: dict) -> dict:
    return {"type": "search_result", "source": p["url"] or p["chunk_id"], "title": source_label(p),
            "content": [{"type": "text", "text": p["text"]}], "citations": {"enabled": True}}


def render_citations(message, passages: list[dict]) -> tuple[str, list[dict]]:
    """Text with [n] markers after each cited block, plus the cited passages numbered in first-cited order.

    `passages` must list every search_result block sent in the conversation, in order: the API's
    search_result_index counts all of them."""
    numbers: dict[int, int] = {}
    parts = []
    for block in message.content:
        if block.type != "text":
            continue
        marks = []
        for c in block.citations or []:
            if getattr(c, "type", "") == "search_result_location":
                idx = c.search_result_index
                if 0 <= idx < len(passages):
                    n = numbers.setdefault(idx, len(numbers) + 1)
                    if n not in marks:
                        marks.append(n)
        parts.append(block.text + "".join(f"[{n}]" for n in marks))
    text = "".join(parts).strip()
    sources = [{**passages[idx], "n": n} for idx, n in sorted(numbers.items(), key=lambda x: x[1])]
    return text, sources
