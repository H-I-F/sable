"""Immutable, byte-addressable SSTable files.

Layout (little-endian throughout, no implicit padding)::

    +--------------------+
    | HEADER       100 B | magic | version | flags | counts | offsets | spans
    +--------------------+
    | smallest_key       | variable, length from header
    | largest_key        | variable, length from header
    +--------------------+
    | DATA BLOCKS        | restart-aligned runs of length-prefixed records
    +--------------------+
    | INDEX BLOCK        | per-block (offset, size, crc32, first_key)
    +--------------------+
    | FILTER BLOCK       | reserved for the Bloom bit-vector (PR 08)
    +--------------------+
    | FOOTER        16 B | magic | crc32(header+spans) | reserved
    +--------------------+

Why each piece exists
---------------------
* **Data blocks** are the unit of IO. Moving 4 KiB at a time makes read
  amplification measurable instead of accidental, and each block carries its own
  CRC so corruption is detected against the block actually being read rather
  than by whole-file checksumming on open.
* **Index blocks** hold one row per data block. Memory cost is one entry per
  4 KiB instead of per record, and binary search over them finds the only block
  that can contain a key.
* **Footers** re-check the header CRC. Together with atomic publish this is the
  difference between "a file is either complete or absent" and "a file exists but
  nobody agrees how long it is".

Publishing
----------
Nothing is written in place. The writer assembles the complete image, then hands
it to :func:`sable.fsutil.atomic_write_bytes`, whose temp → fsync → rename →
directory-fsync sequence guarantees a reader never observes a partial file.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

from sable.bloom import BloomFilter
from sable.errors import CorruptSSTableError, InvalidArgumentError
from sable.fsutil import ATOMIC_TMP_SUFFIX, atomic_write_bytes
from sable.types import EngineOptions, InternalEntry, InternalKey, KeyRange, OpType, SSTableMeta

__all__ = [
    "FILE_MAGIC",
    "RECORD_HEADER_SIZE",
    "FILE_VERSION",
    "FOOTER_SIZE",
    "HEADER_SIZE",
    "SSTableReader",
    "SSTableWriter",
]

FILE_MAGIC: bytes = b"SABLESST"
FILE_VERSION: int = 1

# magic | version | flags | entry/block counts | seq span | four offsets |
# two sizes | two key lengths. Tightly packed: '<' precludes alignment padding.
HEADER_STRUCT = struct.Struct("<8sHHQQQQQQQQQQII")
HEADER_SIZE = HEADER_STRUCT.size

# magic | crc32 of header+key spans | reserved
FOOTER_STRUCT = struct.Struct("<8sII")
FOOTER_SIZE = FOOTER_STRUCT.size

# klen | vlen | op | sequence, then key bytes and value bytes.
_RECORD_STRUCT = struct.Struct("<IIBQ")
RECORD_HEADER_SIZE = _RECORD_STRUCT.size

# block_offset | block_size | first_key_len | block_crc32, then key bytes.
_INDEX_ROW_HEADER = struct.Struct("<QIII")
_INDEX_ROW_HEADER_SIZE = _INDEX_ROW_HEADER.size

FLAG_HAS_FILTER: int = 0x1


class SSTableWriter:
    """Accumulates sorted entries and publishes one immutable file.

    Input must arrive in internal-key order (user key ascending, sequence
    descending). Ordering is what makes the file binary-searchable, so a
    violation is rejected rather than silently producing a corrupt index.
    """

    def __init__(
        self,
        target: Path,
        options: EngineOptions | None = None,
        *,
        file_id: int = 0,
        level: int = 0,
    ) -> None:
        self._target = Path(target)
        self._options = options or EngineOptions()
        self._file_id = file_id
        self._level = level

        self._blocks: list[bytes] = []
        self._open_records: list[bytes] = []
        self._open_bytes = 0
        self._index_rows: list[tuple[int, int, int, bytes]] = []
        self._filter_keys: list[bytes] = []

        self._num_entries = 0
        self._smallest_key: bytes | None = None
        self._largest_key: bytes | None = None
        self._min_sequence = -1
        self._max_sequence = -1
        self._previous_sort_key: tuple[bytes, int] | None = None
        self._finished = False

    def add_entry(self, entry: InternalEntry) -> None:
        """Stage one entry.

        Args:
            entry: Mutation to serialise, arriving in internal-key order.

        Raises:
            InvalidArgumentError: The entry regressed against the previous one,
                which would otherwise produce an index that silently lies.
        """
        if self._finished:
            raise InvalidArgumentError("writer already finished")

        sort_key = entry.sort_key
        if self._previous_sort_key is not None and sort_key <= self._previous_sort_key:
            raise InvalidArgumentError(
                f"entry for {entry.user_key!r} breaks internal-key ordering; "
                "SSTables require user key ascending, sequence descending"
            )
        self._previous_sort_key = sort_key

        record = (
            _RECORD_STRUCT.pack(
                len(entry.user_key), len(entry.value), int(entry.op), entry.sequence
            )
            + entry.user_key
            + entry.value
        )
        self._open_records.append(record)
        self._open_bytes += len(record)

        self._num_entries += 1
        self._filter_keys.append(entry.user_key)
        if self._smallest_key is None:
            self._smallest_key = entry.user_key
        self._largest_key = entry.user_key
        self._min_sequence = entry.sequence if self._min_sequence < 0 else min(
            self._min_sequence, entry.sequence
        )
        self._max_sequence = max(self._max_sequence, entry.sequence)

        if self._open_bytes >= self._options.block_size_bytes:
            self._seal_block()

    def finish(self) -> SSTableMeta:
        """Serialise everything and publish atomically.

        Returns:
            Metadata describing exactly what landed on disk.

        Raises:
            InvalidArgumentError: Nothing was ever added.
            OSError: Propagated from the publish sequence after cleanup, so a
                failed write leaves neither a target nor a temporary sibling.
        """
        if self._finished:
            raise InvalidArgumentError("writer already finished")
        if self._num_entries == 0:
            raise InvalidArgumentError("refusing to publish an empty SSTable")
        if self._open_records:
            self._seal_block()

        smallest = self._smallest_key or b""
        largest = self._largest_key or b""
        data_offset = HEADER_SIZE + len(smallest) + len(largest)
        data_size = sum(len(block) for block in self._blocks)
        index_offset = data_offset + data_size
        index_block = self._serialise_index(data_offset)
        filter_blob = self._serialise_filter()
        filter_offset = index_offset + len(index_block)
        flags = FLAG_HAS_FILTER if filter_blob else 0

        header = HEADER_STRUCT.pack(
            FILE_MAGIC,
            FILE_VERSION,
            flags,
            self._num_entries,
            len(self._blocks),
            max(self._min_sequence, 0),
            max(self._max_sequence, 0),
            data_offset,
            data_size,
            index_offset,
            len(index_block),
            filter_offset,
            len(filter_blob),
            len(smallest),
            len(largest),
        )
        head = header + smallest + largest
        footer = FOOTER_STRUCT.pack(FILE_MAGIC, zlib.crc32(head) & 0xFFFFFFFF, 0)
        payload = head + b"".join(self._blocks) + index_block + filter_blob + footer

        written = atomic_write_bytes(self._target, payload)
        self._finished = True

        return SSTableMeta(
            file_id=self._file_id,
            level=self._level,
            file_size=written,
            num_entries=self._num_entries,
            num_blocks=len(self._blocks),
            smallest_key=smallest,
            largest_key=largest,
            min_sequence=max(self._min_sequence, 0),
            max_sequence=max(self._max_sequence, 0),
        )

    def _seal_block(self) -> None:
        """Close the current block, recording its offset *relative* to the data region.

        The absolute offset is only knowable in :meth:`finish`, once the final
        key spans (and therefore ``data_offset``) are settled.
        """
        raw = b"".join(self._open_records)
        relative_offset = sum(len(block) for block in self._blocks)
        first_key = self._first_key_of(raw)
        self._index_rows.append(
            (relative_offset, len(raw), zlib.crc32(raw) & 0xFFFFFFFF, first_key)
        )
        self._blocks.append(raw)
        self._open_records = []
        self._open_bytes = 0

    @staticmethod
    def _first_key_of(block: bytes) -> bytes:
        klen, _vlen, _op, _seq = _RECORD_STRUCT.unpack_from(block, 0)
        return block[RECORD_HEADER_SIZE : RECORD_HEADER_SIZE + klen]

    def _serialise_filter(self) -> bytes:
        """Build the Bloom block, or an empty image when filtering is disabled."""
        if not self._filter_keys or self._options.bloom_false_positive_rate <= 0.0:
            return b""
        filter = BloomFilter.with_capacity(
            len(self._filter_keys), self._options.bloom_false_positive_rate
        )
        for key in self._filter_keys:
            filter.add(key)
        return filter.to_bytes()

    def _serialise_index(self, data_offset: int) -> bytes:
        """Serialise the sparse index, rebasing relative offsets to absolute ones."""
        rows: list[bytes] = []
        for relative_offset, size, crc, first_key in self._index_rows:
            rows.append(
                _INDEX_ROW_HEADER.pack(data_offset + relative_offset, size, len(first_key), crc)
                + first_key
            )
        return b"".join(rows)


class SSTableReader:
    """Random- and range-access over one published SSTable file."""

    def __init__(self, path: Path, *, verify_checksums: bool = True) -> None:
        self._path = Path(path)
        self._verify = verify_checksums
        self._handle = self._path.open("rb")
        self._closed = False
        self._data_block_reads = 0
        self._index_cache: list[tuple[int, int, int, bytes]] | None = None
        self._bloom: BloomFilter | None = None
        self._bloom_rejections = 0
        self._filter_loaded = False
        self._header_flags = 0
        self._header_filter_offset = 0
        self._header_filter_size = 0
        self._meta = self._load_header()

    @classmethod
    def open(cls, path: Path, *, verify_checksums: bool = True) -> SSTableReader:
        """Open an existing table for reading.

        Args:
            path: File published by :class:`SSTableWriter`.
            verify_checksums: Validate each data block's CRC as it is read.

        Returns:
            A reader whose :meth:`close` the caller owns.
        """
        return cls(path, verify_checksums=verify_checksums)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def meta(self) -> SSTableMeta:
        return self._meta

    @property
    def data_block_reads(self) -> int:
        """Number of data-block fetches performed; the unit of read cost."""
        return self._data_block_reads

    @property
    def bloom_rejections(self) -> int:
        """Reads pruned without touching any data block."""
        return self._bloom_rejections

    @property
    def has_bloom_filter(self) -> bool:
        """True once a filter has been loaded; triggers the lazy read if needed."""
        self._ensure_filter()
        return self._bloom is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def maybe_contains_key(self, user_key: bytes) -> bool:
        """Probabilistic membership test; a ``False`` result is always certain.

        Args:
            user_key: Key to test.

        Returns:
            ``False`` guarantees absence. ``True`` means the key may be present
            and still requires reading the containing block.
        """
        self._ensure_filter()
        if self._bloom is None:
            return True
        present = self._bloom.may_contain(user_key)
        if not present:
            self._bloom_rejections += 1
        return present

    def close(self) -> None:
        """Release the descriptor. Idempotent."""
        if self._closed:
            return
        self._handle.close()
        self._closed = True

    def covers_key(self, user_key: bytes) -> bool:
        """Cheap membership test from the key span alone."""
        return self._meta.covers(user_key)

    def get_entry(self, user_key: bytes) -> InternalEntry | None:
        """Return the newest version of ``user_key`` held by this file.

        Args:
            user_key: Exact key to resolve.

        Returns:
            The first matching record (records are ordered newest-first within a
            key), or ``None`` when the file cannot contain it.
        """
        if not self.covers_key(user_key):
            return None
        if not self.maybe_contains_key(user_key):
            return None
        block_index = self._locate_block(user_key)
        if block_index < 0:
            return None
        for entry in self._iter_records(block_index):
            if entry.user_key == user_key:
                return entry
            if entry.user_key > user_key:
                return None
        return None

    def iter_slice(self, key_range: KeyRange) -> Iterator[InternalEntry]:
        """Yield every record intersecting ``key_range``, in internal-key order.

        Args:
            key_range: Window to scan; unbounded is allowed.
        """
        if not self._meta.num_blocks:
            return
        start = 0
        if key_range.start is not None:
            start = self._locate_block(key_range.start)
            if start < 0:
                start = 0
        for block_index in range(start, self._meta.num_blocks):
            for entry in self._iter_records(block_index):
                if key_range.contains(entry.user_key):
                    yield entry
                elif key_range.end is not None and entry.user_key >= key_range.end:
                    return

    def iter_all(self) -> Iterator[InternalEntry]:
        """Yield every record in the file."""
        for block_index in range(self._meta.num_blocks):
            yield from self._iter_records(block_index)

    # -- internals ---------------------------------------------------------

    def _load_header(self) -> SSTableMeta:
        size = self._path.stat().st_size
        if size < HEADER_SIZE + FOOTER_SIZE:
            raise CorruptSSTableError(f"{self._path.name} is too small to be a table")

        head_prefix = self._read_at(0, HEADER_SIZE)
        fields = HEADER_STRUCT.unpack(head_prefix)
        magic, version = fields[0], fields[1]
        if magic != FILE_MAGIC:
            raise CorruptSSTableError(f"{self._path.name} is not a sable SSTable")
        if version != FILE_VERSION:
            raise CorruptSSTableError(
                f"{self._path.name} uses format version {version}, expected {FILE_VERSION}"
            )

        smallest_len, largest_len = fields[13], fields[14]
        spans = self._read_at(HEADER_SIZE, smallest_len + largest_len)
        head = head_prefix + spans
        self._header_flags = fields[2]
        self._header_filter_offset = fields[11]
        self._header_filter_size = fields[12]
        footer = self._read_at(size - FOOTER_SIZE, FOOTER_SIZE)
        footer_magic, header_crc, _reserved = FOOTER_STRUCT.unpack(footer)
        if footer_magic != FILE_MAGIC or header_crc != zlib.crc32(head) & 0xFFFFFFFF:
            raise CorruptSSTableError(f"{self._path.name} failed header CRC verification")

        return SSTableMeta(
            file_id=int(self._path.stem),
            level=-1,
            file_size=size,
            num_entries=fields[3],
            num_blocks=fields[4],
            smallest_key=spans[:smallest_len],
            largest_key=spans[smallest_len:],
            min_sequence=fields[5],
            max_sequence=fields[6],
        )

    def _index_rows(self) -> list[tuple[int, int, int, bytes]]:
        head_prefix = self._read_at(0, HEADER_SIZE)
        fields = HEADER_STRUCT.unpack(head_prefix)
        index_offset, index_size = fields[9], fields[10]
        blob = self._read_at(index_offset, index_size)

        rows: list[tuple[int, int, int, bytes]] = []
        position = 0
        while position < len(blob):
            offset, size, key_len, crc = _INDEX_ROW_HEADER.unpack_from(blob, position)
            key_start = position + _INDEX_ROW_HEADER_SIZE
            first_key = blob[key_start : key_start + key_len]
            rows.append((offset, size, crc, first_key))
            position = key_start + key_len
        return rows

    def _ensure_filter(self) -> None:
        """Load the Bloom block lazily; it is small and shared by every probe."""
        if self._filter_loaded:
            return
        self._filter_loaded = True
        if not self._header_flags & FLAG_HAS_FILTER:
            return
        size = self._path.stat().st_size
        start = self._header_filter_offset
        end = min(size - FOOTER_SIZE, start + self._header_filter_size)
        if start <= 0 or end <= start or start >= size:
            return
        self._bloom = BloomFilter.from_bytes(self._read_at(start, end - start))

    def _ensure_index(self) -> list[tuple[int, int, int, bytes]]:
        """Load and memoise the index block; it is immutable once published."""
        if self._index_cache is None:
            self._index_cache = self._index_rows()
        return self._index_cache

    def _locate_block(self, user_key: bytes) -> int:
        """Index of the last block whose first key is ``<= user_key``."""
        rows = self._ensure_index()
        low, high, found = 0, len(rows) - 1, -1
        while low <= high:
            mid = (low + high) // 2
            if rows[mid][3] <= user_key:
                found = mid
                low = mid + 1
            else:
                high = mid - 1
        return found

    def _iter_records(self, block_index: int) -> Iterator[InternalEntry]:
        rows = self._ensure_index()
        if block_index >= len(rows):
            return
        offset, size, crc, _first_key = rows[block_index]
        raw = self._read_block(offset, size, crc)
        position = 0
        while position < size:
            klen, vlen, op_raw, sequence = _RECORD_STRUCT.unpack_from(raw, position)
            body = position + RECORD_HEADER_SIZE
            key = raw[body : body + klen]
            value = raw[body + klen : body + klen + vlen]
            yield InternalEntry(InternalKey(key, sequence, OpType(op_raw)), value)
            position = body + klen + vlen

    def _read_block(self, offset: int, size: int, expected_crc: int) -> bytes:
        raw = self._read_at(offset, size)
        self._data_block_reads += 1
        if self._verify and zlib.crc32(raw) & 0xFFFFFFFF != expected_crc:
            raise CorruptSSTableError(
                f"block at offset {offset} in {self._path.name} failed CRC32 verification"
            )
        return raw

    def _read_at(self, offset: int, size: int) -> bytes:
        self._handle.seek(offset)
        chunk = self._handle.read(size)
        if len(chunk) != size:
            raise CorruptSSTableError(
                f"{self._path.name} ended early at offset {offset} ({size} bytes requested)"
            )
        return chunk

    def __enter__(self) -> SSTableReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def looks_like_temporary(path: Path) -> bool:
    """True for writer scratch files that must never be mistaken for tables."""
    return path.name.endswith(ATOMIC_TMP_SUFFIX)
