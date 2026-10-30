"""Decode the physical structure of one immutable SSTable file.

An SSTable is a self-describing container: a fixed header names every region, a
sparse index maps each data block to its physical offset and first key, a Bloom
filter answers membership before any block IO, and a footer re-checks the header
CRC. This module reads all of that back and renders it, so the file can be
audited without the engine and without reading every record.

Layout reference (see :mod:`sable.sstable`)::

    HEADER (100 B) | smallest/largest keys | DATA BLOCKS | INDEX BLOCK |
    FILTER BLOCK | FOOTER (16 B)

The header carries the region offsets; the footer carries the magic and a CRC32
over ``header + key spans``. The inspector verifies that CRC and reports it
rather than aborting on it, because diagnosing a damaged table is the point.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sable.bloom import BloomFilter
from sable.errors import CorruptSSTableError
from sable.sstable import (
    FILE_MAGIC,
    FLAG_HAS_FILTER,
    FOOTER_SIZE,
    FOOTER_STRUCT,
    HEADER_SIZE,
    HEADER_STRUCT,
)

__all__ = [
    "BloomStats",
    "IndexRow",
    "SSTableInspection",
    "format_report",
    "main",
    "parse_sstable",
]

# block_offset | block_size | first_key_len | block_crc32, then key bytes.
_INDEX_ROW = struct.Struct("<QIII")
_INDEX_ROW_SIZE = _INDEX_ROW.size


@dataclass(slots=True)
class IndexRow:
    """One sparse-index entry: where a data block lives and what it starts with.

    Attributes:
        block_offset: Absolute byte offset of the block in the file.
        block_size: Length of the block in bytes.
        crc32: Checksum stored for the block.
        first_key: Smallest user key contained in the block.
    """

    block_offset: int
    block_size: int
    crc32: int
    first_key: bytes


@dataclass(slots=True)
class BloomStats:
    """Geometry and inferred quality of the filter block.

    Attributes:
        bit_width: Total number of bits in the filter.
        byte_width: Bytes the bit array occupies.
        probe_count: Number of hash probes per lookup.
        fill_ratio: Fraction of bits currently set.
        estimated_entries: Distinct keys inferred from the fill ratio via
            ``n ≈ -(m / k) * ln(1 - p)``. The filter does not store ``n``; this is
            a reconstruction, exact only in expectation.
        estimated_fp_rate: False-positive probability implied by the observed
            fill, ``p ** k``.
    """

    bit_width: int
    byte_width: int
    probe_count: int
    fill_ratio: float
    estimated_entries: float
    estimated_fp_rate: float


@dataclass(slots=True)
class SSTableInspection:
    """A fully decoded view of one SSTable file."""

    path: Path
    file_size: int
    magic: str
    version: int
    flags: int
    num_entries: int
    num_blocks: int
    min_sequence: int
    max_sequence: int
    data_offset: int
    data_size: int
    index_offset: int
    index_size: int
    filter_offset: int
    filter_size: int
    smallest_key: bytes
    largest_key: bytes
    header_crc_ok: bool
    index_rows: list[IndexRow]
    bloom: BloomStats | None


def _read_exact(handle: BinaryIO, offset: int, size: int) -> bytes:
    """Read exactly ``size`` bytes at ``offset`` from an open binary handle."""
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise CorruptSSTableError(f"file ended early at offset {offset} ({size} bytes requested)")
    return data


def _parse_index(blob: bytes) -> list[IndexRow]:
    """Decode the sparse index block into one row per data block."""
    rows: list[IndexRow] = []
    position = 0
    while position < len(blob):
        block_offset, block_size, key_len, crc = _INDEX_ROW.unpack_from(blob, position)
        key_start = position + _INDEX_ROW_SIZE
        first_key = blob[key_start : key_start + key_len]
        rows.append(
            IndexRow(
                block_offset=block_offset,
                block_size=block_size,
                crc32=crc,
                first_key=first_key,
            )
        )
        position = key_start + key_len
    return rows


def _bloom_stats(blob: bytes) -> BloomStats:
    """Summarise a Bloom block, inferring element count and false-positive rate."""
    bloom = BloomFilter.from_bytes(blob)
    bit_width = bloom.bit_width
    probes = bloom.probe_count
    fill = bloom.fill_ratio()
    if 0.0 < fill < 1.0:
        estimated_entries = -(bit_width / probes) * math.log(1.0 - fill)
    else:
        # A completely empty or saturated filter carries no slope to invert.
        estimated_entries = float(bit_width) if fill >= 1.0 else 0.0
    return BloomStats(
        bit_width=bit_width,
        byte_width=bloom.byte_width,
        probe_count=probes,
        fill_ratio=fill,
        estimated_entries=estimated_entries,
        estimated_fp_rate=fill**probes,
    )


def parse_sstable(path: Path) -> SSTableInspection:
    """Decode ``path`` into an :class:`SSTableInspection`.

    Args:
        path: A file published by :class:`sable.sstable.SSTableWriter`.

    Returns:
        The decoded header, sparse index and Bloom geometry.

    Raises:
        CorruptSSTableError: The file is too small, or its leading magic is not
            ``SABLESST`` — both mean the bytes cannot be interpreted at all. A
            footer CRC mismatch is *reported* via ``header_crc_ok`` instead of
            raised, so a damaged-but-readable table can still be inspected.
    """
    path = Path(path)
    size = path.stat().st_size
    if size < HEADER_SIZE + FOOTER_SIZE:
        raise CorruptSSTableError(f"{path.name} is too small to be a table")

    with path.open("rb") as handle:
        header = _read_exact(handle, 0, HEADER_SIZE)
        fields = HEADER_STRUCT.unpack(header)
        (magic, version, flags) = (fields[0], fields[1], fields[2])
        if magic != FILE_MAGIC:
            raise CorruptSSTableError(f"{path.name} is not a sable SSTable")

        (
            num_entries,
            num_blocks,
            min_sequence,
            max_sequence,
            data_offset,
            data_size,
            index_offset,
            index_size,
            filter_offset,
            filter_size,
            smallest_len,
            largest_len,
        ) = (
            fields[3],
            fields[4],
            fields[5],
            fields[6],
            fields[7],
            fields[8],
            fields[9],
            fields[10],
            fields[11],
            fields[12],
            fields[13],
            fields[14],
        )

        spans = _read_exact(handle, HEADER_SIZE, smallest_len + largest_len)
        footer = _read_exact(handle, size - FOOTER_SIZE, FOOTER_SIZE)
        footer_magic, header_crc, _reserved = FOOTER_STRUCT.unpack(footer)
        header_crc_ok = (
            footer_magic == FILE_MAGIC
            and header_crc == zlib.crc32(header + spans) & 0xFFFFFFFF
        )

        index_blob = _read_exact(handle, index_offset, index_size) if index_size else b""
        rows = _parse_index(index_blob)

        bloom: BloomStats | None = None
        if flags & FLAG_HAS_FILTER and filter_size:
            bloom = _bloom_stats(_read_exact(handle, filter_offset, filter_size))

    return SSTableInspection(
        path=path,
        file_size=size,
        magic=magic.decode("ascii", errors="replace"),
        version=version,
        flags=flags,
        num_entries=num_entries,
        num_blocks=num_blocks,
        min_sequence=min_sequence,
        max_sequence=max_sequence,
        data_offset=data_offset,
        data_size=data_size,
        index_offset=index_offset,
        index_size=index_size,
        filter_offset=filter_offset,
        filter_size=filter_size,
        smallest_key=spans[:smallest_len],
        largest_key=spans[smallest_len:],
        header_crc_ok=header_crc_ok,
        index_rows=rows,
        bloom=bloom,
    )


def _short(key: bytes, limit: int = 32) -> str:
    """Render a key compactly, eliding the middle when it is long."""
    if len(key) <= limit:
        return repr(key)
    return repr(key[: limit // 2])[:-1] + "..." + repr(key[-(limit // 2) :])[1:]


def format_report(inspection: SSTableInspection) -> str:
    """Render a decoded table as a multi-section text report.

    Args:
        inspection: Result of :func:`parse_sstable`.

    Returns:
        A report covering header/footer, the sparse index as a table of block
        offset and first key, and the Bloom geometry.
    """
    lines: list[str] = []
    lines.append(f"SSTable: {inspection.path}  ({inspection.file_size} bytes)")
    lines.append(
        f"[Header]  magic={inspection.magic} version={inspection.version} "
        f"flags={inspection.flags:#04x} entries={inspection.num_entries} "
        f"blocks={inspection.num_blocks}"
    )
    lines.append(
        f"          seq=[{inspection.min_sequence}..{inspection.max_sequence}]  "
        f"smallest={_short(inspection.smallest_key)}  largest={_short(inspection.largest_key)}"
    )
    lines.append(
        f"[Footer]  header-crc={'OK' if inspection.header_crc_ok else 'FAIL'}"
    )
    lines.append(
        f"[Data]    offset={inspection.data_offset} size={inspection.data_size}"
    )
    lines.append(
        f"[Index]   offset={inspection.index_offset} size={inspection.index_size} "
        f"rows={len(inspection.index_rows)}"
    )

    header = ("#", "BlockOffset", "BlockSize", "CRC32", "FirstKey")
    table_rows: list[tuple[str, ...]] = []
    for number, index_row in enumerate(inspection.index_rows):
        table_rows.append(
            (
                str(number),
                str(index_row.block_offset),
                str(index_row.block_size),
                f"{index_row.crc32:08x}",
                _short(index_row.first_key),
            )
        )
    widths = [len(cell) for cell in header]
    for table_row in table_rows:
        for index, cell in enumerate(table_row):
            widths[index] = max(widths[index], len(cell))

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()

    lines.append("          " + render(header))
    lines.append("          " + render(tuple("-" * width for width in widths)))
    for table_row in table_rows:
        lines.append("          " + render(table_row))

    if inspection.bloom is None:
        lines.append("[Bloom]   absent (filtering disabled for this table)")
    else:
        bloom = inspection.bloom
        lines.append(
            f"[Bloom]   bits={bloom.bit_width} bytes={bloom.byte_width} "
            f"probes={bloom.probe_count} fill={bloom.fill_ratio:.1%}"
        )
        lines.append(
            f"          ~entries={bloom.estimated_entries:.0f} "
            f"~false-positive={bloom.estimated_fp_rate:.4%}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print the structural report for one SSTable.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` once a table was decoded. ``2`` when the file cannot be read or
        cannot be interpreted as a table, with the reason on stderr: an operator
        asking about one file wants to know what is wrong with it, not where the
        interpreter gave up, and the exit status has to differ from success for
        the diagnosis to be usable from a script.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sable.sstable_inspector",
        description="Decode an SSTable's header, sparse index and Bloom filter.",
    )
    parser.add_argument("path", type=Path, help="path to an NNNNNN.sst file")
    args = parser.parse_args(argv)

    try:
        inspection = parse_sstable(args.path)
    except (OSError, CorruptSSTableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_report(inspection))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
