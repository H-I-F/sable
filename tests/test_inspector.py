"""WAL inspector: frame-level classification of healthy and damaged segments.

The inspector must agree with the recovery contract the engine enforces, so the
cases below cover each branch that contract distinguishes: a clean segment, a
partial write at the tail, and a checksum failure that has live frames behind it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import flip_bit, sequential_puts
from sable.inspector import (
    FrameStatus,
    format_table,
    inspect_wal,
    inspect_wal_bytes,
    main,
    summarise,
)
from sable.types import InternalEntry
from sable.wal import FRAME_HEADER_SIZE, encode_frame


def _frames(entries: list[InternalEntry]) -> list[bytes]:
    """Encode entries into independent frame byte strings, in order."""
    return [encode_frame(entry) for entry in entries]


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_inspect_healthy_segment_reports_every_frame(tmp_path: Path) -> None:
    """Every frame in a clean segment is OK, with contiguous offsets and lengths."""
    raw = _frames(sequential_puts(4))
    path = _write(tmp_path / "wal-000001.log", b"".join(raw))

    reports = inspect_wal(path)

    assert [report.status for report in reports] == [FrameStatus.OK] * 4
    assert [report.sequence for report in reports] == [0, 1, 2, 3]
    assert [report.op for report in reports] == ["PUT"] * 4

    running = 0
    for report, frame in zip(reports, raw, strict=True):
        assert report.offset == running
        assert report.length == len(frame)
        assert report.stored_crc == report.computed_crc
        running += len(frame)
    assert running == path.stat().st_size


def test_truncated_tail_is_reported_as_torn(tmp_path: Path) -> None:
    """A frame cut mid-body is a torn tail, and the frames before it stay OK."""
    raw = _frames(sequential_puts(3))
    cut = len(b"".join(raw)) - 5  # remove the last five bytes of the final frame
    path = _write(tmp_path / "wal-000001.log", b"".join(raw)[:cut])

    reports = inspect_wal(path)

    assert [report.status for report in reports] == [
        FrameStatus.OK,
        FrameStatus.OK,
        FrameStatus.TORN_TAIL,
    ]
    assert reports[-1].offset == sum(len(frame) for frame in raw[:2])
    assert "declared" in reports[-1].detail


def test_partial_header_is_reported_as_torn(tmp_path: Path) -> None:
    """A tail shorter than one header cannot describe a frame; it is torn."""
    raw = b"".join(_frames(sequential_puts(2)))
    path = _write(tmp_path / "wal-000001.log", raw + b"\x01\x02\x03")

    reports = inspect_wal(path)

    assert reports[-1].status is FrameStatus.TORN_TAIL
    assert reports[-1].length == 3
    assert "partial header" in reports[-1].detail


def test_mid_stream_corruption_is_fatal_and_stops_the_walk(tmp_path: Path) -> None:
    """A CRC failure with frames behind it is CORRUPTED, and parsing stops there."""
    raw = _frames(sequential_puts(3))
    frames = b"".join(raw)
    corrupt_at = len(raw[0]) + FRAME_HEADER_SIZE  # first key byte of the middle frame
    path = _write(tmp_path / "wal-000001.log", flip_bit(frames, corrupt_at))

    reports = inspect_wal(path)

    assert [report.status for report in reports] == [
        FrameStatus.OK,
        FrameStatus.CORRUPTED,
    ]
    assert reports[-1].offset == len(raw[0])
    assert "crc mismatch" in reports[-1].detail


def test_terminal_crc_mismatch_is_torn_by_default(tmp_path: Path) -> None:
    """A CRC failure in the last frame is indistinguishable from a torn write."""
    raw = _frames(sequential_puts(2))
    frames = b"".join(raw)
    corrupt_at = len(raw[0]) + FRAME_HEADER_SIZE  # inside the terminal frame's key
    path = _write(tmp_path / "wal-000001.log", flip_bit(frames, corrupt_at))

    reports = inspect_wal(path)

    assert [report.status for report in reports] == [
        FrameStatus.OK,
        FrameStatus.TORN_TAIL,
    ]


def test_terminal_crc_mismatch_is_corrupt_under_strict_mode(tmp_path: Path) -> None:
    """Strict tail checking refuses the benefit of the doubt and calls it corrupt."""
    raw = _frames(sequential_puts(2))
    frames = b"".join(raw)
    corrupt_at = len(raw[0]) + FRAME_HEADER_SIZE
    path = _write(tmp_path / "wal-000001.log", flip_bit(frames, corrupt_at))

    reports = inspect_wal(path, strict_tail_crc=True)

    assert reports[-1].status is FrameStatus.CORRUPTED


def test_impossible_lengths_are_corrupt(tmp_path: Path) -> None:
    """A header declaring an impossible value length is flagged, not parsed past."""
    frame = bytearray(_frames(sequential_puts(1))[0])
    # Overwrite vlen (the last 4 bytes of the 21-byte header) with a huge value.
    frame[FRAME_HEADER_SIZE - 4 : FRAME_HEADER_SIZE] = (0xFFFFFFFF).to_bytes(4, "little")
    reports = inspect_wal_bytes(bytes(frame))

    assert len(reports) == 1
    assert reports[0].status is FrameStatus.CORRUPTED
    assert "impossible lengths" in reports[0].detail


def test_missing_file_yields_no_reports(tmp_path: Path) -> None:
    """A path that does not exist is an empty inspection, not an error."""
    assert inspect_wal(tmp_path / "absent.log") == []


def test_summarise_counts_each_status(tmp_path: Path) -> None:
    """The summary tallies OK and torn-tail frames separately."""
    raw = b"".join(_frames(sequential_puts(3)))
    path = _write(tmp_path / "wal-000001.log", raw[:-5])

    counts = summarise(inspect_wal(path))

    assert counts == {"OK": 2, "CORRUPTED": 0, "TORN_TAIL": 1, "total": 3}


def test_format_table_carries_offset_length_and_status(tmp_path: Path) -> None:
    """The rendered table exposes the columns an operator needs to triage."""
    path = _write(tmp_path / "wal-000001.log", b"".join(_frames(sequential_puts(2))))

    table = format_table(inspect_wal(path))
    header = table.splitlines()[0]

    for column in ("Offset", "Length", "Status", "Seq", "Op"):
        assert column in header
    assert "OK" in table
    assert "PUT" in table


def test_main_prints_table_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints the table and a summary line, and always exits zero."""
    path = _write(tmp_path / "wal-000001.log", b"".join(_frames(sequential_puts(2))))

    assert main([str(path)]) == 0

    captured = capsys.readouterr().out
    assert "Offset" in captured
    assert "2 frame(s)" in captured


def test_main_reports_a_missing_segment_instead_of_zero_frames(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path that does not exist exits 2 rather than printing an empty table.

    The library returns ``[]`` for a missing file, which is right for a caller
    iterating over segments that may have rotated away. A CLI has a different
    duty: "this file has no frames" and "this file is not there" must not both
    look like a successful run to whatever is parsing the exit status.
    """
    missing = tmp_path / "wal-999999.log"

    assert main([str(missing)]) == 2

    captured = capsys.readouterr()
    assert "not a readable file" in captured.err
    assert "frame(s)" not in captured.out
