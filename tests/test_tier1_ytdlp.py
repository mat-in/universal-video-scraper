"""Offline tests for Tier 1 (yt-dlp wrapper) using a fake YoutubeDL."""

from __future__ import annotations

from typing import Any

import pytest
import yt_dlp

from video_scraper.core import tier1_ytdlp as mod
from video_scraper.core.models import Outcome


class FakeYoutubeDL:
    info: dict[str, Any] | None = None
    error: str | None = None

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        assert download is False
        if type(self).error:
            raise yt_dlp.utils.DownloadError(type(self).error)
        assert type(self).info is not None
        return type(self).info


@pytest.fixture()
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    FakeYoutubeDL.info = None
    FakeYoutubeDL.error = None
    return FakeYoutubeDL


def test_prefers_tallest_mp4_over_manifest(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {
        "title": "Sample",
        "formats": [
            {"url": "https://x/720.mp4", "ext": "mp4", "height": 720, "vcodec": "h264", "acodec": "aac"},
            {"url": "https://x/1080.m3u8", "ext": "m3u8", "height": 1080, "protocol": "m3u8_native", "vcodec": "h264", "acodec": None},
            {"url": "https://x/1080.mp4", "ext": "mp4", "height": 1080, "protocol": "https", "vcodec": "h264", "acodec": None},
        ],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://x/1080.mp4"


def test_falls_back_to_manifest_when_no_progressive(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {
        "title": "Adaptive",
        "formats": [
            {"url": "https://x/master.m3u8", "ext": "m3u8", "protocol": "m3u8_native",
             "height": 1080, "vcodec": "h264", "acodec": None},
        ],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].kind == "hls"


def test_single_url_format_without_formats_list(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {"title": "Direct", "url": "https://x/file.mp4", "ext": "mp4"}
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://x/file.mp4"
    assert result.title == "Direct"


def test_all_formats_flagged_drm_is_skipped(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {
        "title": "Locked",
        "formats": [
            {"url": "https://x/a", "has_drm": True},
            {"url": "https://x/b", "has_drm": True},
        ],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.SKIPPED_DRM


def test_mixed_drm_flags_use_clean_format(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {
        "title": "Mixed",
        "formats": [
            {"url": "https://x/drm.mpd", "has_drm": True, "vcodec": "h264", "acodec": "aac"},
            {"url": "https://x/clean.mp4", "ext": "mp4", "height": 480, "vcodec": "h264", "acodec": "aac"},
        ],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://x/clean.mp4"


def test_download_error_unsupported_is_failed_not_drm(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.error = "ERROR: Unsupported URL: https://example.org/nothing"
    result = mod.YtdlpTier().extract_sync("https://example.org/nothing")
    assert result.outcome is Outcome.FAILED


def test_download_error_mentioning_widevine_is_drm_skip(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.error = "ERROR: This video is protected by Widevine DRM and cannot be extracted"
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.SKIPPED_DRM


def test_playlist_takes_first_entry(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {
        "_type": "playlist",
        "entries": [{"title": "Entry One", "url": "https://x/e1.mp4", "ext": "mp4"}],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/list")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://x/e1.mp4"


def test_formats_without_codec_metadata_pick_tallest_mp4(fake_ydl: type[FakeYoutubeDL]) -> None:
    """archive.org-style formats omit vcodec/acodec; must still prefer video MP4s over audio."""
    fake_ydl.info = {
        "title": "Duck and Cover",
        "formats": [
            {"url": "https://x/a.mp3", "ext": "mp3", "filesize": 4438016},
            {"url": "https://x/240.mp4", "ext": "mp4", "height": 240},
            {"url": "https://x/480.mp4", "ext": "mp4", "height": 480},
            {"url": "https://x/a.ogv", "ext": "ogv", "height": 300},
        ],
    }
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://x/480.mp4"


def test_no_formats_at_all_is_failed(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.info = {"title": "Empty"}
    result = mod.YtdlpTier().extract_sync("https://example.org/v")
    assert result.outcome is Outcome.FAILED
