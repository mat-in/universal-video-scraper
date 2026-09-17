"""HLS/DASH manifest handling: DRM probing and ffmpeg-driven stream download."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from ..utils.http import KIND_DASH, KIND_HLS, USER_AGENT
from ..utils.logging import get_logger
from ..utils.naming import unique_destination
from .models import Candidate, DownloadFailure, DrmProtected

log = get_logger("manifest")

_FFMPEG_TIMEOUT_S = 3600.0
_MAX_MANIFEST_BYTES = 2_000_000

# hrefs/segment URIs are re-fetched directly over HTTP, bypassing ffmpeg's HLS
# demuxer entirely. ffmpeg 9.0 rejects many CDNs' segments (no .ts extension,
# application/octet-stream mime) with "not in allowed_segment_extensions" /
# "mime type is not rfc8216 compliant"; that's exactly where the HTTP-aided
# path earns its keep. These cap discovery/download side effects.
_HLS_MAX_SEGMENTS = 512
_HLS_DISCOVER_ROUNDS = 24  # rotating-window playlists (Terabox) need several rounds
_HLS_DISCOVER_STALL = 3  # stop once N consecutive rounds yield nothing new
_SEGMENT_DOWNLOAD_CONCURRENCY = 4

_M3U8_KEY_RE = re.compile(r"#EXT-X-(?:SESSION-)?KEY[^\n]*", re.IGNORECASE)
_M3U8_METHOD_RE = re.compile(r"METHOD=([^,\s]+)", re.IGNORECASE)

_WIDEVINE_SYSTEM_ID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
_PLAYREADY_SYSTEM_ID = "9a04f389-62fc-423c-a58b-bac90d4b7fee"
_FAIRPLAY_SYSTEM_ID = "94ce86fb-07ff-4f43-adb8-93d2dfa968ca"
_KNOWN_DRM_IDS = (_WIDEVINE_SYSTEM_ID, _PLAYREADY_SYSTEM_ID, _FAIRPLAY_SYSTEM_ID)


def find_ffmpeg() -> str | None:
    located = shutil.which("ffmpeg")
    if located:
        return located
    packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        for hit in packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            return str(hit)
    return None


def m3u8_is_drm(manifest_text: str) -> bool:
    """True when the playlist declares anything beyond plain AES-128.

    METHOD=NONE and METHOD=AES-128 are handled natively by ffmpeg. SAMPLE-AES
    or any Widevine/PlayReady/FairPlay keyformat means DRM -> skip.
    """
    for match in _M3U8_KEY_RE.finditer(manifest_text):
        attrs = match.group(0)
        method_match = _M3U8_METHOD_RE.search(attrs)
        method = (method_match.group(1) if method_match else "").upper()
        if method in ("NONE", "AES-128"):
            continue
        return True
    return False


def mpd_is_drm(mpd_text: str) -> bool:
    lowered = mpd_text.lower()
    if "<contentprotection" not in lowered:
        return False
    if any(system_id in lowered for system_id in _KNOWN_DRM_IDS):
        return True
    return any(name in lowered for name in ("widevine", "playready", "fairplay"))


def is_drm_manifest(kind: str, text: str) -> bool:
    if kind == KIND_HLS:
        return m3u8_is_drm(text)
    if kind == KIND_DASH:
        return mpd_is_drm(text)
    return False


def _manifest_referer(candidate: Candidate) -> str | None:
    """Referer the CDN expects for stream manifests: the manifest's own origin.

    Hotlink-protected CDNs (e.g. Terabox/dubox) validate the Referer against
    the media host and reject requests that carry the original share page URL
    (which can be a mirror/redirect domain like terasharefile.com), returning
    JSON errors that ffmpeg chokes on. Drive the request from the media host.
    """
    parsed = urlparse(candidate.url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return candidate.referer


async def probe_and_flag_drm(client: httpx.AsyncClient, candidate: Candidate) -> None:
    """Fetch the manifest and raise DrmProtected if it declares DRM."""
    headers = {"Referer": _manifest_referer(candidate)}
    if candidate.cookies:
        headers["Cookie"] = candidate.cookies
    try:
        response = await client.get(candidate.url, headers=headers)
        response.raise_for_status()
        text = response.text[:_MAX_MANIFEST_BYTES]
    except httpx.HTTPError as exc:
        raise DownloadFailure(f"manifest fetch failed: {exc}") from exc
    effective_kind = candidate.kind
    if effective_kind not in (KIND_HLS, KIND_DASH):
        lowered_head = text[:512].lower()
        effective_kind = KIND_HLS if "#extm3u" in lowered_head else KIND_DASH
    if is_drm_manifest(effective_kind, text):
        raise DrmProtected("manifest declares DRM protection")


def _ffmpeg_command(
    ffmpeg_bin: str,
    candidate: Candidate,
    target: Path,
    user_agent: str,
    container: str,
) -> list[str]:
    headers = f"User-Agent: {user_agent}\r\n"
    referer = _manifest_referer(candidate)
    if referer:
        headers += f"Referer: {referer}\r\n"
    if candidate.cookies:
        headers += f"Cookie: {candidate.cookies}\r\n"
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if urlparse(candidate.url).scheme.lower() in ("http", "https"):
        command += ["-headers", headers]
    command += ["-i", candidate.url, "-map", "0", "-c", "copy"]
    if container == "mp4":
        command += ["-bsf:a", "aac_adtstoasc"]
    command.append(str(target))
    return command


def _candidate_headers(candidate: Candidate) -> dict[str, str]:
    headers = {"Referer": _manifest_referer(candidate)}
    if candidate.cookies:
        headers["Cookie"] = candidate.cookies
    return headers


def _segment_sort_key(url: str) -> int:
    """Stable ordering key for rotating-window playlists.

    Terabox/dubox hand out a ~30s window of segments at a time; each segment URL
    carries its byte range (range=START-END) into the underlying TS file. Sorting
    by that starting offset reconstructs the full video in order even when the
    windows themselves arrive out of sequence.
    """
    match = re.search(r"[?&]range=(\d+)-", url)
    if match:
        return int(match.group(1))
    return -1


def _playlist_segment_urls(playlist_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            urls.append(line)
        else:
            urls.append(urljoin(base_url, line))
    return urls


async def _collect_rotating_segments(
    client: httpx.AsyncClient,
    candidate: Candidate,
    headers: dict[str, str],
    limiter: object | None = None,
) -> list[str]:
    """Fetch the playlist repeatedly and accumulate unique segment URLs.

    Returns segments sorted by their byte-range offset so concatenation yields a
    contiguous stream. Stops early once repeated fetches stop yielding new
    segments (windows rotate, so a few rounds usually cover the whole file).
    """
    seen: dict[int, str] = {}
    stale_rounds = 0
    for _ in range(_HLS_DISCOVER_ROUNDS):
        if limiter is not None:
            await limiter.wait(candidate.url)  # type: ignore[attr-defined]
        try:
            response = await client.get(candidate.url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if seen:
                break
            raise DownloadFailure(f"playlist fetch failed: {exc}") from exc
        text = response.text[:_MAX_MANIFEST_BYTES]
        discovered = 0
        for url in _playlist_segment_urls(text, str(response.url)):
            key = _segment_sort_key(url)
            if key not in seen:
                seen[key] = url
                discovered += 1
        if discovered:
            stale_rounds = 0
        else:
            stale_rounds += 1
            if stale_rounds >= _HLS_DISCOVER_STALL and seen:
                break
        if _HLS_DISCOVER_STALL and len(seen) >= _HLS_MAX_SEGMENTS:
            break
    return [seen[key] for key in sorted(seen)]


async def _download_segments_local(
    client: httpx.AsyncClient,
    segment_urls: list[str],
    headers: dict[str, str],
    workdir: Path,
    limiter: object | None = None,
) -> None:
    """Download every segment into workdir as segNNNN.ts (network only)."""
    if not segment_urls:
        raise DownloadFailure("playlist contained no usable segments")

    semaphore = asyncio.Semaphore(_SEGMENT_DOWNLOAD_CONCURRENCY)

    async def fetch_one(index: int, url: str) -> None:
        async with semaphore:
            if limiter is not None:
                await limiter.wait(url)  # type: ignore[attr-defined]
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise DownloadFailure(f"segment {index} fetch failed: {exc}") from exc
            (workdir / f"seg{index:04d}.ts").write_bytes(response.content)

    await asyncio.gather(*(fetch_one(i, u) for i, u in enumerate(segment_urls)))


async def _join_segments_with_ffmpeg(
    ffmpeg_bin: str,
    workdir: Path,
    target: Path,
    container: str,
    *,
    overwrite: bool,
) -> None:
    """Concatenate local segNNNN.ts files into a single playable media file."""
    concat_txt = workdir / "concat.txt"
    with concat_txt.open("w", encoding="utf-8") as fh:
        for part in sorted(workdir.glob("seg*.ts")):
            fh.write(f"file '{part.as_posix()}'\n")
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "+genpts",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_txt),
        "-map",
        "0",
        "-c",
        "copy",
    ]
    if container == "mp4":
        command += ["-bsf:a", "aac_adtstoasc"]
    command.append(str(target))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=_FFMPEG_TIMEOUT_S)
    if process.returncode != 0:
        tail = (stderr.decode(errors="replace") or "").strip()[-500:]
        raise DownloadFailure(f"ffmpeg join failed: {tail or 'exit code ' + str(process.returncode)}")
    if not target.exists() or target.stat().st_size == 0:
        raise DownloadFailure("ffmpeg produced an empty output file")


async def _download_hls_via_http(
    candidate: Candidate,
    client: httpx.AsyncClient,
    output_dir: Path,
    stem: str,
    *,
    limiter: object | None = None,
    overwrite: bool = False,
    user_agent: str = USER_AGENT,
) -> Path:
    """Download HLS by fetching playlist + segments over HTTP, then joining locally.

    ffmpeg 9.0's HLS demuxer rejects many CDNs outright (segments without a .ts
    extension and/or application/octet-stream mime fail with "not in
    allowed_segment_extensions" / "mime type is not rfc8216 compliant"). Doing
    the HTTP ourselves keeps cookies/referer under our control and feeds ffmpeg
    plain local files it can always handle.
    """
    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin is None:
        raise DownloadFailure("ffmpeg binary not found on PATH; required to join HLS segments")
    headers = {"User-Agent": user_agent}
    headers.update(_candidate_headers(candidate))

    dest_mp4 = unique_destination(output_dir, stem, ".mp4", overwrite=overwrite)
    attempts: list[tuple[str, Path]] = [
        ("mp4", dest_mp4),
        ("mkv", unique_destination(output_dir, stem, ".mkv", overwrite=overwrite)),
    ]
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="hls-") as tmp_raw:
        workdir = Path(tmp_raw)
        try:
            log.debug("collecting HLS segments over HTTP (cookies=%s)", bool(candidate.cookies))
            segment_urls = await _collect_rotating_segments(client, candidate, headers, limiter)
            log.debug("collected %d unique HLS segment(s)", len(segment_urls))
            await _download_segments_local(client, segment_urls, headers, workdir, limiter)
        except DownloadFailure as exc:
            raise DownloadFailure("HTTP-assisted HLS download failed: " + str(exc)) from exc
        for container, target in attempts:
            try:
                await _join_segments_with_ffmpeg(
                    ffmpeg_bin, workdir, target, container, overwrite=overwrite
                )
                return target
            except DownloadFailure as exc:
                errors.append(f"http-{target.name}: {exc}")

    raise DownloadFailure("HTTP-assisted HLS download failed: " + " | ".join(errors))


async def download_manifest_stream(
    candidate: Candidate,
    output_dir: Path,
    stem: str,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: object | None = None,
    overwrite: bool = False,
    user_agent: str = USER_AGENT,
) -> Path:
    """Download an HLS/DASH stream via system ffmpeg; returns final media path.

    First attempt remuxes into .mp4 (-c copy). On failure, retries once into a
    more permissive .mkv container (handles webm/opus DASH profiles). When an
    httpx client is provided, HLS also falls back to fetching playlist+segments
    over HTTP and joining the local files with ffmpeg (handles CDNs ffmpeg's own
    HLS demuxer rejects).
    """
    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin is None:
        raise DownloadFailure(
            "ffmpeg binary not found on PATH; required to download HLS/DASH streams"
        )

    dest_mp4 = unique_destination(output_dir, stem, ".mp4", overwrite=overwrite)
    attempts: list[tuple[str, Path]] = [
        ("mp4", dest_mp4),
        ("mkv", unique_destination(output_dir, stem, ".mkv", overwrite=overwrite)),
    ]
    errors: list[str] = []
    for container, target in attempts:
        command = _ffmpeg_command(ffmpeg_bin, candidate, target, user_agent, container)
        log.debug("running: %s", " ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DownloadFailure(f"failed to launch ffmpeg: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=_FFMPEG_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise DownloadFailure(f"ffmpeg timed out after {int(_FFMPEG_TIMEOUT_S)}s") from None
        if process.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return target
        tail = (stderr.decode(errors="replace") or "").strip()[-500:]
        errors.append(f"{target.name}: {tail or 'exit code ' + str(process.returncode)}")

    if candidate.kind in (KIND_HLS,) and client is not None:
        log.debug("ffmpeg-direct failed; falling back to HTTP-assisted HLS download")
        try:
            return await _download_hls_via_http(
                candidate,
                client,
                output_dir,
                stem,
                limiter=limiter,
                overwrite=overwrite,
                user_agent=user_agent,
            )
        except DownloadFailure as exc:
            errors.append(f"http-fallback: {exc}")

    raise DownloadFailure("ffmpeg failed: " + " | ".join(errors))
