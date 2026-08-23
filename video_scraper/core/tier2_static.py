"""Tier 2 extraction: static HTML parsing (video tags, meta tags, JSON-LD, direct links)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..utils.http import (
    KIND_DASH,
    KIND_DIRECT,
    KIND_HLS,
    classify_content_type,
    classify_url,
)
from .models import Candidate, Extraction, Outcome, Tier

_META_TAGS = (
    "og:video:url",
    "og:video:secure_url",
    "og:video",
    "twitter:player:stream",
)


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _walk_jsonld(node: Any) -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        node_type = node.get("@type", "")
        types = {node_type} if isinstance(node_type, str) else set(node_type or [])
        if any(str(t).lower() == "videoobject" for t in types):
            content_url = node.get("contentUrl")
            if isinstance(content_url, str) and content_url:
                yield content_url
        graph = node.get("@graph")
        if graph is not None:
            yield from _walk_jsonld(graph)


class StaticHtmlTier:
    tier = Tier.STATIC_HTML

    def __init__(self, max_candidates: int = 8) -> None:
        self._max_candidates = max_candidates

    async def extract(self, client: httpx.AsyncClient, url: str) -> Extraction:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return Extraction(Outcome.FAILED, tier=self.tier, reason=f"HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return Extraction(Outcome.FAILED, tier=self.tier, reason=f"fetch failed: {exc}")

        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
        base_type = content_type.split(";")[0].strip().lower()

        kind = classify_content_type(base_type)
        if kind in (KIND_HLS, KIND_DASH):
            return Extraction(
                Outcome.OK,
                tier=self.tier,
                candidates=[Candidate(url=final_url, kind=kind, referer=url)],
            )
        if base_type.startswith(("video/", "audio/")) and "mpegurl" not in base_type and "dash" not in base_type:
            return Extraction(
                Outcome.OK,
                tier=self.tier,
                candidates=[Candidate(url=final_url, kind=KIND_DIRECT, referer=url)],
            )
        if "html" not in base_type and base_type != "":
            return Extraction(
                Outcome.FAILED,
                tier=self.tier,
                reason=f"unhandled content-type {base_type!r}",
            )

        soup = BeautifulSoup(response.text, "lxml")
        candidates = self._collect(soup, final_url)
        title = self._title(soup)
        if not candidates:
            return Extraction(
                Outcome.FAILED,
                tier=self.tier,
                title=title,
                reason="no embedded media found in static HTML",
            )
        return Extraction(Outcome.OK, tier=self.tier, title=title, candidates=candidates)

    def _collect(self, soup: BeautifulSoup, base_url: str) -> list[Candidate]:
        found: list[Candidate] = []

        def add(raw: str | None, *, trusted: bool = False) -> None:
            if not raw:
                return
            absolute = urljoin(base_url, raw.strip())
            if not absolute.lower().startswith(("http://", "https://")):
                return
            kind = classify_url(absolute)
            if kind is None:
                if not trusted:
                    return
                kind = KIND_DIRECT
            found.append(Candidate(url=absolute, kind=kind, referer=base_url))

        for tag in soup.find_all(["video", "source", "audio"]):
            add(tag.get("src") or tag.get("data-src"), trusted=True)

        for name in _META_TAGS:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag is not None:
                add(tag.get("content"), trusted=True)

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            data = _safe_json_loads(script.string or script.get_text())
            for content_url in _walk_jsonld(data):
                add(content_url, trusted=True)

        for anchor in soup.find_all("a", href=True):
            add(anchor["href"])

        ordered: list[Candidate] = []
        seen: set[str] = set()
        for candidate in found:
            if candidate.url not in seen:
                seen.add(candidate.url)
                ordered.append(candidate)
        return ordered[: self._max_candidates]

    def _title(self, soup: BeautifulSoup) -> str | None:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og is not None and og.get("content"):
            return str(og["content"]).strip()
        tag = soup.find("title")
        if tag is not None and tag.string:
            return tag.string.strip() or None
        return None
