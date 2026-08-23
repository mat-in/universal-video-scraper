"""Tests for robots.txt handling using MockTransport (no network)."""

from __future__ import annotations

import httpx

from video_scraper.utils.http import build_client
from video_scraper.utils.robots import RobotsCache

ROBOTS_TEXT = """User-agent: *
Disallow: /private/
Allow: /public/

Sitemap: https://example.org/sitemap.xml
"""


def client_for_robots(body: str | None, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                status,
                content=(body or "").encode(),
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(200)

    return build_client(transport=httpx.MockTransport(handler))


async def test_disallowed_path_is_blocked() -> None:
    async with client_for_robots(ROBOTS_TEXT) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.org/private/secret.mp4") is False
        assert await cache.allowed("https://example.org/public/ok.mp4") is True


async def test_missing_robots_allows_everything() -> None:
    async with client_for_robots("", status=404) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.org/anything/at/all") is True


async def test_403_robots_disallows_all() -> None:
    async with client_for_robots("", status=403) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.org/") is False


async def test_network_error_on_robots_allows(tmp_path: object) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with build_client(transport=httpx.MockTransport(failing_handler)) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://unreachable.example.org/page") is True


async def test_robots_fetched_once_per_origin() -> None:
    calls = {"robots": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(200, content=ROBOTS_TEXT.encode())
        return httpx.Response(200)

    async with build_client(transport=httpx.MockTransport(counting_handler)) as client:
        cache = RobotsCache(client)
        await cache.allowed("https://example.org/a")
        await cache.allowed("https://example.org/b")
        assert calls["robots"] == 1
