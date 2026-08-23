"""Async media downloader: retries, rate limiting, progress reporting, manifest routing."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from rich.progress import Progress
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..utils import naming
from ..utils.http import (
    KIND_DASH,
    KIND_HLS,
    USER_AGENT,
    DomainRateLimiter,
    classify_content_type,
    extension_from_content_type,
)
from ..utils.logging import get_logger
from . import manifest
from .models import Candidate, DownloadFailure, TransientDownloadError

log = get_logger("downloader")

_CHUNK_SIZE = 1 << 16


def _stem_for(candidate: Candidate, title: str | None) -> str:
    slug = naming.slugify(title)
    if not slug:
        path = unquote(urlparse(candidate.url).path)
        slug = naming.slugify(Path(path).stem)
    return naming.sanitize_stem(slug)


def _normalized_ext(ext: str | None) -> str:
    if not ext:
        return ".mp4"
    cleaned = "".join(ch for ch in ext.lower() if ch.isalnum())
    return f".{cleaned}" if cleaned else ".mp4"


class Downloader:
    def __init__(
        self,
        client: httpx.AsyncClient,
        output_dir: Path,
        *,
        overwrite: bool = False,
        retries: int = 3,
        limiter: DomainRateLimiter,
        progress: Progress,
    ) -> None:
        self._client = client
        self._output_dir = output_dir
        self._overwrite = overwrite
        self._retries = retries
        self._limiter = limiter
        self._progress = progress

    async def download(self, candidate: Candidate, title: str | None) -> Path:
        """Download one candidate; returns the final media file path."""
        if candidate.kind in (KIND_HLS, KIND_DASH):
            return await self._download_manifest(candidate, title)
        return await self._download_direct(candidate, title)

    async def _download_manifest(self, candidate: Candidate, title: str | None) -> Path:
        await self._limiter.wait(candidate.url)
        await manifest.probe_and_flag_drm(self._client, candidate)
        return await manifest.download_manifest_stream(
            candidate,
            self._output_dir,
            _stem_for(candidate, title),
            overwrite=self._overwrite,
            user_agent=USER_AGENT,
        )

    async def _download_direct(self, candidate: Candidate, title: str | None) -> Path:
        await self._limiter.wait(candidate.url)
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max(1, self._retries + 1)),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(
                (TransientDownloadError, httpx.TransportError, httpx.TimeoutException)
            ),
            reraise=True,
        ):
            with attempt:
                return await self._attempt_stream(candidate, title)
        raise DownloadFailure("unreachable")  # pragma: no cover - tenacity always raises/returns

    async def _attempt_stream(self, candidate: Candidate, title: str | None) -> Path:
        headers = {"Referer": candidate.referer} if candidate.referer else {}
        request = self._client.build_request("GET", candidate.url, headers=headers)
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code >= 500 or response.status_code == 429:
                raise TransientDownloadError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                raise DownloadFailure(f"HTTP {response.status_code} for {candidate.url}")

            content_type = response.headers.get("content-type", "")
            kind = classify_content_type(content_type)
            if kind in (KIND_HLS, KIND_DASH):
                rerouted = Candidate(url=str(response.url), kind=kind, referer=candidate.referer)
                return await self._download_manifest(rerouted, title)

            ext = _normalized_ext(
                candidate.ext
                or classify_url_ext(candidate.url)
                or extension_from_content_type(content_type)
                or ".mp4"
            )
            dest = naming.unique_destination(
                self._output_dir,
                _stem_for(candidate, title),
                ext,
                overwrite=self._overwrite,
            )
            temp = dest.parent / (dest.name + ".part")
            total = int(response.headers.get("content-length") or 0)
            task_id = self._progress.add_task(dest.name, total=total or None)
            received = 0
            try:
                with temp.open("wb") as fh:
                    async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                        fh.write(chunk)
                        received += len(chunk)
                        self._progress.advance(task_id, len(chunk))
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
            finally:
                self._progress.remove_task(task_id)
            if received == 0:
                temp.unlink(missing_ok=True)
                raise DownloadFailure(f"empty response body from {candidate.url}")
            temp.replace(dest)
            log.debug("downloaded %s (%d bytes)", dest.name, received)
            return dest
        finally:
            await response.aclose()


def classify_url_ext(url: str) -> str | None:
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    if dot != -1 and len(path) - dot <= 6:
        candidate_ext = path[dot:]
        if candidate_ext.lstrip(".") in {
            "mp4", "m4v", "webm", "mkv", "mov", "avi", "ogv", "3gp",
            "ts", "mpg", "mpeg", "wmv", "flv",
            "m4a", "mp3", "aac", "ogg", "opus", "wav", "flac",
        }:
            return candidate_ext
    return None
