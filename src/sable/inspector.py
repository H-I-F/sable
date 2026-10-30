"""Frame-by-frame, non-destructive inspection of a WAL segment.

Every WAL frame is self-describing, so a segment can be audited without the
engine: a fixed 21-byte header (``crc32 | sequence | op | klen | vlen``) telling
the reader exactly where the next frame starts, followed by the key and value
bytes. This module walks that stream purely for observation — it never truncates,
rotates or replays — and classifies each frame it meets.

The three outcomes mirror the recovery contract the engine actually enforces,
restated so an operator can see which one bit them:

* ``OK`` — the header parsed, the declared body was present, and the CRC32 over
  ``sequence..value`` matched.
* ``CORRUPTED`` — a structurally impossible frame, or a CRC mismatch that has
  further frames behind it. The engine refuses to proceed on this; so does the
  walk, because once lengths are untrusted the next offset is a guess.
* ``TORN_TAIL`` — a partial write at the very end of the stream, either a short
  header/body or a terminal frame whose CRC disagrees. Expected after a crash
  and safely discardable, exactly like ``WriteAheadLog.recover`` treats it.

The distinction between the last two is the whole point of the tool: a torn tail
is housekeeping, a mid-stream checksum failure is an incident.
"""

from __future__ import annotations

import argparse
import enum
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from sable.types import MAX_KEY_BYTES, MAX_VALUE_BYTES, OpType
from sable.wal import FRAME_HEADER, FRAME_HEADER_SIZE

__all__ = [
    "FrameReport",
    "FrameStatus",
    "format_table",
    "inspect_wal",
    "inspect_wal_bytes",
    "main",
    "summarise",
]

_OP_NAMES: dict[int, str] = {
    int(OpType.PUT): "PUT",
    int(OpType.DELETE): "DELETE",
}


class FrameStatus(str, enum.Enum):
    """Per-frame verdict, named after the recovery outcome it represents."""

    OK = "OK"
    CORRUPTED = "CORRUPTED"
    TORN_TAIL = "TORN_TAIL"


@dataclass(slots=True)
class FrameReport:
    """One parsed frame, or the unparseable bytes that ended the walk.

    Attributes:
        offset: Byte position of the frame within the segment.
        length: Bytes the frame occupies, or every remaining byte when the frame
            could not be delimited.
        status: Verdict for this frame.
        sequence: Sequence number read from the header, when it was parseable.
        op: ``"PUT"`` or ``"DELETE"``, when the op byte was recognised.
        key_len: Declared key length in bytes.
        value_len: Declared value length in bytes.
        stored_crc: CRC32 stored in the header.
        computed_crc: CRC32 recomputed over the frame body.
        detail: Human-readable explanation, empty for an ``OK`` frame.
    """

    offset: int
    length: int
    status: FrameStatus
    sequence: int | None = None
    op: str | None = None
    key_len: int = 0
    value_len: int = 0
    stored_crc: int = 0
    computed_crc: int = 0
    detail: str = ""


def inspect_wal(path: Path, *, strict_tail_crc: bool = False) -> list[FrameReport]:
    """Inspect a segment on disk without modifying it.

    Args:
        path: WAL segment to read. A missing file yields an empty report list so
            a caller iterating candidate segments needs no special case.
        strict_tail_crc: When true, a CRC disagreement in the terminal frame is
            reported as ``CORRUPTED`` rather than folded into ``TORN_TAIL``.

    Returns:
        One :class:`FrameReport` per frame reached, in file order.
    """
    path = Path(path)
    if not path.exists():
        return []
    return inspect_wal_bytes(path.read_bytes(), strict_tail_crc=strict_tail_crc)


