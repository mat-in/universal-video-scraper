"""Offline tests for the Downloader using MockTransport (no network, no ffmpeg)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from rich.progress import Progress

from video_scraper.core.downloader import Downloader
from video_scraper.core.models import (
    Candidate,
    DownloadFailure,
    DrmProtected,
    TransientDownloadError,
)
from video_scraper.utils.http import DomainRateLimiter, build_client

DRM_M3U8 = (
    "#EXTM3U\n"
    '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.microsoft.playready"\n'
    "#EXTINF:5.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
)


def make_downloader(client: httpx.AsyncClient, tmp_path: Path) -> Downloader:
    return Downloader(
        client,
        tmp_path,
        overwrite=False,
        retries=2,
        limiter=DomainRateLimiter(0),
        progress=Progress(),
    )


def test_successful_direct_download(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x" * 4096, headers={"content-type": "video/mp4"})

    async def run() -> Path:
        async with build_client(transport=httpx.MockTransport(handler)) as client:
            downloader = make_downloader(client, tmp_path)
            return await downloader.download(Candidate(url="http://t/clip.mp4", kind="direct"), None)

    result = asyncio.run(run())
    assert result.name == "clip.mp4"
    assert result.stat().st_size == 4096
    assert not list(tmp_path.glob("*.part"))
    assert calls["n"] == 1


def test_retry_on_5xx_then_success(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b"data", headers={"content-type": "video/mp4"})

    async def run() -> Path:
        async with build_client(transport=httpx.MockTransport(handler)) as client:
            downloader = make_downloader(client, tmp_path)
            return await downloader.download(Candidate(url="http://t/clip.mp4", kind="direct"), "My Clip")

    result = asyncio.run(run())
    assert calls["n"] == 3
    assert result.exists()
    assert not list(tmp_path.glob("*.part"))


def test_exhausted_retries_raise_transient(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run() -> None:
        async with build_client(transport=httpx.MockTransport(handler)) as client:
            downloader = make_downloader(client, tmp_path)
            await downloader.download(Candidate(url="http://t/clip.mp4", kind="direct"), None)

    with pytest.raises(TransientDownloadError):
        asyncio.run(run())
    assert not list(tmp_path.glob("*"))


def test_empty_body_is_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "video/mp4"})

    async def run() -> None:
        async with build_client(transport=httpx.MockTransport(handler)) as client:
            downloader = make_downloader(client, tmp_path)
            await downloader.download(Candidate(url="http://t/clip.mp4", kind="direct"), None)

    with pytest.raises(DownloadFailure):
        asyncio.run(run())


def test_drm_manifest_raises_before_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=DRM_M3U8.encode(),
            headers={"content-type": "application/vnd.apple.mpegurl"},
        )

    async def run() -> None:
        async with build_client(transport=httpx.MockTransport(handler)) as client:
            downloader = make_downloader(client, tmp_path)
            await downloader.download(Candidate(url="http://t/stream.m3u8", kind="hls"), None)

    with pytest.raises(DrmProtected):
        asyncio.run(run())
