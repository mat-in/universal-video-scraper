"""Tier 3 extraction: headless Chromium network observation via Playwright."""

from __future__ import annotations

import asyncio
from typing import Any

from ..utils.http import KIND_DASH, KIND_DIRECT, KIND_HLS, USER_AGENT, classify_url
from .models import Candidate, Extraction, Outcome, Tier

_MEDIA_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl": KIND_HLS,
    "application/x-mpegurl": KIND_HLS,
    "application/dash+xml": KIND_DASH,
}

_SEGMENT_SUFFIXES = (".ts", ".m4s", ".mp4s")

_DOM_COLLECT_JS = """
() => {
  const urls = [];
  const push = (value) => {
    if (typeof value === "string" && value &&
        !value.startsWith("blob:") && !value.startsWith("javascript:")) {
      urls.push(value);
    }
  };
  document.querySelectorAll("video, source, audio").forEach((el) => push(el.getAttribute("src")));
  document
    .querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"]')
    .forEach((el) => push(el.getAttribute("content")));
  return urls;
}
"""


class BrowserTier:
    tier = Tier.BROWSER

    def __init__(self, timeout_s: float = 45.0) -> None:
        self._timeout_s = timeout_s

    async def extract(self, url: str) -> Extraction:
        return await asyncio.to_thread(self._extract_sync, url)

    def _extract_sync(self, url: str) -> Extraction:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            return Extraction(Outcome.FAILED, tier=self.tier, reason=f"playwright unavailable: {exc}")

        observed: list[tuple[str, str]] = []
        title = ""
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context(user_agent=USER_AGENT)
                    page = context.new_page()

                    def on_response(response: Any) -> None:
                        try:
                            content_type = (response.headers or {}).get("content-type", "")
                            content_type = content_type.split(";")[0].strip().lower()
                        except Exception:
                            content_type = ""
                        observed.append((response.url, content_type))

                    page.on("response", on_response)
                    timeout_ms = int(self._timeout_s * 1000)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 20_000))
                    except PlaywrightTimeoutError:
                        pass
                    title = page.title() or ""
                    dom_urls = page.evaluate(_DOM_COLLECT_JS)
                finally:
                    browser.close()
        except Exception as exc:
            return Extraction(
                Outcome.FAILED,
                tier=self.tier,
                reason=f"browser automation failed: {exc}",
            )

        candidates: list[Candidate] = []

        def add(raw: Any, content_type: str = "", *, trusted: bool = False) -> None:
            if not isinstance(raw, str):
                return
            if not raw.lower().startswith(("http://", "https://")):
                return
            kind = _MEDIA_CONTENT_TYPES.get(content_type) or classify_url(raw)
            if kind is None:
                if not trusted:
                    return
                kind = KIND_DIRECT
            candidates.append(Candidate(url=raw, kind=kind, referer=url))

        for raw in dom_urls or []:
            add(raw, trusted=True)
        for raw, content_type in observed:
            add(raw, content_type)

        ordered: list[Candidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.url not in seen:
                seen.add(candidate.url)
                ordered.append(candidate)

        has_manifest = any(c.kind in (KIND_HLS, KIND_DASH) for c in ordered)
        if has_manifest:
            ordered = [
                c for c in ordered
                if c.kind in (KIND_HLS, KIND_DASH)
                or not c.url.lower().split("?")[0].endswith(_SEGMENT_SUFFIXES)
            ]

        if not ordered:
            return Extraction(
                Outcome.FAILED,
                tier=self.tier,
                reason="browser session observed no media requests",
            )
        return Extraction(
            Outcome.OK,
            tier=self.tier,
            title=title or None,
            candidates=ordered[:10],
        )