def inspect_wal_bytes(data: bytes, *, strict_tail_crc: bool = False) -> list[FrameReport]:
    """Inspect an in-memory WAL image, stopping at the first non-``OK`` frame.

    Args:
        data: Complete byte image of a segment.
        strict_tail_crc: As for :func:`inspect_wal`.

    Returns:
        Frame reports in file order. Parsing stops at a torn tail or a corrupted
        frame, because neither leaves a trustworthy offset to continue from.
    """
    reports: list[FrameReport] = []
    offset = 0
    total = len(data)

    while offset < total:
        remaining = total - offset
        if remaining < FRAME_HEADER_SIZE:
            reports.append(
                FrameReport(
                    offset=offset,
                    length=remaining,
                    status=FrameStatus.TORN_TAIL,
                    detail=f"partial header: {remaining} of {FRAME_HEADER_SIZE} bytes",
                )
            )
            break

        stored_crc, sequence, op_raw, klen, vlen = FRAME_HEADER.unpack_from(data, offset)
        if klen > MAX_KEY_BYTES or vlen > MAX_VALUE_BYTES:
            reports.append(
                FrameReport(
                    offset=offset,
                    length=remaining,
                    status=FrameStatus.CORRUPTED,
                    sequence=sequence,
                    stored_crc=stored_crc,
                    detail=f"impossible lengths: klen={klen} vlen={vlen}",
                )
            )
            break
        if op_raw not in _OP_NAMES:
            reports.append(
                FrameReport(
                    offset=offset,
                    length=remaining,
                    status=FrameStatus.CORRUPTED,
                    sequence=sequence,
                    stored_crc=stored_crc,
                    detail=f"unknown op byte: {op_raw}",
                )
            )
            break

        frame_bytes = FRAME_HEADER_SIZE + klen + vlen
        if remaining < frame_bytes:
            reports.append(
                FrameReport(
                    offset=offset,
                    length=remaining,
                    status=FrameStatus.TORN_TAIL,
                    sequence=sequence,
                    op=_OP_NAMES[op_raw],
                    key_len=klen,
                    value_len=vlen,
                    stored_crc=stored_crc,
                    detail=f"declared {frame_bytes} bytes, only {remaining} present",
                )
            )
            break

        computed_crc = zlib.crc32(data[offset + 4 : offset + frame_bytes]) & 0xFFFFFFFF
        if computed_crc != stored_crc:
            is_terminal = offset + frame_bytes == total
            status = FrameStatus.CORRUPTED
            if is_terminal and not strict_tail_crc:
                status = FrameStatus.TORN_TAIL
            reports.append(
                FrameReport(
                    offset=offset,
                    length=frame_bytes,
                    status=status,
                    sequence=sequence,
                    op=_OP_NAMES[op_raw],
                    key_len=klen,
                    value_len=vlen,
                    stored_crc=stored_crc,
                    computed_crc=computed_crc,
                    detail=(
                        f"crc mismatch: stored={stored_crc:#010x} "
                        f"computed={computed_crc:#010x}"
                    ),
                )
            )
            break

        reports.append(
            FrameReport(
                offset=offset,
                length=frame_bytes,
                status=FrameStatus.OK,
                sequence=sequence,
                op=_OP_NAMES[op_raw],
                key_len=klen,
                value_len=vlen,
                stored_crc=stored_crc,
                computed_crc=computed_crc,
            )
        )
        offset += frame_bytes

    return reports


def summarise(reports: list[FrameReport]) -> dict[str, int]:
    """Count frames by status, plus a total.

    Args:
        reports: Output of :func:`inspect_wal`.

    Returns:
        Mapping with ``"OK"``, ``"CORRUPTED"``, ``"TORN_TAIL"`` and ``"total"``.
    """
    counts = {"OK": 0, "CORRUPTED": 0, "TORN_TAIL": 0, "total": len(reports)}
    for report in reports:
        counts[report.status.value] += 1
    return counts


def format_table(reports: list[FrameReport]) -> str:
    """Render reports as a fixed-width table suitable for a terminal.

    Args:
        reports: Output of :func:`inspect_wal`.

    Returns:
        A multi-line string with a header row and one row per frame. Every row
        carries the offset, the byte length and the status, so the table answers
        "where does the damage start and how much went with it?" at a glance.
    """
    headers = ("Offset", "Length", "Status", "Seq", "Op", "Key", "Value", "Detail")
    rows: list[tuple[str, ...]] = []
    for report in reports:
        rows.append(
            (
                str(report.offset),
                str(report.length),
                report.status.value,
                "-" if report.sequence is None else str(report.sequence),
                report.op or "-",
                str(report.key_len),
                str(report.value_len),
                report.detail,
            )
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: tuple[str, ...]) -> str:
        pads = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        return "  ".join(pads).rstrip()

    lines = [render(headers), render(tuple("-" * width for width in widths))]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print the frame table for one WAL segment.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` once a segment was read, whatever its frames look like: the table
        content, not the exit status, carries the verdict so the command composes
        cleanly with pipelines. ``2`` when the path does not name a readable file,
        because "no frames" and "no such file" must not look the same to a
        pipeline watching the exit status.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sable.inspector",
        description="Inspect a sable WAL segment frame by frame without mutating it.",
    )
    parser.add_argument("path", type=Path, help="path to a wal-NNNNNN.log segment")
    parser.add_argument(
        "--strict-tail-crc",
        action="store_true",
        help="treat a terminal-frame CRC mismatch as corruption, not a torn tail",
    )
    args = parser.parse_args(argv)

    if not args.path.is_file():
        print(f"error: not a readable file: {args.path}", file=sys.stderr)
        return 2

    reports = inspect_wal(args.path, strict_tail_crc=args.strict_tail_crc)
    print(format_table(reports))
    counts = summarise(reports)
    print(
        f"\n{counts['total']} frame(s): "
        f"{counts['OK']} ok, {counts['CORRUPTED']} corrupted, {counts['TORN_TAIL']} torn tail"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
