"""Tests for filename slugification, Windows-safety, and collision handling."""

from __future__ import annotations

from pathlib import Path

from video_scraper.utils.naming import sanitize_stem, slugify, unique_destination


def test_slugify_basic() -> None:
    assert slugify("My Video: Episode 1!") == "My-Video-Episode-1"


def test_slugify_empty_returns_empty() -> None:
    assert slugify("") == ""
    assert slugify(None) == ""
    assert slugify("///") == ""


def test_sanitize_stem_strips_invalid_windows_chars() -> None:
    result = sanitize_stem('a<b>c:d"e/f\\g|h?i*j')
    for ch in '<>:"/\\|?*':
        assert ch not in result


def test_sanitize_stem_guards_windows_reserved_names() -> None:
    assert sanitize_stem("CON").startswith("_")
    assert sanitize_stem("nul").startswith("_")
    assert sanitize_stem("com1").startswith("_")


def test_sanitize_stem_fallback_for_empty() -> None:
    assert sanitize_stem("   ") == "video"


def test_unique_destination_collides_with_suffix(tmp_path: Path) -> None:
    first = unique_destination(tmp_path, "clip", ".mp4")
    first.touch()
    second = unique_destination(tmp_path, "clip", ".mp4")
    assert second.name == "clip-1.mp4"
    second.touch()
    third = unique_destination(tmp_path, "clip", ".mp4")
    assert third.name == "clip-2.mp4"


def test_unique_destination_overwrite_returns_same_path(tmp_path: Path) -> None:
    existing = unique_destination(tmp_path, "clip", ".mp4")
    existing.touch()
    again = unique_destination(tmp_path, "clip", ".mp4", overwrite=True)
    assert again == existing


def test_unique_destination_creates_directory(tmp_path: Path) -> None:
    target_dir = tmp_path / "nested" / "deeper"
    path = unique_destination(target_dir, "x", ".mp4")
    assert target_dir.exists()
    assert path.parent == target_dir
