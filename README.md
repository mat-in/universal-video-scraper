<div align="center">

# video-scraper

**Universal video extractor CLI** — give it webpage URLs, get back playable `.mp4` files.

[![CI](https://github.com/mat-in/universal-video-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/mat-in/universal-video-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Lint: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)
[![Tested with pytest](https://img.shields.io/badge/tested%20with-pytest-0a9edc.svg)](https://pytest.org)

</div>

Works across arbitrary, previously-unseen websites via a tiered extraction strategy —
not a hardcoded site list. Every download lands as a standard playable file plus a
JSON metadata sidecar.

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Install](#install)
- [Usage](#usage)
- [Output & exit codes](#output--exit-codes)
- [Project layout](#project-layout)
- [Legal & ethical scope](#legal--ethical-scope)
- [Tests](#tests)
- [Implementation notes](#implementation-notes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## How it works

For each URL, progressively more expensive methods are tried until one succeeds:

| Tier | Method | Covers |
|------|--------|--------|
| 1 | [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) library API | ~1800 sites with dedicated extractors |
| 2 | Static HTML parse (`httpx` + `BeautifulSoup`/`lxml`) | `<video>/<source>` tags, `og:video*` meta, JSON-LD `VideoObject.contentUrl`, direct media/manifest links |
| 3 | Headless Chromium ([Playwright](https://playwright.dev)) | JS-only players: observes network responses for video content types |
| — | Manifest resolution | HLS `.m3u8` / DASH `.mpd` downloaded and remuxed by system `ffmpeg` |

Every tier decision is visible in logs with `-v/--verbose`. One bad URL never aborts a
batch; results are summarized at the end (ok / skipped-DRM / robots-blocked / failed).

## Features

- **Universal** — three escalating strategies cover static pages, dedicated sites,
  and JS-only players; no per-site plugins to maintain.
- **Batch-first** — mix positional URLs, `--url` flags, and `--url-file`; inputs are
  deduplicated and processed with bounded concurrency.
- **Polite by default** — per-host rate limiting, exponential-backoff retries, honest
  User-Agent, `robots.txt` respected.
- **Collision-safe naming** — Windows-aware slugs, numeric suffixes on collisions,
  atomic `.part` temp files.
- **Structured output** — every download gets a JSON sidecar recording source, tier
  used, and timestamp.
- **DRM-honest** — protected streams are detected, reported, and skipped; never attacked.

## Install

Requirements: **Python 3.11+** and [ffmpeg](https://ffmpeg.org) on PATH.

```bash
# 1. ffmpeg (Windows/winget shown; or choco, brew, apt...)
winget install Gyan.FFmpeg

# 2. package + dev tools
pip install -e ".[dev]"

# 3. Chromium for Tier 3 fallback (~150 MB, one-time)
playwright install chromium
```

Verify:

```bash
video-scraper --version
```

## Usage

```bash
video-scraper https://example.org/some-page            # single page
video-scraper --url https://a.example/x --url https://b.example/y
video-scraper --url-file urls.txt                      # one URL per line (# comments ok)
video-scraper URL1 URL2 --concurrency 8 --delay 0.5    # batch tuning
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--output-dir` | `./downloads` | Where files land |
| `--concurrency` | `4` | Max parallel URL jobs |
| `--retries` | `3` | Retries w/ exponential backoff on transient errors |
| `--delay` | `1.0` | Min seconds between requests to the same host |
| `--overwrite` | off | Replace existing files instead of renaming `-1`, `-2`… |
| `--ignore-robots` | off | Skip robots.txt checks (**explicit opt-in only**) |
| `--browser-timeout` | `45` | Tier 3 page-load budget in seconds |
| `-v` / `--verbose` | off | Debug logging incl. tier-by-tier decisions |
| `--version` | — | Print version and exit |

URLs may also be passed positionally; all sources are merged and deduplicated.

## Output & exit codes

Each success produces:

- `<slugified-title>.mp4` — collision-safe suffixes (`-1`, `-2`…) unless `--overwrite`
- `<name>.json` — sidecar with `source_url`, `title`, `extractor_tier_used`,
  `downloaded_at`, `file`, `size_bytes`

| Exit code | Meaning |
|-----------|---------|
| `0` | All URLs downloaded |
| `1` | One or more URLs failed or were blocked by robots.txt (DRM skips don't fail the run) |
| `2` | Bad invocation — no URLs, or non-http(s) input |

## Project layout

```
video_scraper/
├── cli.py              # Typer CLI + batch orchestration
├── core/
│   ├── downloader.py   # HTTP downloads w/ retries, .part files
│   ├── manifest.py     # HLS/DASH resolution via ffmpeg
│   ├── models.py       # Shared dataclasses/enums
│   ├── tier1_ytdlp.py  # yt-dlp library tier
│   ├── tier2_static.py # Static HTML parsing tier
│   └── tier3_browser.py# Playwright network-sniffing tier
└── utils/
    ├── http.py         # Client builder + per-host rate limiter
    ├── logging.py      # Logging setup
    ├── naming.py       # Slugification + collision handling
    └── robots.py       # robots.txt cache
```

## Legal & ethical scope

This tool is for content **you own**, have **explicit permission to download**, or that
is public-domain/CC-licensed. You are responsible for the ToS and copyright rules of
whatever site you point it at. Hard scope limits, by design:

- **No DRM circumvention.** Widevine/PlayReady/FairPlay-protected streams are detected
  and reported as `skipped: DRM-protected`. They are never decrypted or attacked.
  (Plain HLS AES-128 is standard encryption with keys published in the manifest; it is
  handled normally.)
- **No auth/paywall bypass.** Only anonymous requests; no credentials, cookies, or
  login automation.
- **No CAPTCHA solving or anti-bot evasion.** Honest User-Agent and rate limiting only.
- **robots.txt respected by default** (`--ignore-robots` exists but is an explicit,
  conscious opt-in).

## Tests

```bash
pytest        # fully offline: fixtures + MockTransport + locally generated HLS
ruff check .
```

The manifest test generates a real 2-second HLS stream with your local ffmpeg and
downloads it back through the resolver — no network needed.

## Implementation notes

Documented decisions, so future contributors don't have to reverse-engineer them:

- **Format preference (Tier 1):** progressive MP4 files are preferred over adaptive
  manifests for reliability (single playable file, no muxing); within those, highest
  resolution wins. Missing codec metadata is treated optimistically since some
  extractors (e.g. archive.org) omit it.
- **HLS/DASH download strategy:** `ffmpeg` pulls the manifest and writes the output
  directly (`-c copy`, mp4 first, mkv fallback). Hand-rolling segment logic would only
  re-create ffmpeg's battle-tested key handling, variant selection, and muxer.
- **Dependencies are floor-pinned** (`>=`) rather than hard-pinned so bug-fix releases
  of yt-dlp (whose extractor freshness matters daily) flow in freely.
- **Extra module:** `core/models.py` holds shared dataclasses/enums used by all tiers;
  everything else matches the architecture sketch this project was built from.
- **Windows-aware naming:** slugs strip illegal characters and reserved device names;
  collisions get numeric suffixes; downloads go through `.part` temp files.

## Troubleshooting

<details>
<summary><b>ffmpeg not found</b></summary>

`HLS/DASH downloads will fail` warning at startup means ffmpeg isn't on PATH. Install it
(`winget install Gyan.FFmpeg`, `brew install ffmpeg`, `sudo apt install ffmpeg`) and open
a fresh terminal.
</details>

<details>
<summary><b>Every URL fails on a JS-heavy site</b></summary>

Tier 3 needs Chromium: run `playwright install chromium` once. Check `-v` logs to see
which tiers ran and why each was rejected.
</details>

<details>
<summary><b>Downloads land as `.mkv` instead of `.mp4`</b></summary>

ffmpeg fell back because the stream couldn't be remuxed to mp4 without re-encoding —
the file is still fully playable in modern players.
</details>

## License

[MIT](LICENSE) © 2026 Abdul Mateen R I
