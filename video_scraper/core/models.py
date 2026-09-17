"""Shared data structures and exceptions used across extraction tiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..utils.http import KIND_DIRECT

__all__ = [
    "Candidate",
    "DrmProtected",
    "DownloadFailure",
    "Extraction",
    "Outcome",
    "Tier",
    "TransientDownloadError",
    "UrlReport",
]


class Tier(StrEnum):
    YT_DLP = "tier1: yt-dlp"
    STATIC_HTML = "tier2: static html"
    BROWSER = "tier3: headless browser"


class Outcome(StrEnum):
    OK = "ok"
    SKIPPED_DRM = "skipped_drm"
    ROBOTS_BLOCKED = "robots_blocked"
    FAILED = "failed"


@dataclass(slots=True)
class Candidate:
    """A discovered, potentially downloadable media resource."""

    url: str
    kind: str = KIND_DIRECT
    ext: str | None = None
    referer: str | None = None
    cookies: str = ""  # raw "name=value; name2=value2" Cookie header value


@dataclass(slots=True)
class Extraction:
    """Result of running one extraction tier against one page URL."""

    outcome: Outcome
    tier: Tier | None = None
    title: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class UrlReport:
    """Final per-URL result for batch summaries."""

    source_url: str
    outcome: Outcome
    tier_used: Tier | None = None
    output_path: Path | None = None
    detail: str = ""


class DrmProtected(Exception):
    """Raised when media declares DRM; never worked around."""


class DownloadFailure(Exception):
    """Permanent download failure after retries were exhausted."""


class TransientDownloadError(Exception):
    """Transient condition worth retrying (5xx, connection reset)."""
