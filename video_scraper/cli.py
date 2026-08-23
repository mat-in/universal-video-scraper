"""Typer CLI and batch orchestration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .core.downloader import Downloader
from .core.manifest import find_ffmpeg
from .core.models import DrmProtected, Extraction, Outcome, UrlReport
from .core.tier1_ytdlp import YtdlpTier
from .core.tier2_static import StaticHtmlTier
from .core.tier3_browser import BrowserTier
from .utils.http import DomainRateLimiter, build_client
from .utils.logging import get_logger, setup_logging
from .utils.robots import RobotsCache

app = typer.Typer(add_completion=False)

console = Console()
err_console = Console(stderr=True)
log = get_logger("cli")

_STATUS_LABELS = {
    Outcome.OK: "[green]ok[/green]",
    Outcome.SKIPPED_DRM: "[yellow]skipped: DRM-protected[/yellow]",
    Outcome.ROBOTS_BLOCKED: "[yellow]blocked: robots.txt[/yellow]",
    Outcome.FAILED: "[red]failed[/red]",
}

_MAX_CANDIDATE_ATTEMPTS = 3


def _collect_urls(
    positionals: tuple[str, ...],
    url_option: str | None,
    url_file_option: Path | None,
) -> list[str]:
    urls: list[str] = []
    if url_option:
        urls.append(url_option)
    if url_file_option is not None:
        text = Path(url_file_option).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(stripped)
    urls.extend(positionals)

    ordered: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = url.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _write_sidecar(path: Path, source_url: str, extraction: Extraction) -> Path:
    sidecar = path.parent / f"{path.stem}.json"
    payload = {
        "source_url": source_url,
        "title": extraction.title,
        "extractor_tier_used": extraction.tier.value if extraction.tier else None,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "file": path.name,
        "size_bytes": path.stat().st_size,
    }
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sidecar


class Orchestrator:
    def __init__(
        self,
        client: httpx.AsyncClient,
        downloader: Downloader,
        robots_cache: RobotsCache,
        *,
        ignore_robots: bool,
        browser_timeout_s: float,
    ) -> None:
        self._client = client
        self._downloader = downloader
        self._robots = robots_cache
        self._ignore_robots = ignore_robots
        self._tier1 = YtdlpTier()
        self._tier2 = StaticHtmlTier()
        self._tier3 = BrowserTier(timeout_s=browser_timeout_s)

    async def process(self, url: str) -> UrlReport:
        if not self._ignore_robots and not await self._robots.allowed(url):
            log.info("[robots] %s: disallowed by robots.txt", url)
            return UrlReport(url, Outcome.ROBOTS_BLOCKED, detail="robots.txt disallows this path")
        return await self._process_allowed(url)

    async def _process_allowed(self, url: str) -> UrlReport:
        reasons: list[str] = []
        runners = (
            ("Tier 1 (yt-dlp)", self._run_tier1),
            ("Tier 2 (static html)", self._run_tier2),
            ("Tier 3 (browser)", self._run_tier3),
        )
        for label, runner in runners:
            extraction = await runner(url)
            self._log_tier(label, extraction)
            if extraction.outcome == Outcome.SKIPPED_DRM:
                return UrlReport(
                    url,
                    Outcome.SKIPPED_DRM,
                    tier_used=extraction.tier,
                    detail=extraction.reason,
                )
            if extraction.outcome == Outcome.OK:
                report = await self._try_download_candidates(url, extraction, reasons)
                if report is not None:
                    return report
            elif extraction.reason:
                reasons.append(f"{label}: {extraction.reason}")
        detail = "; ".join(dict.fromkeys(reasons))[:400] or "all tiers exhausted"
        return UrlReport(url, Outcome.FAILED, detail=detail)

    def _log_tier(self, label: str, extraction: Extraction) -> None:
        if extraction.outcome == Outcome.OK:
            suffix = f" ({extraction.title})" if extraction.title else ""
            log.debug("%s: found %d candidate(s)%s", label, len(extraction.candidates), suffix)
        elif extraction.outcome == Outcome.SKIPPED_DRM:
            log.debug("%s: DRM detected - skipping (%s)", label, extraction.reason)
        elif extraction.outcome == Outcome.ROBOTS_BLOCKED:
            log.debug("%s: blocked by robots.txt", label)
        else:
            log.debug("%s: no result%s", label, f": {extraction.reason}" if extraction.reason else "")

    async def _try_download_candidates(
        self,
        url: str,
        extraction: Extraction,
        reasons: list[str],
    ) -> UrlReport | None:
        for candidate in extraction.candidates[:_MAX_CANDIDATE_ATTEMPTS]:
            try:
                path = await self._downloader.download(candidate, extraction.title)
            except Exception as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, DrmProtected):
                    return UrlReport(
                        url,
                        Outcome.SKIPPED_DRM,
                        tier_used=extraction.tier,
                        detail=str(exc),
                    )
                log.debug("candidate failed [%s]: %s", candidate.url, exc)
                reasons.append(f"{type(exc).__name__}: {exc}")
                continue
            sidecar = _write_sidecar(path, url, extraction)
            log.info("saved %s (+ %s)", path.name, sidecar.name)
            return UrlReport(url, Outcome.OK, tier_used=extraction.tier, output_path=path)
        return None

    async def _run_tier1(self, url: str) -> Extraction:
        return await self._tier1.extract(url)

    async def _run_tier2(self, url: str) -> Extraction:
        return await self._tier2.extract(self._client, url)

    async def _run_tier3(self, url: str) -> Extraction:
        return await self._tier3.extract(url)


def _summarize(reports: list[UrlReport]) -> int:
    table = Table(title="video-scraper results", show_lines=False)
    table.add_column("URL", overflow="fold", max_width=60)
    table.add_column("Result")
    table.add_column("Output", overflow="fold")
    table.add_column("Detail", overflow="fold")
    counts = {outcome: 0 for outcome in Outcome}
    for report in reports:
        counts[report.outcome] += 1
        tier = report.tier_used.value if report.tier_used else "-"
        output = report.output_path.name if report.output_path else "-"
        table.add_row(report.source_url, _STATUS_LABELS[report.outcome], output, report.detail or tier)
    console.print(table)
    summary = ", ".join(
        f"{counts[outcome]} {label}"
        for outcome, label in (
            (Outcome.OK, "ok"),
            (Outcome.SKIPPED_DRM, "skipped-DRM"),
            (Outcome.ROBOTS_BLOCKED, "robots-blocked"),
            (Outcome.FAILED, "failed"),
        )
        if counts[outcome]
    ) or "nothing processed"
    console.print(f"[bold]Summary:[/bold] {summary}")
    return 1 if (counts[Outcome.FAILED] or counts[Outcome.ROBOTS_BLOCKED]) else 0


async def _run_batch(urls: list[str], config: dict[str, object]) -> int:
    concurrency = int(config["concurrency"])
    output_dir = Path(str(config["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)

    if find_ffmpeg() is None:
        log.warning("ffmpeg not found on PATH; HLS/DASH downloads will fail")

    limits = httpx.Limits(max_connections=max(4, concurrency * 2))
    async with build_client(limits=limits) as client:
        limiter = DomainRateLimiter(float(config["delay"]))
        robots_cache = RobotsCache(client)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            downloader = Downloader(
                client,
                output_dir,
                overwrite=bool(config["overwrite"]),
                retries=int(config["retries"]),
                limiter=limiter,
                progress=progress,
            )
            orchestrator = Orchestrator(
                client,
                downloader,
                robots_cache,
                ignore_robots=bool(config["ignore_robots"]),
                browser_timeout_s=float(config["browser_timeout"]),
            )
            semaphore = asyncio.Semaphore(concurrency)

            async def bounded(target_url: str) -> UrlReport:
                async with semaphore:
                    return await orchestrator.process(target_url)

            reports = list(await asyncio.gather(*(bounded(u) for u in urls)))
    return _summarize(reports)


def _print_version(value: bool) -> None:
    if value:
        console.print(f"video-scraper {__version__}")
        raise typer.Exit()


@app.command()
def scrape(
    urls: Annotated[
        list[str] | None,
        typer.Argument(help="One or more page URLs to extract video from."),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Single page URL.")
    ] = None,
    url_file: Annotated[
        Path | None,
        typer.Option("--url-file", help="Text file with one URL per line."),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for downloaded files.")
    ] = Path("./downloads"),
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, help="Max parallel URL jobs.")
    ] = 4,
    retries: Annotated[
        int, typer.Option("--retries", min=0, help="Retries per download on transient errors.")
    ] = 3,
    delay: Annotated[
        float, typer.Option("--delay", min=0.0, help="Minimum seconds between requests to the same host.")
    ] = 1.0,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Overwrite existing files instead of renaming (-1, -2...).")
    ] = False,
    ignore_robots: Annotated[
        bool, typer.Option("--ignore-robots", help="Skip robots.txt checks (opt-in only).")
    ] = False,
    browser_timeout: Annotated[
        float, typer.Option("--browser-timeout", min=1.0, help="Headless-browser timeout seconds (Tier 3).")
    ] = 45.0,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Debug logging incl. tier decisions.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Find embedded video on webpages and download it as playable files."""
    setup_logging(verbose)
    collected = _collect_urls(tuple(urls or ()), url, url_file)
    if not collected:
        err_console.print("[red]Error:[/red] no URLs provided (use positional args, --url, or --url-file)")
        raise typer.Exit(code=2)
    invalid = [u for u in collected if not u.lower().startswith(("http://", "https://"))]
    if invalid:
        err_console.print(f"[red]Error:[/red] non-http(s) URLs are not supported: {invalid[:3]}")
        raise typer.Exit(code=2)
    exit_code = asyncio.run(
        _run_batch(
            collected,
            {
                "output_dir": output_dir,
                "concurrency": concurrency,
                "retries": retries,
                "delay": delay,
                "overwrite": overwrite,
                "ignore_robots": ignore_robots,
                "browser_timeout": browser_timeout,
            },
        )
    )
    raise typer.Exit(code=exit_code)


if __name__ == "__main__":
    app()
