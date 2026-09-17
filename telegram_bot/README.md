# Telegram Bot

A Telegram front-end for the `video-scraper` pipeline. Send a URL that
contains an embedded video and the bot extracts, downloads, and replies
with the playable file — same engine (yt-dlp → static HTML → headless
browser) and the same rules (no DRM bypass, robots.txt respected) as the
CLI.

## How hosting works

Telegram does **not** run your code. The bot token you get from
[@BotFather](https://t.me/botfather) is attached to your Telegram
account, but the bot process runs on your own machine (or any server
you control). Telegram's servers only relay messages to and from it.

## Setup

1. Python 3.11+ and [ffmpeg](https://ffmpeg.org) on PATH
   (`winget install Gyan.FFmpeg`).

2. Install the base project plus bot extras:

   ```bash
   pip install -e ".[dev]"
   pip install -r telegram_bot/requirements.txt
   ```

3. Headless browser for Tier 3 fallback (optional but recommended):

   ```bash
   playwright install chromium
   ```

4. Get a bot token:

   - Open Telegram → search `@BotFather`
   - Run `/newbot`, pick a name and username
   - Copy the `123456:ABC-...` token
   - Read your numeric account ID (e.g. from
     [@userinfobot](https://t.me/userinfobot)) to restrict access

5. Run:

   ```bash
   set TELEGRAM_BOT_TOKEN=123456:ABC-...
   set JARVIS_BOT_ALLOWED_IDS=123456789
   python -m telegram_bot.bot
   ```

   (PowerShell) or export equivalents under Linux/WSL.

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Token from @BotFather |
| `JARVIS_BOT_ALLOWED_IDS` | *(empty)* | Comma-separated Telegram user IDs; empty = allow anyone |
| `JARVIS_BOT_DOWNLOAD_DIR` | `./telegram_downloads` | Where files land |
| `JARVIS_BOT_CONCURRENCY` | `2` | Max parallel downloads across all users |

## Commands

- `/start` — help text
- `/status` — show downloader state and download directory

## Notes

- Files over **50 MB** (Telegram's standard Bot API upload cap) are not
  uploaded; the on-server path is reported instead.
- DRM-protected and robots-blocked sources are reported and skipped,
  exactly like the CLI.
- For a longer-running deployment, consider a VPS and running the bot
  under `systemd` (Linux) or Task Scheduler (Windows).