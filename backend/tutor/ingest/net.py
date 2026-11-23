"""The network seam: every network touch of the ingestion pipeline goes through one `Net`.

`make_net` returns real adapters, or a fixture-backed `Net` when NET_FIXTURES_DIR is set, so unit
tests, E2E and the CI eval run with no internet. The real YouTube adapter is deliberately thin;
routing between transcript routes, throttling and circuit breaking live above it."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import Engine

from backend.tutor.ingest.fetch import TIMEOUT_S, USER_AGENT, Fetcher, HttpCacheStore, public_resolver
from backend.tutor.settings import Settings


class SearchUnavailable(RuntimeError):
    pass


class CaptionsBlocked(RuntimeError):
    """YouTube is refusing this IP (blocked or rate limited); retrying right away will not help."""


class NoCaptions(RuntimeError):
    """The video has no transcript, or transcripts are disabled."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str


@dataclass(frozen=True)
class Chapter:
    start_s: float
    title: str


@dataclass(frozen=True)
class Cue:
    start: float
    dur: float
    text: str


@dataclass(frozen=True)
class VideoMeta:
    video_id: str
    title: str
    channel: str
    description: str
    duration_s: float
    chapters: list[Chapter] = field(default_factory=list)  # empty if the source gave none


class Search(Protocol):
    def search(self, query: str, *, category: str = "general") -> list[SearchResult]: ...


class YouTube(Protocol):
    def video_meta(self, video_id: str) -> VideoMeta | None: ...
    def playlist_items(self, list_id: str, limit: int) -> list[str]: ...
    def top_comments(self, video_id: str, limit: int = 20) -> list[str]: ...
    def captions(self, video_id: str) -> list[Cue]: ...


@dataclass
class Net:
    fetcher: Fetcher
    search: Search | None
    youtube: YouTube


# -- real adapters ------------------------------------------------------------------------------
class SearxngSearch:
    def __init__(self, client: httpx.Client, base_url: str):
        self._client = client
        self._base = base_url.rstrip("/")

    def search(self, query: str, *, category: str = "general") -> list[SearchResult]:
        try:
            resp = self._client.get(f"{self._base}/search",
                                    params={"q": query, "format": "json", "categories": category})
            resp.raise_for_status()
            items = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError) as e:
            raise SearchUnavailable(f"SearXNG at {self._base} is unavailable: {e}") from e
        out, seen = [], set()
        for r in items:
            url = r.get("url") or ""
            try:
                parsed = urlsplit(url)
                ok = parsed.scheme in ("http", "https") and bool(parsed.hostname)
            except ValueError:
                ok = False
            if not ok or url in seen:  # brave sometimes returns host-less redirects like http:///go/v1/...
                continue
            seen.add(url)
            # engines hand SearXNG titles still HTML-escaped ("Pods &amp; Services"); shown and proposed as text
            title = html.unescape(str(r.get("title") or ""))
            out.append(SearchResult(title, url, r.get("content", ""), r.get("engine", "")))
        return out


_ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _parse_iso_duration(value: str) -> float:
    m = _ISO_DURATION.match(value or "")
    h, mi, s = (int(g or 0) for g in m.groups()) if m else (0, 0, 0)
    return float(h * 3600 + mi * 60 + s)


def _first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or [""])[0]


