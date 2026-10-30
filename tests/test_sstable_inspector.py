"""SSTable inspector: header/footer, sparse index and Bloom geometry.

Each test builds a real table through :class:`SSTableWriter` and then decodes it
back, so the inspector is checked against the same bytes the engine publishes
rather than a hand-rolled fixture that could drift from the format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import flip_bit, key_for, value_for
from sable.errors import CorruptSSTableError
from sable.sstable import FILE_MAGIC, HEADER_SIZE, SSTableWriter
from sable.sstable_inspector import format_report, main, parse_sstable
from sable.types import EngineOptions, InternalEntry

COUNT = 20


def _build_table(path: Path, *, block_size: int = 64, bloom: float = 0.01) -> Path:
    """Publish a deterministic table with several blocks and return its path."""
    options = EngineOptions(block_size_bytes=block_size, bloom_false_positive_rate=bloom)
    writer = SSTableWriter(path, options, file_id=1, level=0)
    for index in range(COUNT):
        writer.add_entry(InternalEntry.put(key_for(index), index, value_for(index)))
    writer.finish()
    return path


def test_parse_reports_header_and_index(tmp_path: Path) -> None:
    """Header counts, key span and one index row per block are all recovered."""
    path = _build_table(tmp_path / "000001.sst")

    inspection = parse_sstable(path)

    assert inspection.magic == FILE_MAGIC.decode("ascii")
    assert inspection.version == 1
    assert inspection.num_entries == COUNT
    assert inspection.num_blocks == len(inspection.index_rows)
    assert inspection.header_crc_ok is True
    assert inspection.smallest_key == key_for(0)
    assert inspection.largest_key == key_for(COUNT - 1)
    assert inspection.index_rows[0].first_key == key_for(0)
    assert inspection.index_rows[0].block_offset == inspection.data_offset
    assert sum(row.block_size for row in inspection.index_rows) == inspection.data_size

    offsets = [row.block_offset for row in inspection.index_rows]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_parse_reports_bloom_geometry(tmp_path: Path) -> None:
    """The Bloom block decodes to a sane bit array with a low implied FP rate."""
    path = _build_table(tmp_path / "000001.sst")

    bloom = parse_sstable(path).bloom

    assert bloom is not None
    assert bloom.bit_width >= COUNT
    assert bloom.byte_width == (bloom.bit_width + 7) // 8
    assert bloom.probe_count >= 1
    assert 0.0 < bloom.fill_ratio <= 1.0
    assert 0.0 < bloom.estimated_fp_rate <= 1.0
    assert 0 < bloom.estimated_entries <= COUNT * 4


def test_bloom_disabled_yields_no_filter(tmp_path: Path) -> None:
    """A table built with filtering disabled reports no Bloom block."""
    path = _build_table(tmp_path / "000001.sst", bloom=0.0)

    inspection = parse_sstable(path)

    assert inspection.bloom is None
    assert inspection.filter_size == 0


def test_header_crc_detects_tamper(tmp_path: Path) -> None:
    """Flipping a byte in the header/span region flips header-crc to FAIL."""
    path = _build_table(tmp_path / "000001.sst")
    tampered = flip_bit(path.read_bytes(), HEADER_SIZE)
    path.write_bytes(tampered)

    inspection = parse_sstable(path)

    assert inspection.header_crc_ok is False


def test_foreign_magic_is_rejected(tmp_path: Path) -> None:
    """A file that does not start with SABLESST cannot be interpreted."""
    path = tmp_path / "000001.sst"
    path.write_bytes(b"\x00" * 256)

    with pytest.raises(CorruptSSTableError):
        parse_sstable(path)


def test_tiny_file_is_rejected(tmp_path: Path) -> None:
    """A file shorter than a header plus a footer is not a table."""
    path = tmp_path / "000001.sst"
    path.write_bytes(b"short")

    with pytest.raises(CorruptSSTableError):
        parse_sstable(path)


def test_format_report_covers_every_section(tmp_path: Path) -> None:
    """The rendered report names each region and lists block offsets."""
    path = _build_table(tmp_path / "000001.sst")

    report = format_report(parse_sstable(path))

    for marker in ("SSTable:", "[Header]", "[Footer]", "[Data]", "[Index]", "[Bloom]"):
        assert marker in report
    assert "FirstKey" in report
    assert "header-crc=OK" in report


def test_main_prints_report_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints the report and exits zero."""
    path = _build_table(tmp_path / "000001.sst")

    assert main([str(path)]) == 0

    captured = capsys.readouterr().out
    assert "SSTable:" in captured
    assert "[Bloom]" in captured


def test_main_reports_an_unreadable_file_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing file exits 2 and says so on stderr, rather than raising."""
    missing = tmp_path / "000404.sst"

    assert main([str(missing)]) == 2

    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "000404.sst" in captured.err
    assert captured.out == ""


def test_main_reports_a_foreign_file_as_damage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that is not an SSTable exits 2 with the corruption reason."""
    foreign = tmp_path / "000405.sst"
    foreign.write_bytes(b"NOTSABLE" + b"\x00" * 128)

    assert main([str(foreign)]) == 2

    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert captured.out == ""
