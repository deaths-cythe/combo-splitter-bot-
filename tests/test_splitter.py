from __future__ import annotations

from pathlib import Path

import pytest

from bot.splitter import (
    TooManyParts,
    count_lines,
    sanitize_filename,
    split_by_lines,
    split_by_size,
    split_into_n_parts,
)


@pytest.fixture
def make_src(tmp_path: Path):
    def _make(content: bytes, name: str = "input.txt") -> Path:
        p = tmp_path / name
        p.write_bytes(content)
        return p

    return _make


# ------------------------------------------------------------------ lines

def test_split_by_lines_basic(make_src, tmp_path):
    src = make_src(b"a\nb\nc\nd\ne\n")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 2)
    assert [p.read_bytes() for p in r.parts] == [b"a\nb\n", b"c\nd\n", b"e\n"]
    assert r.total_lines == 5


def test_split_by_lines_no_trailing_newline(make_src, tmp_path):
    src = make_src(b"a\nb\nc")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 2)
    assert [p.read_bytes() for p in r.parts] == [b"a\nb\n", b"c"]
    assert r.total_lines == 3


def test_split_by_lines_crlf_preserved(make_src, tmp_path):
    src = make_src(b"a\r\nb\r\nc\r\n")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 2)
    assert [p.read_bytes() for p in r.parts] == [b"a\r\nb\r\n", b"c\r\n"]


def test_split_by_lines_preserve_header(make_src, tmp_path):
    src = make_src(b"h1,h2\na,b\nc,d\ne,f\n")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 1, preserve_header=True)
    assert [p.read_bytes() for p in r.parts] == [
        b"h1,h2\na,b\n",
        b"h1,h2\nc,d\n",
        b"h1,h2\ne,f\n",
    ]
    assert r.total_lines == 3


def test_split_by_lines_empty(make_src, tmp_path):
    src = make_src(b"")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 10)
    assert r.parts == []
    assert r.total_lines == 0


def test_split_by_lines_single_line(make_src, tmp_path):
    src = make_src(b"only\n")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 10)
    assert [p.read_bytes() for p in r.parts] == [b"only\n"]


def test_split_by_lines_single_line_no_newline(make_src, tmp_path):
    src = make_src(b"only")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "input.txt", 10)
    assert [p.read_bytes() for p in r.parts] == [b"only"]


# ------------------------------------------------------------------- size

def test_split_by_size(make_src, tmp_path):
    src = make_src(b"aaa\n" * 10)  # 4 bytes per line
    out = tmp_path / "out"
    r = split_by_size(src, out, "input.txt", 8)
    assert [p.read_bytes() for p in r.parts] == [b"aaa\naaa\n"] * 5


def test_split_by_size_huge_single_line(make_src, tmp_path):
    src = make_src(b"x" * 100 + b"\n")
    out = tmp_path / "out"
    r = split_by_size(src, out, "input.txt", 50)
    assert len(r.parts) == 1
    assert r.parts[0].read_bytes() == b"x" * 100 + b"\n"


def test_split_by_size_header_counts_toward_budget(make_src, tmp_path):
    # header 12 bytes + lines of 4 bytes, budget 16 → header + one line per part
    src = make_src(b"headerline\r\n" + b"aaa\n" * 4)
    out = tmp_path / "out"
    r = split_by_size(src, out, "input.txt", 16, preserve_header=True)
    for p in r.parts:
        b = p.read_bytes()
        assert b.startswith(b"headerline\r\n")
        assert len(b) <= 16 + 1  # header + one content line, then next line breaks


# ------------------------------------------------------------------- equal

def test_split_into_n_parts(make_src, tmp_path):
    src = make_src(b"".join(b"line\n" for _ in range(10)))
    out = tmp_path / "out"
    r = split_into_n_parts(src, out, "input.txt", 3)
    assert len(r.parts) == 3
    assert sum(count_lines(p) for p in r.parts) == 10


def test_split_into_n_parts_empty(make_src, tmp_path):
    src = make_src(b"")
    out = tmp_path / "out"
    r = split_into_n_parts(src, out, "input.txt", 3)
    assert r.parts == []


def test_split_into_n_parts_more_parts_than_lines(make_src, tmp_path):
    src = make_src(b"a\nb\n")
    out = tmp_path / "out"
    r = split_into_n_parts(src, out, "input.txt", 5)
    assert len(r.parts) == 2


# ------------------------------------------------------------------- guards

def test_max_parts_guard(make_src, tmp_path):
    src = make_src(b"".join(b"x\n" for _ in range(100)))
    out = tmp_path / "out"
    with pytest.raises(TooManyParts):
        split_by_lines(src, out, "input.txt", 1, max_parts=10)


# ------------------------------------------------------------------- names

def test_sanitize_filename():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b/c.txt") == "c.txt"
    assert sanitize_filename("") == "file.txt"
    assert sanitize_filename("my file (1).txt") == "my file (1).txt"
    assert sanitize_filename("weird<name>.txt") == "weird_name_.txt"


def test_output_filenames(make_src, tmp_path):
    src = make_src(b"a\nb\nc\n")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "mycombo.txt", 1)
    assert [p.name for p in r.parts] == [
        "mycombo_part001.txt",
        "mycombo_part002.txt",
        "mycombo_part003.txt",
    ]


def test_output_filenames_no_extension(make_src, tmp_path):
    src = make_src(b"a\nb\n", name="mycombo")
    out = tmp_path / "out"
    r = split_by_lines(src, out, "mycombo", 1)
    assert [p.name for p in r.parts] == [
        "mycombo_part001.txt",
        "mycombo_part002.txt",
    ]