class DataApiYouTube:
    """YouTube Data API v3. Used for metadata, playlists and comments when a key is configured."""

    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, client: httpx.Client, api_key: str):
        self._client = client
        self._key = api_key

    def _get(self, path: str, **params) -> dict:
        resp = self._client.get(f"{self.BASE}/{path}", params={**params, "key": self._key})
        resp.raise_for_status()
        return resp.json()

    def video_meta(self, video_id: str) -> VideoMeta | None:
        items = self._get("videos", part="snippet,contentDetails", id=video_id).get("items", [])
        if not items:
            return None
        sn = items[0]["snippet"]
        return VideoMeta(
            video_id, sn.get("title", ""), sn.get("channelTitle", ""), sn.get("description", ""),
            _parse_iso_duration(items[0].get("contentDetails", {}).get("duration", "")),
        )

    def playlist_items(self, list_id: str, limit: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            params = {"part": "contentDetails", "playlistId": list_id, "maxResults": min(50, limit - len(ids))}
            if token:
                params["pageToken"] = token
            data = self._get("playlistItems", **params)
            ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def top_comments(self, video_id: str, limit: int = 20) -> list[str]:
        data = self._get("commentThreads", part="snippet", videoId=video_id, order="relevance",
                         maxResults=min(100, limit), textFormat="plainText")
        return [i["snippet"]["topLevelComment"]["snippet"]["textOriginal"]
                for i in data.get("items", [])][:limit]


class RealYouTube:
    """Data API when a key is set, else yt-dlp metadata (never downloads, no cookies), else oEmbed
    for the title alone. Captions come from youtube-transcript-api."""

    def __init__(self, client: httpx.Client, api_key: str | None = None):
        self._client = client
        self._api = DataApiYouTube(client, api_key) if api_key else None

    def video_meta(self, video_id: str) -> VideoMeta | None:
        attempts = ([self._api_meta] if self._api else []) + [self._ytdlp_meta, self._oembed_meta]
        for attempt in attempts:
            try:
                meta = attempt(video_id)
            except Exception:  # each source fails in its own ways; fall through to the next
                continue
            if meta:
                return meta
        return None

    def _api_meta(self, video_id: str) -> VideoMeta | None:
        return self._api.video_meta(video_id)

    def _ytdlp_meta(self, video_id: str) -> VideoMeta | None:
        import yt_dlp

        opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            return None
        chapters = [Chapter(float(c["start_time"]), c.get("title") or "") for c in info.get("chapters") or []]
        return VideoMeta(video_id, info.get("title") or "", info.get("channel") or info.get("uploader") or "",
                         info.get("description") or "", float(info.get("duration") or 0), chapters)

    def _oembed_meta(self, video_id: str) -> VideoMeta | None:
        url = quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        resp = self._client.get(f"https://www.youtube.com/oembed?url={url}&format=json")
        resp.raise_for_status()
        data = resp.json()
        return VideoMeta(video_id, data.get("title", ""), data.get("author_name", ""), "", 0.0)

    def playlist_items(self, list_id: str, limit: int) -> list[str]:
        try:
            if self._api:
                return self._api.playlist_items(list_id, limit)
        except httpx.HTTPError:
            pass
        try:
            import yt_dlp

            opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                    "extract_flat": "in_playlist", "playlistend": limit}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)
            return [e["id"] for e in (info or {}).get("entries") or [] if e and e.get("id")][:limit]
        except Exception:
            return []

    def top_comments(self, video_id: str, limit: int = 20) -> list[str]:
        if not self._api:
            return []
        try:
            return self._api.top_comments(video_id, limit)
        except (httpx.HTTPError, KeyError):  # e.g. comments disabled
            return []

    def captions(self, video_id: str) -> list[Cue]:
        from youtube_transcript_api import (
            RequestBlocked,
            TranscriptsDisabled,
            YouTubeTranscriptApi,
        )

        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):  # older releases
                segs = [(s["start"], s.get("duration", 0.0), s["text"])
                        for s in YouTubeTranscriptApi.get_transcript(video_id)]
            else:
                segs = [(s.start, s.duration, s.text) for s in YouTubeTranscriptApi().fetch(video_id)]
        except RequestBlocked as e:  # includes IpBlocked
            raise CaptionsBlocked(_first_line(e) or "blocked") from e
        except TranscriptsDisabled as e:
            raise NoCaptions("transcripts are disabled for this video") from e
        except Exception as e:  # the protocol promises only CaptionsBlocked and NoCaptions
            if "429" in str(e) or "too many requests" in str(e).lower():
                raise CaptionsBlocked(_first_line(e) or "too many requests") from e
            raise NoCaptions(_first_line(e) or type(e).__name__) from e
        return [Cue(float(a), float(b), str(c).replace("\n", " ")) for a, b, c in segs]


# -- fixture mode -------------------------------------------------------------------------------
class FixtureSearch:
    def __init__(self, table: dict[str, list[dict]]):
        self._table = table

    def search(self, query: str, *, category: str = "general") -> list[SearchResult]:
        return [SearchResult(**r) for r in self._table.get(f"{category}:{query}", [])]


class FixtureYouTube:
    def __init__(self, root: Path):
        self._root = root

    def _video(self, video_id: str) -> dict | None:
        path = self._root / "youtube" / f"{video_id}.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def video_meta(self, video_id: str) -> VideoMeta | None:
        data = self._video(video_id)
        if not data or not data.get("meta"):
            return None
        m = dict(data["meta"])
        chapters = [Chapter(**c) for c in m.pop("chapters", [])]
        return VideoMeta(video_id=video_id, chapters=chapters, **m)

    def playlist_items(self, list_id: str, limit: int) -> list[str]:
        path = self._root / "playlists" / f"{list_id}.json"
        return json.loads(path.read_text())[:limit] if path.is_file() else []

    def top_comments(self, video_id: str, limit: int = 20) -> list[str]:
        return list((self._video(video_id) or {}).get("comments", []))[:limit]

    def captions(self, video_id: str) -> list[Cue]:
        data = self._video(video_id) or {}
        error = data.get("captions_error")
        if error == "blocked":
            raise CaptionsBlocked("fixture: captions blocked")
        if error or not data.get("cues"):
            raise NoCaptions("fixture: no captions")
        return [Cue(**c) for c in data["cues"]]


def fixture_net(root: Path) -> Net:
    """A Net that serves only what is in `root` (see tests/fixtures/net). Unknown URLs are 404,
    unknown queries are empty. Never throttled: it touches no real host."""
    root = Path(root)

    def load(name: str) -> dict:
        path = root / name
        return json.loads(path.read_text()) if path.is_file() else {}

    pages = load("pages.json")

    def handler(request: httpx.Request) -> httpx.Response:
        entry = pages.get(str(request.url))
        if entry is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})
        body = (root / "pages" / entry["file"]).read_bytes() if entry.get("file") else b""
        headers = {"content-type": entry.get("content_type", "text/html"), **entry.get("headers", {})}
        return httpx.Response(entry.get("status", 200), content=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(client, domain_interval_s=0, user_agent=USER_AGENT, cache=None, resolver=public_resolver)
    return Net(fetcher=fetcher, search=FixtureSearch(load("search.json")), youtube=FixtureYouTube(root))


def make_net(settings: Settings, engine: Engine | None) -> Net:
    if settings.net_fixtures_dir:
        return fixture_net(settings.net_fixtures_dir)
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    fetcher = Fetcher(client, domain_interval_s=settings.domain_interval_s, user_agent=USER_AGENT,
                      cache=HttpCacheStore(engine) if engine is not None else None)
    search = SearxngSearch(client, settings.searxng_url) if settings.searxng_url else None
    return Net(fetcher=fetcher, search=search, youtube=RealYouTube(client, settings.youtube_api_key))
