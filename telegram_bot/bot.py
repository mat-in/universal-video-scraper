"""Telegram bot exposing the video_scraper pipeline.

Runs the same Orchestrator as the CLI, but driven from Telegram
messages: send a URL, get the playable file back.

Usage:
    TELEGRAM_BOT_TOKEN=... python -m telegram_bot.bot

The bot runs on YOUR machine; Telegram only relays messages. The
bot token is obtained from @BotFather on Telegram.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import httpx
from rich.console import Console as RichConsole
from rich.progress import Progress
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from video_scraper.cli import Orchestrator
from video_scraper.core.downloader import Downloader
from video_scraper.core.manifest import find_ffmpeg
from video_scraper.utils.http import DomainRateLimiter, build_client
from video_scraper.utils.logging import get_logger, setup_logging
from video_scraper.utils.robots import RobotsCache

log = get_logger("telegram_bot")

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".ogv", ".3gp", ".ts"}
MAX_SEND_BYTES = 50 * 1024 * 1024  # Telegram bot upload cap (standard Bot API)


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _allowed_ids() -> set[int]:
    raw = os.environ.get("JARVIS_BOT_ALLOWED_IDS", "").strip()
    if not raw:
        return set()  # empty = allow everyone
    return {int(part) for part in raw.split(",") if part.strip().isdigit()}


def _download_dir() -> Path:
    return Path(os.environ.get("JARVIS_BOT_DOWNLOAD_DIR", "./telegram_downloads"))


def _concurrency() -> int:
    return int(os.environ.get("JARVIS_BOT_CONCURRENCY", "2"))


class ScraperRunner:
    """Owns the httpx client, orchestrator, and a global concurrency semaphore."""

    def __init__(self, download_dir: Path, concurrency: int) -> None:
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._limiter = DomainRateLimiter(1.0)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._quiet = RichConsole(file=StringIO(), width=200)
        self._client: httpx.AsyncClient | None = None
        self._progress: Progress | None = None
        self._orchestrator: Orchestrator | None = None

    async def __aenter__(self) -> ScraperRunner:
        self._client = build_client(limits=httpx.Limits(max_connections=16))
        self._progress = Progress(console=self._quiet)
        downloader = Downloader(
            self._client,
            self.download_dir,
            overwrite=False,
            retries=3,
            limiter=self._limiter,
            progress=self._progress,
        )
        robots_cache = RobotsCache(self._client)
        self._orchestrator = Orchestrator(
            self._client,
            downloader,
            robots_cache,
            ignore_robots=False,
            browser_timeout_s=45.0,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._progress is not None:
            self._progress.stop()

    async def scrape_one(self, url: str) -> dict[str, object]:
        """Run the pipeline for a single URL, bounded by the global semaphore."""
        async with self._semaphore:
            report = await self._orchestrator.process(url)
        result: dict[str, object] = {
            "url": url,
            "outcome": report.outcome.value,
            "detail": report.detail,
            "tier": report.tier_used.value if report.tier_used else None,
            "path": report.output_path,
            "sidecar": None,
        }
        if report.output_path is not None:
            sidecar = _build_sidecar(report, url)
            _write_sidecar_json(report.output_path, sidecar)
            result["sidecar"] = sidecar
        return result


def _build_sidecar(report, url: str) -> dict[str, object]:
    path = report.output_path
    return {
        "source_url": url,
        "title": path.stem if path else None,
        "extractor_tier_used": report.tier_used.value if report.tier_used else None,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "file": path.name if path else None,
        "size_bytes": path.stat().st_size if path else 0,
    }


def _write_sidecar_json(path: Path, sidecar: dict[str, object]) -> None:
    (path.parent / f"{path.stem}.json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )


class Bot:
    def __init__(self, token: str, allowed: set[int]) -> None:
        self.token = token
        self.allowed = allowed
        self.runner: ScraperRunner | None = None

    def _check_user(self, update: Update) -> bool:
        return not self.allowed or (
            update.effective_user is not None and update.effective_user.id in self.allowed
        )

    async def _deny(self, update: Update) -> None:
        msg = update.effective_message
        if msg is not None:
            await msg.reply_text("You are not authorised to use this bot.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not self._check_user(update):
            await self._deny(update)
            return
        await msg.reply_text(
            "Video Scraper Bot\n\n"
            "Send me a URL to a page containing a video and I'll extract the "
            "playable file for you.\n\n"
            "Commands:\n"
            "/start - this help\n"
            "/status - check downloader status\n\n"
            "Rules: no DRM bypass, robots.txt respected.\n"
            "Limit: 50 MB per file (Telegram Bot API cap)."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not self._check_user(update):
            return
        download_dir = self.runner.download_dir if self.runner else _download_dir()
        await msg.reply_text(
            f"Runner alive: {self.runner is not None}\n"
            f"Download dir: {download_dir.resolve()}"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or msg.text is None or not self._check_user(update):
            await self._deny(update)
            return

        urls = [m.strip().rstrip(".,)") for m in _URL_RE.findall(msg.text)]
        urls = [u for u in dict.fromkeys(urls) if u.startswith(("http://", "https://"))]
        if not urls:
            await msg.reply_text("No URL found. Send me a link to a page with a video.")
            return

        await msg.reply_text(f"I found {len(urls)} URL(s). Starting extraction…")
        for i, url in enumerate(urls, 1):
            await self._process_url(msg, url, i, len(urls))

    async def _process_url(self, origin: Message, url: str, index: int, total: int) -> None:
        status = await origin.reply_text(f"[{index}/{total}] Fetching: {url}")
        try:
            result = await self.runner.scrape_one(url)
            outcome = str(result["outcome"])

            if outcome == "ok":
                await status.edit_text(
                    f"[{index}/{total}] ✓ Downloaded, sending… (may take a moment)"
                )
                await origin.reply_chat_action(ChatAction.UPLOAD_VIDEO)
                await self._send_result(origin, result)
            elif outcome == "skipped_drm":
                await status.edit_text(f"[{index}/{total}] ⛔ Skipped: DRM-protected stream.")
            elif outcome == "robots_blocked":
                await status.edit_text(f"[{index}/{total}] 🚫 Blocked by robots.txt.")
            else:
                await status.edit_text(
                    f"[{index}/{total}] ❌ Failed: {str(result['detail'])[:200]}"
                )
        except Exception as exc:
            log.exception("failed processing %s", url)
            try:
                await status.edit_text(f"[{index}/{total}] ❌ Error: {exc}")
            except Exception:
                pass

    async def _send_result(self, origin: Message, result: dict[str, object]) -> None:
        path = result["path"]
        if not isinstance(path, Path) or not path.exists():
            await origin.reply_text("File was downloaded but could not be found on disk.")
            return

        size = path.stat().st_size
        if size > MAX_SEND_BYTES:
            await origin.reply_text(
                f"File is {size / 1e6:.1f} MB, over Telegram's 50 MB upload cap. "
                f"Path on server: `{path}`"
            )
            return

        sidecar = result.get("sidecar")
        title = sidecar["title"] if isinstance(sidecar, dict) and sidecar.get("title") else path.stem
        caption = f"🎬 {title}\nSource: {result['url']}"
        with path.open("rb") as fh:
            if path.suffix.lower() in _VIDEO_EXTS:
                await origin.reply_video(fh, caption=caption, supports_streaming=True)
            else:
                await origin.reply_document(fh, caption=caption)


def main() -> None:
    setup_logging(True)
    token = _token()
    allowed = _allowed_ids()
    download_dir = _download_dir()
    concurrency = _concurrency()

    if find_ffmpeg() is None:
        log.warning("ffmpeg not found on PATH; HLS/DASH downloads will fail")

    bot = Bot(token, allowed)
    app = Application.builder().token(token).build()

    async def start_runner(application: Application) -> None:
        runner = ScraperRunner(download_dir, concurrency)
        bot.runner = await runner.__aenter__()
        application.bot_data["runner"] = runner

    async def stop_runner(application: Application) -> None:
        runner = application.bot_data.get("runner")
        if runner is not None:
            await runner.__aexit__()

    app.post_init = start_runner
    app.post_shutdown = stop_runner

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("status", bot.status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    print(f"Download dir: {download_dir.resolve()}")
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
