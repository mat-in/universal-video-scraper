"""CLI smoke tests: --help, version, URL validation. No network."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from video_scraper.cli import app
from video_scraper.core.models import Outcome, Tier, UrlReport

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--url-file" in result.output
    assert "--concurrency" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "video-scraper" in result.output


def test_no_urls_is_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--output-dir", str(tmp_path)])
    assert result.exit_code == 2


def test_non_http_url_rejected(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ftp://example.org/file", "--output-dir", str(tmp_path)])
    assert result.exit_code == 2


def test_url_file_parsing(tmp_path: Path) -> None:
    url_file = tmp_path / "urls.txt"
    url_file.write_text(
        "https://one.example.org/a\n\n# comment line\nhttps://two.example.org/b\n",
        encoding="utf-8",
    )
    from video_scraper.cli import _collect_urls

    collected = _collect_urls((), None, url_file)
    assert collected == [
        "https://one.example.org/a",
        "https://two.example.org/b",
    ]


def test_summary_exit_code_one_on_failure(capsys: object) -> None:
    from video_scraper.cli import _summarize

    reports = [
        UrlReport("https://ok.example.org/v", Outcome.OK,
                  tier_used=Tier.YT_DLP, output_path=Path("x.mp4")),
        UrlReport("https://bad.example.org/v", Outcome.FAILED, detail="HTTP 404"),
        UrlReport("https://drm.example.org/v", Outcome.SKIPPED_DRM),
    ]
    exit_code = _summarize(reports)
    assert exit_code == 1


def test_summary_exit_code_zero_with_drm_skip_only() -> None:
    from video_scraper.cli import _summarize

    reports = [
        UrlReport("https://ok.example.org/v", Outcome.OK),
        UrlReport("https://drm.example.org/v", Outcome.SKIPPED_DRM),
    ]
    assert _summarize(reports) == 0
