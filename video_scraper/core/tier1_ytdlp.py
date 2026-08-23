"""Tier 1 extraction: yt-dlp's library API, covering ~1800 sites."""

from __future__ import annotations

import asyncio
from typing import Any

import yt_dlp

from ..utils.http import KIND_DASH, KIND_DIRECT, KIND_HLS
from .models import Candidate, Extraction, Outcome, Tier

_DRM_MARKERS = (
    "widevine",
    "playready",
    "fairplay",
    "drm protected",
    "protected by drm",
    "encrypted media",
)

_MANIFEST_PROTOCOLS = {
    "m3u8": KIND_HLS,
    "m3u8_native": KIND_HLS,
    "dash": KIND_DASH,
}

_AUDIO_EXTS = {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac", "mka"}


def looks_like_drm(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _DRM_MARKERS)


def _rank_key(fmt: dict[str, Any]) -> tuple[float, float]:
    height = fmt.get("height") or 0
    tbr = fmt.get("tbr") or 0
    return (float(height), float(tbr))


def _is_audio_format(fmt: dict[str, Any]) -> bool:
    if fmt.get("vcodec") == "none":
        return True
    return str(fmt.get("ext") or "").lower() in _AUDIO_EXTS


def _is_progressive(fmt: dict[str, Any]) -> bool:
    """Audio+video in one file. Missing codec metadata counts optimistically
    (some extractors, e.g. archive.org, omit vcodec/acodec entirely)."""
    if _is_audio_format(fmt):
        return False
    has_audio = fmt.get("acodec") != "none"
    has_video = fmt.get("vcodec") != "none"
    return has_audio and has_video


def _is_video_only(fmt: dict[str, Any]) -> bool:
    if _is_audio_format(fmt):
        return False
    return fmt.get("acodec") == "none"


def _candidate_from_format(fmt: dict[str, Any], referer: str) -> Candidate | None:
    url = fmt.get("url")
    if not isinstance(url, str) or not url:
        return None
    protocol = str(fmt.get("protocol") or "")
    ext = fmt.get("ext")
    kind = _MANIFEST_PROTOCOLS.get(protocol)
    if kind is None and isinstance(ext, str):
        if ext == "m3u8":
            kind = KIND_HLS
        elif ext == "mpd":
            kind = KIND_DASH
    ext_str = ext if isinstance(ext, str) else None
    if ext_str and ext_str.startswith("."):
        ext_str = ext_str[1:]
    return Candidate(url=url, kind=kind or KIND_DIRECT, ext=ext_str, referer=referer)


def _info_is_drm(info: dict[str, Any]) -> bool:
    formats = info.get("formats") or []
    if not isinstance(formats, list) or not formats:
        return bool(info.get("has_drm"))
    flagged = [f for f in formats if isinstance(f, dict) and f.get("has_drm")]
    clean = [f for f in formats if isinstance(f, dict) and not f.get("has_drm")]
    return bool(flagged) and not clean


def best_candidate(info: dict[str, Any], referer: str) -> Candidate | None:
    """Pick the single most useful format.

    Preference order: progressive MP4 > any progressive > video-only (manifest
    or raw) > audio-only. Progressive files are preferred because they land as
    one playable file without muxing; higher-resolution adaptive variants are
    sacrificed for reliability. Documented in README.
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not formats:
        url = info.get("url")
        if isinstance(url, str) and url:
            candidate = Candidate(
                url=url,
                kind=KIND_DIRECT,
                ext=str(info["ext"]) if isinstance(info.get("ext"), str) else None,
                referer=referer,
            )
            return candidate
        return None

    usable = [
        f for f in formats
        if isinstance(f.get("url"), str) and f.get("protocol") != "m3u8_meta"
    ]
    progressive = sorted((f for f in usable if _is_progressive(f)), key=_rank_key, reverse=True)
    for fmt in progressive:
        if fmt.get("ext") == "mp4":
            return _candidate_from_format(fmt, referer)
    if progressive:
        return _candidate_from_format(progressive[0], referer)

    video_only = sorted((f for f in usable if _is_video_only(f)), key=_rank_key, reverse=True)
    if video_only:
        return _candidate_from_format(video_only[0], referer)

    audio_only = sorted(
        (f for f in usable if _is_audio_format(f)),
        key=_rank_key,
        reverse=True,
    )
    if audio_only:
        return _candidate_from_format(audio_only[0], referer)

    if usable:
        return _candidate_from_format(usable[-1], referer)
    return None


class YtdlpTier:
    tier = Tier.YT_DLP

    def extract_sync(self, url: str) -> Extraction:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 30,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            message = str(exc)
            outcome = Outcome.SKIPPED_DRM if looks_like_drm(message) else Outcome.FAILED
            return Extraction(outcome=outcome, tier=self.tier, reason=message)
        if not isinstance(info, dict):
            return Extraction(Outcome.FAILED, tier=self.tier, reason="yt-dlp returned no metadata")

        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
            if not entries:
                return Extraction(Outcome.FAILED, tier=self.tier, reason="playlist contained no entries")
            info = entries[0]

        if _info_is_drm(info):
            return Extraction(
                Outcome.SKIPPED_DRM,
                tier=self.tier,
                reason="all formats are DRM-protected",
            )

        candidate = best_candidate(info, referer=url)
        if candidate is None:
            return Extraction(Outcome.FAILED, tier=self.tier, reason="no usable formats found")

        title = info.get("title")
        return Extraction(
            Outcome.OK,
            tier=self.tier,
            title=title if isinstance(title, str) else None,
            candidates=[candidate],
        )

    async def extract(self, url: str) -> Extraction:
        return await asyncio.to_thread(self.extract_sync, url)
