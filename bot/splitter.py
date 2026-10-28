"""
Streaming combo-file splitter.

Design choices:

* Files are read in binary. We never decode line contents during splitting,
  so whatever line endings the input had (\\n, \\r\\n, \\r, or mixed) are
  preserved byte-for-byte in the output.
* A "line" is whatever `file.readline()` returns — including its trailing
  terminator when there is one. Files without a trailing newline are handled
  correctly.
* The header (when preservation is on) is read separately and prepended to
  every part. It counts toward the size budget in size mode.
* A single line larger than `max_bytes_per_part` is written alone to its own
  part. We can't split a line without breaking "keep whole lines intact",
  so the over-budget part is the trade-off.
* `max_parts` is a hard safety stop. It raises `TooManyParts`.
* Everything is streamed — we do not hold the whole file in memory.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

ProgressCb = Callable[[int, int], None]


class SplitError(Exception):
    pass


class TooManyParts(SplitError):
    pass


@dataclass
class SplitResult:
    parts: list[Path]
    total_lines: int
    total_bytes_in: int
    total_bytes_out: int
    mode: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FILENAME_BAD = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Strip directory components and any character that could escape a dir."""
    name = os.path.basename(name or "")
    name = _FILENAME_BAD.sub("_", name).strip(". ")
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[: 200 - len(ext)] + ext
    return name or "file.txt"


def _output_name(base: str, idx: int) -> str:
    stem, ext = os.path.splitext(base)
    if not ext:
        ext = ".txt"
    return f"{stem}_part{idx:03d}{ext}"


def _iter_content_lines(path: Path, skip_first: bool) -> Iterable[bytes]:
    with path.open("rb") as f:
        if skip_first:
            f.readline()
        for line in f:
            yield line


def _read_header(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.readline()


def count_lines(path: Path, skip_first: bool = False) -> int:
    n = 0
    with path.open("rb") as f:
        if skip_first:
            f.readline()
        for _ in f:
            n += 1
    return n


# ---------------------------------------------------------------------------
# core engine
# ---------------------------------------------------------------------------

def _split_generic(
    src: Path,
    dst_dir: Path,
    base_name: str,
    *,
    should_break: Callable[[int, int, int, int], bool],
    preserve_header: bool,
    max_parts: Optional[int],
    progress: Optional[ProgressCb],
    mode: str,
) -> SplitResult:
    dst_dir.mkdir(parents=True, exist_ok=True)
    total_in = src.stat().st_size

    header = _read_header(src) if preserve_header else b""
    content = _iter_content_lines(src, skip_first=preserve_header)

    parts: list[Path] = []
    fh = None
    cur_path: Optional[Path] = None
    cur_count = 0
    cur_size = 0
    total_lines = 0
    total_out = 0
    bytes_read = len(header) if preserve_header else 0

    def _open_new_part() -> None:
        nonlocal fh, cur_path, cur_count, cur_size, total_out
        if max_parts is not None and len(parts) >= max_parts:
            raise TooManyParts(f"reached MAX_PARTS={max_parts}")
        cur_path = dst_dir / _output_name(base_name, len(parts) + 1)
        fh = cur_path.open("wb")
        cur_count = 0
        cur_size = 0
        if header:
            fh.write(header)
            cur_size += len(header)
            total_out += len(header)

    def _close_current() -> None:
        nonlocal fh, cur_path
        if fh is not None:
            fh.close()
            parts.append(cur_path)
            fh = None
            cur_path = None

    try:
        for line in content:
            line_len = len(line)
            if fh is None:
                _open_new_part()
            elif should_break(cur_count, cur_size, line_len, len(parts) + 1):
                _close_current()
                _open_new_part()

            fh.write(line)
            cur_count += 1
            cur_size += line_len
            total_lines += 1
            total_out += line_len
            bytes_read += line_len

            if progress and (total_lines & 0x1FF) == 0:
                progress(bytes_read, total_in)

        _close_current()
        if progress:
            progress(total_in, total_in)
    finally:
        if fh is not None:
            fh.close()

    return SplitResult(
        parts=parts,
        total_lines=total_lines,
        total_bytes_in=total_in,
        total_bytes_out=total_out,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# public split modes
# ---------------------------------------------------------------------------

def split_by_lines(
    src: Path,
    dst_dir: Path,
    base_name: str,
    lines_per_part: int,
    *,
    preserve_header: bool = False,
    max_parts: Optional[int] = None,
    progress: Optional[ProgressCb] = None,
) -> SplitResult:
    if lines_per_part < 1:
        raise ValueError("lines_per_part must be >= 1")
    return _split_generic(
        src,
        dst_dir,
        base_name,
        should_break=lambda c, s, n, i: c >= lines_per_part,
        preserve_header=preserve_header,
        max_parts=max_parts,
        progress=progress,
        mode=f"lines:{lines_per_part}",
    )


def split_by_size(
    src: Path,
    dst_dir: Path,
    base_name: str,
    max_bytes_per_part: int,
    *,
    preserve_header: bool = False,
    max_parts: Optional[int] = None,
    progress: Optional[ProgressCb] = None,
) -> SplitResult:
    if max_bytes_per_part < 1:
        raise ValueError("max_bytes_per_part must be >= 1")
    return _split_generic(
        src,
        dst_dir,
        base_name,
        # Only break if we already wrote at least one content line,
        # otherwise a large header would cause an infinite string of
        # header-only parts.
        should_break=lambda c, s, n, i: c > 0 and (s + n) > max_bytes_per_part,
        preserve_header=preserve_header,
        max_parts=max_parts,
        progress=progress,
        mode=f"size:{max_bytes_per_part}",
    )


def split_into_n_parts(
    src: Path,
    dst_dir: Path,
    base_name: str,
    n_parts: int,
    *,
    preserve_header: bool = False,
    max_parts: Optional[int] = None,
    progress: Optional[ProgressCb] = None,
) -> SplitResult:
    if n_parts < 1:
        raise ValueError("n_parts must be >= 1")

    total_lines = count_lines(src, skip_first=preserve_header)
    if total_lines == 0:
        return SplitResult(
            parts=[],
            total_lines=0,
            total_bytes_in=src.stat().st_size,
            total_bytes_out=0,
            mode=f"n_parts:{n_parts}",
        )

    lines_per_part = max(1, math.ceil(total_lines / n_parts))
    result = split_by_lines(
        src,
        dst_dir,
        base_name,
        lines_per_part,
        preserve_header=preserve_header,
        max_parts=max_parts,
        progress=progress,
    )
    # Report the requested mode, not the internal line count.
    result.mode = f"n_parts:{n_parts}"
    return result