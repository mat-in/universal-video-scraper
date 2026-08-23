"""HLS/DASH manifest handling: DRM probing and ffmpeg-driven stream download."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..utils.http import KIND_DASH, KIND_HLS, USER_AGENT
from ..utils.logging import get_logger
from ..utils.naming import unique_destination
from .models import Candidate, DownloadFailure, DrmProtected

log = get_logger("manifest")

_FFMPEG_TIMEOUT_S = 3600.0
_MAX_MANIFEST_BYTES = 2_000_000

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


async def probe_and_flag_drm(client: httpx.AsyncClient, candidate: Candidate) -> None:
    """Fetch the manifest and raise DrmProtected if it declares DRM."""
    try:
        response = await client.get(candidate.url)
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
    if candidate.referer:
        headers += f"Referer: {candidate.referer}\r\n"
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


async def download_manifest_stream(
    candidate: Candidate,
    output_dir: Path,
    stem: str,
    *,
    overwrite: bool = False,
    user_agent: str = USER_AGENT,
) -> Path:
    """Download an HLS/DASH stream via system ffmpeg; returns final media path.

    First attempt remuxes into .mp4 (-c copy). On failure, retries once into a
    more permissive .mkv container (handles webm/opus DASH profiles).
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
    raise DownloadFailure("ffmpeg failed: " + " | ".join(errors))
