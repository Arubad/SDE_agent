"""Turn the legacy LlamaIndex docstore into clean source documents and chunks.

v1 = the legacy chunks exactly as they were indexed (the baseline).
v2 = documents reconstructed from those chunks, cleaned (translations dropped, near-duplicate
     pages removed), split along markdown sections into smaller chunks, each carrying a
     contextual header ("doc title > section") that is embedded with the text.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.core import config

LEGACY_DOCSTORE = config.BACKEND_DIR / "storage" / "docstore.json"
TRANSLATION_RE = re.compile(r"README-(ja|zh-TW|zh-Hans|zh-CN|ko|es|pt-BR|ru|fr|de|it|tr|ar|fa|th|vi|id|pl|uk)\.md$")


@dataclass
class Document:
    doc_id: str
    text: str
    title: str
    source_type: str  # github | book | web | youtube
    url: str
    meta: dict = field(default_factory=dict)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str          # what the LLM and the user see
    embed_text: str    # what gets embedded (text + contextual header)
    title: str
    section: str
    source_type: str
    url: str
    meta: dict = field(default_factory=dict)

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("embed_text")
        return d


def stable_id(*parts: str) -> str:
    """Deterministic UUID-shaped id, so re-ingesting the same content overwrites rather than duplicates."""
    h = hashlib.sha1("\x1f".join(parts).encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _describe(meta: dict) -> tuple[str, str, str]:
    """(source_type, title, url) for a legacy node's metadata."""
    src = meta.get("source", "")
    if src.startswith("github:"):
        owner_repo = src.split(":", 1)[1]
        path = meta.get("file_path", "")
        return "github", f"{owner_repo.split('/')[-1]}/{path}", f"https://github.com/{owner_repo}/blob/master/{path}"
    if src == "web":
        return "web", meta.get("title") or meta.get("url", "web page"), meta.get("url", "")
    if meta.get("page_label") is not None:
        return "book", f"Software Engineering at Google, p.{meta['page_label']}", meta.get("file_name", "")
    return "web", meta.get("file_name", "document"), meta.get("url", "")


def load_legacy_nodes(path: Path = LEGACY_DOCSTORE) -> list[dict]:
    data = json.loads(path.read_text())["docstore/data"]
    nodes = []
    for node_id, v in data.items():
        d = v["__data__"]
        source_rel = d.get("relationships", {}).get("1") or {}
        nodes.append({
            "id": node_id,
            "text": d.get("text", ""),
            "meta": d.get("metadata", {}),
            "ref_doc_id": source_rel.get("node_id", node_id),
            "start": d.get("start_char_idx"),
            "end": d.get("end_char_idx"),
        })
    return nodes


def legacy_chunks(nodes: list[dict] | None = None) -> list[Chunk]:
    """v1: the original chunks, untouched (no cleaning, no headers)."""
    chunks = []
    for n in nodes or load_legacy_nodes():
        if not n["text"].strip():
            continue
        st, title, url = _describe(n["meta"])
        chunks.append(Chunk(chunk_id=stable_id("v1", n["id"]), doc_id=n["ref_doc_id"], text=n["text"],
                            embed_text=n["text"], title=title, section="", source_type=st, url=url,
                            meta={k: n["meta"][k] for k in ("page_label", "repo", "file_path") if k in n["meta"]}))
    return chunks


def reconstruct_documents(nodes: list[dict] | None = None) -> list[Document]:
    """Stitch overlapping legacy chunks back into their source documents using char offsets."""
    by_doc: dict[str, list[dict]] = {}
    for n in nodes or load_legacy_nodes():
        by_doc.setdefault(n["ref_doc_id"], []).append(n)
    docs = []
    for doc_id, parts in by_doc.items():
        parts.sort(key=lambda n: (n["start"] is None, n["start"] or 0))
        text = ""
        for p in parts:
            if p["start"] is None or p["start"] > len(text):
                text += ("\n\n" if text else "") + p["text"]
            else:
                text += p["text"][len(text) - p["start"]:]
        st, title, url = _describe(parts[0]["meta"])
        docs.append(Document(doc_id=doc_id, text=text, title=title, source_type=st, url=url,
                             meta={k: parts[0]["meta"][k] for k in ("page_label", "repo", "file_path")
                                   if k in parts[0]["meta"]}))
    return docs


def _shingles(text: str, n: int = 5) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    return {hash(" ".join(words[i:i + n])) for i in range(max(0, len(words) - n + 1))}


def clean_documents(docs: list[Document]) -> tuple[list[Document], dict]:
    """Drop translated duplicates, code-free boilerplate, and web pages that duplicate a repo file."""
    report = {"input_docs": len(docs), "dropped_translations": 0, "dropped_boilerplate": 0, "dropped_near_duplicates": 0}
    kept = []
    for d in docs:
        path = d.meta.get("file_path", "")
        if TRANSLATION_RE.search(path):
            report["dropped_translations"] += 1
            continue
        if path.startswith(".github/") or path.endswith(("LICENSE.txt", "LICENSE", "CONTRIBUTING.md")):
            report["dropped_boilerplate"] += 1
            continue
        if len(d.text.strip()) < 80:
            report["dropped_boilerplate"] += 1
            continue
        kept.append(d)

    # Near-duplicate removal: web pages that mostly repeat a GitHub markdown file (Jaccard on 5-shingles)
    repo_shingles = [_shingles(d.text) for d in kept if d.source_type == "github"]
    final = []
    for d in kept:
        if d.source_type == "web":
            s = _shingles(d.text)
            if s and any(len(s & r) / max(1, len(s)) > 0.5 for r in repo_shingles if r):
                report["dropped_near_duplicates"] += 1
                continue
        final.append(d)
    report["output_docs"] = len(final)
    return final, report


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading path, body) pairs, ignoring '#' lines inside code fences."""
    sections, path, buf, in_code = [], [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            if "".join(buf).strip():
                sections.append((" > ".join(path), "\n".join(buf).strip()))
            level = len(m.group(1))
            path = path[: level - 1] + [re.sub(r"[*_`\[\]]|\(.*?\)", "", m.group(2)).strip()]
            buf = [line]
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((" > ".join(path), "\n".join(buf).strip()))
    return sections


def chunk_documents(docs: list[Document], chunk_tokens: int = 320, overlap_tokens: int = 48) -> list[Chunk]:
    """v2 chunking: markdown sections -> token-bounded pieces, each with a contextual header."""
    from llama_index.core.node_parser import SentenceSplitter

    splitter = SentenceSplitter(chunk_size=chunk_tokens, chunk_overlap=overlap_tokens)
    chunks = []
    for d in docs:
        is_md = d.source_type in ("github", "web", "youtube") and not d.meta.get("file_path", "").endswith(".py")
        sections = _markdown_sections(d.text) if is_md else [("", d.text)]
        for section, body in sections:
            for i, piece in enumerate(splitter.split_text(body)):
                if len(piece.strip()) < 40:
                    continue
                header = d.title + (f" > {section}" if section else "")
                chunks.append(Chunk(
                    chunk_id=stable_id("v2", d.doc_id, section, str(i), piece[:64]),
                    doc_id=d.doc_id, text=piece, embed_text=f"{header}\n\n{piece}", title=d.title,
                    section=section, source_type=d.source_type, url=d.url, meta=dict(d.meta),
                ))
    return chunks
