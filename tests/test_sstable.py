"""SSTable serialization, integrity and atomic publish.

The interruption tests matter most here: an engine that leaves a half-written
table on disk cannot make any claim about recovery later, so failures are
injected at every point in the publish sequence and the directory is asserted
to be clean afterwards.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import db_path, key_for, sequential_puts, value_for
from sable.errors import CorruptSSTableError, InvalidArgumentError
from sable.sstable import (
    FILE_MAGIC,
    FILE_VERSION,
    FOOTER_SIZE,
    HEADER_SIZE,
    RECORD_HEADER_SIZE,
    SSTableReader,
    SSTableWriter,
    looks_like_temporary,
)
from sable.types import EngineOptions, InternalEntry, KeyRange, OpType


def build_table(
    path: Path, count: int = 40, *, block_size: int = 64, file_id: int = 1, level: int = 0
) -> SSTableWriter:
    writer = SSTableWriter(
        path, EngineOptions(block_size_bytes=block_size), file_id=file_id, level=level
    )
    for item in sequential_puts(count):
        writer.add_entry(item)
    return writer


def write_table(path: Path, count: int = 40, **kwargs: int) -> object:
    build_table(path, count, **kwargs).finish()
    return path


def test_header_size_is_fully_packed() -> None:
    # '<' forbids implicit padding, so the layout is byte-identical everywhere.
    assert HEADER_SIZE == 100
    assert FOOTER_SIZE == 16
    assert FILE_MAGIC == b"SABLESST"
    assert FILE_VERSION == 1


def test_roundtrip_preserves_every_record(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    meta = build_table(path, count=60).finish()

    assert meta.num_entries == 60
    assert meta.num_blocks > 1, "small blocks are required for sparse indexing to matter"
    assert meta.smallest_key == key_for(0)
    assert meta.largest_key == key_for(59)

    with SSTableReader.open(path) as reader:
        records = list(reader.iter_all())

    assert len(records) == 60
    assert [entry.user_key for entry in records] == [key_for(i) for i in range(60)]
    assert records[7].value == value_for(7)
    assert meta.file_size == path.stat().st_size


def test_point_reads_resolve_across_blocks(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=50).finish()

    with SSTableReader.open(path) as reader:
        for index in (0, 1, 25, 49):
            found = reader.get_entry(key_for(index))
            assert found is not None
            assert found.user_key == key_for(index)
            assert found.value == value_for(index)
        assert reader.get_entry(b"absent") is None
        assert reader.data_block_reads > 0


def test_tombstones_survive_serialization(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    writer = SSTableWriter(tmp_path / "000001.sst", EngineOptions(block_size_bytes=32))
    writer.add_entry(InternalEntry.put(b"a", 10, b"live"))
    writer.add_entry(InternalEntry.delete(b"b", 11))
    writer.add_entry(InternalEntry.put(b"c", 12, b""))
    writer.finish()

    with SSTableReader.open(path) as reader:
        shadowed = reader.get_entry(b"b")
        assert shadowed is not None
        assert shadowed.is_tombstone
        assert shadowed.op is OpType.DELETE
        assert reader.get_entry(b"c").value == b""


def test_writer_rejects_out_of_order_entries(tmp_path: Path) -> None:
    writer = SSTableWriter(tmp_path / "x.sst", EngineOptions())
    writer.add_entry(InternalEntry.put(b"b", 1, b"v"))

    with pytest.raises(InvalidArgumentError, match="internal-key ordering"):
        writer.add_entry(InternalEntry.put(b"a", 2, b"v"))


def test_writer_rejects_empty_and_double_finish(tmp_path: Path) -> None:
    writer = SSTableWriter(tmp_path / "empty.sst", EngineOptions())
    with pytest.raises(InvalidArgumentError, match="empty SSTable"):
        writer.finish()

    filled = build_table(tmp_path / "filled.sst", count=1)
    filled.finish()
    with pytest.raises(InvalidArgumentError, match="already finished"):
        filled.finish()


def test_interruption_before_rename_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject failure at the rename step and assert the directory stays clean."""
    path = tmp_path / "000001.sst"
    real_replace = os.replace

    def exploding_replace(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(28, "simulated ENOSPC on rename")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError, match="ENOSPC"):
        build_table(path).finish()
    monkeypatch.setattr(os, "replace", real_replace)

    assert not path.exists(), "a partial table must never be visible under its final name"
    leftovers = [child.name for child in tmp_path.iterdir()]
    assert leftovers == [], f"publish failed but left artifacts: {leftovers}"


