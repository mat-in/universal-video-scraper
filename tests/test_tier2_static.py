"""Offline tests for Tier 2 (static HTML extractor) using saved fixtures + MockTransport."""

from __future__ import annotations

from pathlib import Path

import httpx

from video_scraper.core.models import Extraction, Outcome
from video_scraper.core.tier2_static import StaticHtmlTier
from video_scraper.utils.http import build_client

FIXTURES = Path(__file__).parent / "fixtures"


def client_for(
    routes: dict[str, tuple[int, str | bytes, str]],
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body, content_type = routes[request.url.path]
        payload = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, content=payload, headers={"content-type": content_type})

    return build_client(transport=httpx.MockTransport(handler))


async def extract_from_fixture(filename: str, path: str = "/page") -> Extraction:
    html = (FIXTURES / filename).read_text(encoding="utf-8")
    async with client_for({path: (200, html, "text/html")}) as client:
        return await StaticHtmlTier().extract(client, f"http://testserver{path}")


async def test_finds_video_source_tag() -> None:
    result = await extract_from_fixture("video_source_tag.html")
    assert result.outcome is Outcome.OK
    assert [c.url for c in result.candidates] == ["http://testserver/media/movie.mp4"]
    assert result.candidates[0].kind == "direct"
    assert result.title == "Video Tag Fixture"


async def test_finds_og_video_meta_with_absolute_url() -> None:
    result = await extract_from_fixture("og_meta.html")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "https://cdn.example.org/videos/og-clip.webm"
    assert result.candidates[0].kind == "direct"
    assert result.title == "The OG Clip"


async def test_resolves_jsonld_content_url_relative() -> None:
    result = await extract_from_fixture("jsonld_videoobject.html")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].url == "http://testserver/videos/jsonld-clip.mp4"
    assert result.candidates[0].kind == "direct"


async def test_classifies_manifest_links_and_ignores_noise() -> None:
    result = await extract_from_fixture("link_farm.html")
    assert result.outcome is Outcome.OK
    by_url = {c.url: c.kind for c in result.candidates}
    assert by_url["http://testserver/downloads/episode.mp4"] == "direct"
    assert by_url["https://mirror.example.net/files/episode.webm"] == "direct"
    assert by_url["http://testserver/static/hls/master.m3u8"] == "hls"
    assert by_url["http://testserver/static/dash/manifest.mpd"] == "dash"
    assert all(".jpg" not in url and ".pdf" not in url for url in by_url)


async def test_no_video_page_fails_cleanly() -> None:
    result = await extract_from_fixture("no_video.html")
    assert result.outcome is Outcome.FAILED
    assert "no embedded media" in result.reason


async def test_blob_urls_are_rejected() -> None:
    result = await extract_from_fixture("blob_only.html")
    assert result.outcome is Outcome.FAILED


async def test_direct_video_content_type_short_circuits() -> None:
    async with client_for({"/clip.bin": (200, "fakebytes", "video/mp4")}) as client:
        result = await StaticHtmlTier().extract(client, "http://testserver/clip.bin")
    assert result.outcome is Outcome.OK
    assert result.candidates[0].kind == "direct"
    assert result.candidates[0].url == "http://testserver/clip.bin"


async def test_http_error_maps_to_failed_outcome() -> None:
    async with client_for({"/missing": (404, "not found", "text/html")}) as client:
        result = await StaticHtmlTier().extract(client, "http://testserver/missing")
    assert result.outcome is Outcome.FAILED
    assert "404" in result.reason
