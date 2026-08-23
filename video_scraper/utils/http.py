"""Shared HTTP client configuration, URL classification, and per-domain rate limiting."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 video-scraper/0.1"
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

KIND_DIRECT = "direct"
KIND_HLS = "hls"
KIND_DASH = "dash"

MANIFEST_EXTENSIONS = {".m3u8": KIND_HLS, ".mpd": KIND_DASH}
MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".ogv", ".3gp",
    ".ts", ".mpg", ".mpeg", ".wmv", ".flv",
    ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".flac",
}

CONTENT_TYPE_KIND = {
    "application/vnd.apple.mpegurl": KIND_HLS,
    "application/x-mpegurl": KIND_HLS,
    "application/dash+xml": KIND_DASH,
}

CONTENT_TYPE_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/ogg": ".ogv",
    "video/mp2t": ".ts",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
}


def build_client(**overrides: object) -> httpx.AsyncClient:
    defaults: dict[str, object] = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": DEFAULT_TIMEOUT,
        "follow_redirects": True,
    }
    defaults.update(overrides)
    return httpx.AsyncClient(**defaults)


def classify_url(url: str) -> str | None:
    path = urlparse(url).path.lower()
    for ext, kind in MANIFEST_EXTENSIONS.items():
        if path.endswith(ext):
            return kind
    dot = path.rfind(".")
    if dot != -1 and path[dot:] in MEDIA_EXTENSIONS:
        return KIND_DIRECT
    return None


def classify_content_type(content_type: str) -> str | None:
    ct = content_type.split(";")[0].strip().lower()
    if ct.startswith(("video/", "audio/")):
        return KIND_DIRECT
    return CONTENT_TYPE_KIND.get(ct)


def extension_from_content_type(content_type: str) -> str | None:
    return CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())


class DomainRateLimiter:
    """Enforce a minimum delay between requests to the same host."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = max(0.0, delay_seconds)
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str) -> None:
        if self._delay <= 0:
            return
        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            if elapsed < self._delay:
                await asyncio.sleep(self._delay - elapsed)
            self._last_hit[host] = time.monotonic()
