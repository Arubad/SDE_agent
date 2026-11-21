"""Find videos, playlists, PDFs, GitHub repos and same-site links in a page. Pure; never raises."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_LIST_ID = re.compile(r"[A-Za-z0-9_-]{2,64}")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtube-nocookie.com", "www.youtube-nocookie.com"}
_GH_RESERVED = {
    "about", "features", "orgs", "topics", "sponsors", "marketplace", "settings", "login", "pricing",
    "explore", "collections", "enterprise", "apps", "events", "trending", "notifications", "pulls",
    "issues", "search", "security", "customer-stories", "readme", "join", "new", "organizations",
    "site", "contact", "premium-support", "solutions", "resources", "github-copilot", "team",
}
_ASSET_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".css", ".js", ".json", ".xml",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".mp4", ".mp3", ".webm", ".woff", ".woff2", ".ttf",
    ".eot", ".csv", ".rss", ".atom", ".exe", ".dmg",
}


@dataclass
class Discovered:
    videos: list[str] = field(default_factory=list)
    playlists: list[str] = field(default_factory=list)
    pdfs: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _split(url: str):
    try:
        return urlsplit(url)
    except ValueError:
        return None


def parse_youtube(url: str) -> tuple[str | None, str | None]:
    """Return (video_id, playlist_id) for a YouTube URL; either may be None."""
    parts = _split(url)
    if parts is None:
        return None, None
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    video = playlist = None
    try:
        qs = parse_qs(parts.query)
    except Exception:
        qs = {}
    if host == "youtu.be":
        if segs and _VIDEO_ID.fullmatch(segs[0]):
            video = segs[0]
    elif host in _YT_HOSTS:
        if segs[:1] == ["watch"]:
            v = (qs.get("v") or [""])[0]
            video = v if _VIDEO_ID.fullmatch(v) else None
        elif len(segs) >= 2 and segs[0] in ("embed", "shorts", "live"):
            video = segs[1] if _VIDEO_ID.fullmatch(segs[1]) and segs[1] != "videoseries" else None
        elif segs[:1] != ["playlist"]:
            return None, None
        lst = (qs.get("list") or [""])[0]
        if lst and _LIST_ID.fullmatch(lst):
            playlist = lst
    return video, playlist


def _repo(url: str) -> str | None:
    parts = _split(url)
    if parts is None or (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2 or segs[0].lower() in _GH_RESERVED:
        return None
    name = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", segs[0]) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return None
    return f"{segs[0]}/{name}"


def _json_embed_urls(data) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        if str(data.get("@type")) == "VideoObject" or "VideoObject" in (data.get("@type") or []):
            for key in ("embedUrl", "contentUrl"):
                if isinstance(data.get(key), str):
                    found.append(data[key])
        for v in data.values():
            if isinstance(v, (dict, list)):
                found += _json_embed_urls(v)
    elif isinstance(data, list):
        for v in data:
            found += _json_embed_urls(v)
    return found


def _default_root(page_path: str) -> str:
    path = page_path or "/"
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    return path


def _word_match(terms: list[str], text: str) -> bool:
    for term in terms:
        t = term.strip()
        if t and re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", text, re.I):
            return True
    return False


def discover(
    html: str, url: str, *, focus: list[str] = [], root: str | None = None
) -> Discovered:
    """Find embedded/linked videos, playlists, PDFs, GitHub repos and same-site links.

    Same-site links: same scheme and host as `url`, path under `root`, not assets, fragments stripped.
    `root` defaults to the page URL's directory (the page path up to its last '/'); a caller that knows
    the resource's root path can pass it explicitly. Order: links whose anchor text or path matches a
    `focus` term (case-insensitive whole-word) first, then shallower paths, then page order.
    """
    out = Discovered()
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return out
    base = _split(url)
    seen: dict[str, set] = {k: set() for k in ("videos", "playlists", "pdfs", "repos")}

    def add(kind: str, value: str) -> None:
        if value not in seen[kind]:
            seen[kind].add(value)
            getattr(out, kind).append(value)

    def handle_url(raw: str) -> str | None:
        """Classify a candidate URL; return the absolute URL (without fragment) or None."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            absolute = urljoin(url, raw)
        except ValueError:
            return None
        parts = _split(absolute)
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        video, playlist = parse_youtube(absolute)
        if video:
            add("videos", video)
        if playlist:
            add("playlists", playlist)
        repo = _repo(absolute)
        if repo:
            add("repos", repo)
        if parts.path.lower().endswith(".pdf"):
            add("pdfs", urldefrag(absolute)[0])
        return absolute

    anchors: dict[str, list[str]] = {}
    order: list[str] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        try:
            if tag.name == "iframe":
                handle_url(str(tag.get("src") or ""))
            elif tag.name == "a":
                absolute = handle_url(str(tag.get("href") or ""))
                if absolute:
                    key = urldefrag(absolute)[0]
                    if key not in anchors:
                        anchors[key] = []
                        order.append(key)
                    anchors[key].append(tag.get_text(" ", strip=True))
            elif tag.name == "script" and "ld+json" in str(tag.get("type") or ""):
                try:
                    data = json.loads(tag.string or tag.get_text() or "")
                except ValueError:
                    data = None
                for embed in _json_embed_urls(data):
                    handle_url(embed)
            vid = tag.get("data-video-id")
            if isinstance(vid, str) and _VIDEO_ID.fullmatch(vid.strip()):
                add("videos", vid.strip())
            ds = tag.get("data-src")
            if isinstance(ds, str) and tag.name != "iframe":
                handle_url(ds)
        except Exception:
            continue

    if base is None or not base.hostname:
        return out
    root_path = root if root is not None else _default_root(base.path)
    if not root_path.startswith("/"):
        root_path = "/" + root_path
    page_key = urldefrag(url)[0]
    cands = []
    for idx, key in enumerate(order):
        parts = _split(key)
        if parts is None or key == page_key:
            continue
        if parts.scheme != base.scheme or (parts.hostname or "").lower() != base.hostname.lower():
            continue
        if parts.port != base.port:
            continue
        path = parts.path or "/"
        if not path.startswith(root_path):
            continue
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if ext in _ASSET_EXT:
            continue
        text = " ".join(anchors[key]) + " " + re.sub(r"[/_\-.]+", " ", path)
        matched = _word_match(focus, text)
        depth = len([s for s in path.split("/") if s])
        cands.append((0 if matched else 1, depth, idx, key))
    out.links = [c[3] for c in sorted(cands)]
    return out
