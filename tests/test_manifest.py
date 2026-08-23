"""Manifest DRM-detection unit tests + offline end-to-end HLS download via ffmpeg."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from video_scraper.core.manifest import (
    download_manifest_stream,
    find_ffmpeg,
    m3u8_is_drm,
    mpd_is_drm,
)
from video_scraper.core.models import Candidate

FFMPEG = find_ffmpeg()

HLS_PLAIN = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXTINF:5.0,
seg0.ts
#EXT-X-ENDLIST
"""

HLS_AES128 = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://key.example.org/key.bin",IV=0x1234
#EXTINF:5.0,
seg0.ts
#EXT-X-ENDLIST
"""

HLS_SAMPLE_AES_WIDEVINE = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key65",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:5.0,
seg0.ts
#EXT-X-ENDLIST
"""

HLS_SESSION_KEY_PLAYREADY = """#EXTM3U
#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.microsoft.playready"
#EXTINF:5.0,
seg0.ts
#EXT-X-ENDLIST
"""

MPD_PLAIN = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="1" bandwidth="800000" />
    </AdaptationSet>
  </Period>
</MPD>
"""

MPD_WIDEVINE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>
      <ContentProtection schemeIdUri="urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_m3u8_plain_is_not_drm() -> None:
    assert m3u8_is_drm(HLS_PLAIN) is False


def test_m3u8_aes128_is_not_drm() -> None:
    assert m3u8_is_drm(HLS_AES128) is False


def test_m3u8_sample_aes_is_drm() -> None:
    assert m3u8_is_drm(HLS_SAMPLE_AES_WIDEVINE) is True


def test_m3u8_session_key_playready_is_drm() -> None:
    assert m3u8_is_drm(HLS_SESSION_KEY_PLAYREADY) is True


def test_mpd_plain_is_not_drm() -> None:
    assert mpd_is_drm(MPD_PLAIN) is False


def test_mpd_widevine_is_drm() -> None:
    assert mpd_is_drm(MPD_WIDEVINE) is True


def test_find_ffmpeg_returns_string_or_none() -> None:
    assert FFMPEG is None or Path(FFMPEG).exists()


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_hls_end_to_end_download(tmp_path: Path) -> None:
    """Generate a real 2s HLS stream locally with ffmpeg, then download+mux it."""
    work = tmp_path / "src"
    work.mkdir()
    clip = work / "clip.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=128x96:rate=10",
         "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    playlist = work / "stream.m3u8"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(clip), "-c", "copy", "-f", "hls",
         "-hls_time", "1", "-hls_list_size", "0", str(playlist)],
        check=True,
    )
    assert playlist.exists()

    candidate = Candidate(url=str(playlist), kind="hls")
    output = asyncio.run(
        download_manifest_stream(candidate, tmp_path, "test-stream")
    )
    assert output.exists()
    assert output.stat().st_size > 1000