def test_interruption_before_fsync_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "000001.sst"
    real_fsync = os.fsync

    def exploding_fsync(fd: int) -> None:
        raise OSError(5, "simulated EIO during fsync")

    monkeypatch.setattr(os, "fsync", exploding_fsync)
    with pytest.raises(OSError, match="EIO"):
        build_table(path).finish()
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert not path.exists()
    assert [child.name for child in tmp_path.iterdir()] == []


def test_temporary_siblings_are_never_tables(tmp_path: Path) -> None:
    scratch = tmp_path / "000001.sst.tmp-sable"
    scratch.write_bytes(b"partial")

    assert looks_like_temporary(scratch)
    assert not looks_like_temporary(tmp_path / "000001.sst")


def test_reader_rejects_foreign_content(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    path.write_bytes(b"\x00" * (HEADER_SIZE + FOOTER_SIZE))

    with pytest.raises(CorruptSSTableError, match="not a sable SSTable"):
        SSTableReader.open(path)


def test_reader_rejects_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=10).finish()
    complete = path.read_bytes()

    path.write_bytes(complete[: len(complete) - FOOTER_SIZE])

    with pytest.raises(CorruptSSTableError, match="too small|failed header CRC"):
        SSTableReader.open(path)


def _first_value_offset(reader: SSTableReader) -> int:
    """Locate a byte inside the first record's value payload.

    Derived from the published layout rather than by searching, because the
    all-zero payload also occurs inside key spans — searching would corrupt a
    *key* and make the record unaddressable, which tests nothing about
    checksums.
    """
    meta = reader.meta
    data_offset = HEADER_SIZE + len(meta.smallest_key) + len(meta.largest_key)
    return data_offset + RECORD_HEADER_SIZE + len(meta.smallest_key)


def test_bit_flip_in_data_block_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=20).finish()

    with SSTableReader.open(path) as probe:
        target = _first_value_offset(probe)
    raw = bytearray(path.read_bytes())
    raw[target] ^= 0xFF
    path.write_bytes(bytes(raw))

    with (
        SSTableReader.open(path) as reader,
        pytest.raises(CorruptSSTableError, match="failed CRC32 verification"),
    ):
        reader.get_entry(key_for(0))


def test_checksum_verification_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=20).finish()

    with SSTableReader.open(path) as probe:
        target = _first_value_offset(probe)
    raw = bytearray(path.read_bytes())
    raw[target] ^= 0xFF
    path.write_bytes(bytes(raw))

    with SSTableReader.open(path, verify_checksums=False) as reader:
        damaged = reader.get_entry(key_for(0))
        assert damaged is not None
        assert damaged.value != value_for(0), "skipping CRC must not silently repair bytes"


def test_sparse_index_supports_binary_seek(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=80).finish()

    with SSTableReader.open(path) as reader:
        probe = reader.get_entry(key_for(79))
        assert probe is not None
        # Reaching the last block must not pay for every earlier block.
        assert reader.data_block_reads <= 2


def test_range_iteration_honours_bounds(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    build_table(path, count=30).finish()

    with SSTableReader.open(path) as reader:
        window = KeyRange(start=key_for(5), end=key_for(9))
        assert [entry.user_key for entry in reader.iter_slice(window)] == [
            key_for(i) for i in range(5, 9)
        ]

        unbounded = list(reader.iter_slice(KeyRange()))
        assert len(unbounded) == 30


def test_metadata_reports_sequence_span(tmp_path: Path) -> None:
    path = tmp_path / "000001.sst"
    writer = SSTableWriter(path, EngineOptions(block_size_bytes=48))
    writer.add_entry(InternalEntry.put(b"a", 100, b"v"))
    writer.add_entry(InternalEntry.put(b"b", 105, b"v"))
    writer.add_entry(InternalEntry.put(b"c", 110, b"v"))
    meta = writer.finish()

    assert meta.min_sequence == 100
    assert meta.max_sequence == 110
    assert meta.level == 0
    assert meta.file_id == 0
    assert meta.file_name == "000000.sst"


def test_directory_layout_after_publish(tmp_path: Path) -> None:
    target = db_path(tmp_path, "data") / "000007.sst"
    meta = build_table(target, count=5, file_id=7, level=1).finish()

    assert target.exists()
    assert target.name == meta.file_name
    assert sorted(child.name for child in target.parent.iterdir()) == [meta.file_name]
