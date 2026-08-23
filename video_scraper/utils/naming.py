"""Filename slugification and collision-safe destination resolution (Windows-aware)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_SLUG_STRIP = re.compile(r"[^\w\s.-]")


def sanitize_stem(stem: str) -> str:
    stem = unicodedata.normalize("NFKC", stem)
    stem = _INVALID_CHARS.sub(" ", stem)
    stem = _WHITESPACE.sub("-", stem.strip()).strip("-. ")
    if not stem:
        stem = "video"
    if stem.split(".")[0].upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    return stem[:100].strip(". ") or "video"


def slugify(value: str | None, max_length: int = 100) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = _SLUG_STRIP.sub("", value)
    value = _WHITESPACE.sub("-", value.strip()).strip("-.")
    return value[:max_length].strip("-.")


def unique_destination(
    directory: Path,
    stem: str,
    suffix: str,
    *,
    overwrite: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = sanitize_stem(stem)
    suffix = suffix.lower()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    if overwrite or not candidate.exists():
        return candidate
    for index in range(1, 10_000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free filename for {stem!r} in {directory}")
