"""robots.txt checking with per-origin caching."""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

_DISALLOW_ALL_RULES = ["User-agent: *", "Disallow: /"]


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent_token: str = "video-scraper") -> None:
        self._client = client
        self._token = user_agent_token
        self._cache: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = await self._fetch(origin)
        parser = self._cache[origin]
        if parser is None:
            return True
        return parser.can_fetch(self._token, url)

    async def _fetch(self, origin: str) -> RobotFileParser | None:
        try:
            response = await self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return None
        if response.status_code in (401, 403):
            disallow_all = RobotFileParser()
            disallow_all.parse(_DISALLOW_ALL_RULES)
            return disallow_all
        if response.status_code != 200 or not response.text:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser
