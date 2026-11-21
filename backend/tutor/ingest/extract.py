"""Turn crawled HTML into clean Markdown. Pure functions; never raise on malformed HTML."""
from __future__ import annotations

import re
from dataclasses import dataclass

import trafilatura
from bs4 import BeautifulSoup, Tag

MIN_WORDS = 50
_DROP_TAGS = ("nav", "header", "footer", "aside", "script", "style", "noscript", "template")
_COOKIE_RE = re.compile(r"cookie|consent|gdpr|cc-banner|cc-window", re.I)
_BLOCKS = ("h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "p", "li", "blockquote")


@dataclass
class ExtractedPage:
    title: str
    markdown: str
    lang: str | None
    word_count: int


def _word_count(text: str) -> int:
    return len(text.split())


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines()).strip()


def _cell(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()).replace("|", "\\|")


def _table_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [_cell(c) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _is_cookie(tag: Tag) -> bool:
    attrs = getattr(tag, "attrs", None) or {}
    ident = " ".join([str(attrs.get("id") or ""), " ".join(attrs.get("class") or [])])
    return bool(_COOKIE_RE.search(ident))


def _fallback_markdown(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": "navigation"}):
        tag.decompose()
    for tag in [t for t in soup.find_all(True) if _is_cookie(t)]:
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    out: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in _BLOCKS:
                if name.startswith("h") and len(name) == 2:
                    text = " ".join(child.get_text(" ", strip=True).split())
                    if text:
                        out.append("#" * int(name[1]) + " " + text)
                elif name == "pre":
                    code = child.get_text().strip("\n")
                    if code.strip():
                        out.append("```\n" + code + "\n```")
                elif name == "table":
                    md = _table_md(child)
                    if md:
                        out.append(md)
                elif name == "li":
                    text = " ".join(child.get_text(" ", strip=True).split())
                    if text:
                        out.append("- " + text)
                elif name == "blockquote":
                    text = " ".join(child.get_text(" ", strip=True).split())
                    if text:
                        out.append("> " + text)
                else:
                    text = " ".join(child.get_text(" ", strip=True).split())
                    if text:
                        out.append(text)
            else:
                walk(child)

    walk(root)
    return "\n\n".join(out)


def _pre_blocks(soup: BeautifulSoup) -> list[str]:
    blocks = []
    for pre in soup.find_all("pre"):
        text = _norm(pre.get_text())
        if text:
            blocks.append(text)
    return blocks


def _title(html_soup: BeautifulSoup, meta_title: str | None) -> str:
    if meta_title and meta_title.strip():
        return meta_title.strip()
    if html_soup.title and html_soup.title.get_text(strip=True):
        return html_soup.title.get_text(strip=True)
    h1 = html_soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def extract_page(html: str, url: str) -> ExtractedPage:
    """Extract main content as Markdown (headings, code blocks, tables kept; boilerplate removed)."""
    html = html or ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ExtractedPage(title="", markdown="", lang=None, word_count=0)
    html_tag = soup.find("html")
    lang = None
    if isinstance(html_tag, Tag):
        lang = (str(html_tag.get("lang") or "").strip() or None)

    meta_title = None
    markdown = ""
    try:
        markdown = (
            trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_tables=True,
                include_formatting=True,
                include_links=False,
                favor_recall=True,
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html, default_url=url)
        meta_title = getattr(meta, "title", None)
        if lang is None:
            lang = getattr(meta, "language", None) or None
    except Exception:
        markdown = ""

    pres = _pre_blocks(soup)
    lost_code = any(block not in markdown for block in pres)
    title = _title(soup, meta_title)
    if _word_count(markdown) < MIN_WORDS or lost_code:
        fb_soup = BeautifulSoup(html, "html.parser")
        fallback = _fallback_markdown(fb_soup)
        if lost_code or _word_count(fallback) > _word_count(markdown):
            markdown = fallback
    markdown = markdown.strip()
    return ExtractedPage(title=title, markdown=markdown, lang=lang, word_count=_word_count(markdown))
