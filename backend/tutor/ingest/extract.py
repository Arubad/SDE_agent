"""Turn crawled HTML into clean Markdown. Pure functions; never raise on malformed HTML."""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass

import trafilatura
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

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


_STRUCTURAL = {"html", "body", "main", "article", "head"}


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove nav/header/footer/aside/scripts and small cookie containers, in place."""
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": "navigation"}):
        tag.decompose()
    total = len(soup.get_text(" ", strip=True)) or 1
    for tag in [t for t in soup.find_all(True) if _is_cookie(t)]:
        if tag.name in _STRUCTURAL or tag.decomposed:
            continue
        if tag.find(["main", "article"]) is not None:
            continue
        if len(tag.get_text(" ", strip=True)) * 2 > total:
            continue
        tag.decompose()


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _fallback_markdown(soup: BeautifulSoup) -> str:
    _strip_boilerplate(soup)
    root = soup.find("main") or soup.find("article") or soup.body or soup
    out: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, Comment):
                    text = " ".join(str(child).split())
                    if text:
                        out.append(text)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name == "pre":
                code = child.get_text().strip("\n")
                if code.strip():
                    out.append("```\n" + code + "\n```")
            elif name in ("li", "blockquote") and child.find("pre") is not None:
                walk(child)
            elif name.startswith("h") and len(name) == 2 and name[1] in "123456":
                if _text(child):
                    out.append("#" * int(name[1]) + " " + _text(child))
            elif name == "table":
                md = _table_md(child)
                if md:
                    out.append(md)
            elif name == "li":
                if _text(child):
                    out.append("- " + _text(child))
            elif name == "blockquote":
                if _text(child):
                    out.append("> " + _text(child))
            elif name == "p":
                if _text(child):
                    out.append(_text(child))
            else:
                walk(child)

    walk(root)
    return "\n\n".join(out)


def _squash(text: str) -> str:
    return "".join(html_lib.unescape(text).split())


def _pre_blocks(soup: BeautifulSoup) -> list[str]:
    blocks = []
    for pre in soup.find_all("pre"):
        text = _squash(pre.get_text())
        if text:
            blocks.append(text)
    return blocks


_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.S | re.M)


def _code_survives(markdown: str, pres: list[str]) -> bool:
    """True when every source <pre> text sits inside a fenced code block."""
    fenced = [_squash(m.group(2)) for m in _FENCE_RE.finditer(markdown)]
    return all(any(block in f for f in fenced) for block in pres)


def _title(html_soup: BeautifulSoup, meta_title: str | None) -> str:
    if meta_title and meta_title.strip():
        return meta_title.strip()
    if html_soup.title and html_soup.title.get_text(strip=True):
        return html_soup.title.get_text(strip=True)
    h1 = html_soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def extract_page(html: str, url: str) -> ExtractedPage:
    """Never raises: on total failure returns empty markdown."""
    try:
        return _extract_page(html, url)
    except Exception:
        return ExtractedPage(title="", markdown="", lang=None, word_count=0)


def _extract_page(html: str, url: str) -> ExtractedPage:
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

    title = _title(soup, meta_title)
    try:
        clean = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(clean)
        pres = _pre_blocks(clean)
        code_ok = _code_survives(markdown, pres)
        if _word_count(markdown) < MIN_WORDS or not code_ok:
            fallback = _fallback_markdown(BeautifulSoup(html, "html.parser"))
            fb_ok = _code_survives(fallback, pres)
            if (fb_ok, _word_count(fallback)) > (code_ok, _word_count(markdown)):
                markdown = fallback
    except Exception:
        pass
    markdown = markdown.strip()
    return ExtractedPage(title=title, markdown=markdown, lang=lang, word_count=_word_count(markdown))
