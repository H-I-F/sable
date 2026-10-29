"""Domain primitives shared by every ``sable`` subsystem.

The types declared here form the narrow waist of the engine: the WAL, the
memtable, the SSTable writer/reader and the compaction planner all agree on
these representations, which keeps every byte-level layout inspectable from a
single vantage point.

Ordering contract
-----------------
Every entry carries a monotonically increasing sequence number. Compound
("internal") keys sort by ``user_key`` ascending and ``sequence`` descending so
that the newest version of a key is always encountered first during a merge
walk. This is the invariant that makes tombstones shadow elder writes without
any deletion bookkeeping in the memtable itself.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_KEY_BYTES",
    "MAX_VALUE_BYTES",
    "OpType",
    "InternalKey",
    "InternalEntry",
    "KeyRange",
    "ReadOptions",
    "WriteOptions",
    "EngineOptions",
    "EngineStats",
]

MAX_KEY_BYTES: Final = 1 << 20  # 1 MiB
MAX_VALUE_BYTES: Final = 1 << 30  # 1 GiB


class OpType(enum.IntEnum):
    """Discriminates a live value from a deletion marker inside one byte."""

    PUT = 0
    DELETE = 1


@dataclass(frozen=True, slots=True)
class InternalKey:
    """``user_key`` plus MVCC metadata; the unit of ordering inside the engine."""

    user_key: bytes
    sequence: int
    op: OpType = OpType.PUT

    def __post_init__(self) -> None:
        if not isinstance(self.user_key, (bytes, bytearray)):
            raise TypeError("user_key must be bytes-like")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")

    @property
    def sort_key(self) -> tuple[bytes, int]:
        """Total order used by memtable iterators and SSTable blocks.

        Returns:
            Tuple of ``(user_key, negated_sequence)`` so that equal user keys
            collapse with the highest sequence first.
        """
        return (bytes(self.user_key), -self.sequence)

    def encoded_size(self) -> int:
        return len(self.user_key)


@dataclass(frozen=True, slots=True)
class InternalEntry:
    """A single versioned mutation flowing through every engine stage."""

    key: InternalKey
    value: bytes = b""

    def __post_init__(self) -> None:
        if self.key.op is OpType.DELETE and self.value:
            raise ValueError("tombstones must not carry a value")

    @classmethod
    def put(cls, user_key: bytes, sequence: int, value: bytes) -> InternalEntry:
        if len(user_key) > MAX_KEY_BYTES:
            raise ValueError(f"key exceeds {MAX_KEY_BYTES} bytes")
        if len(value) > MAX_VALUE_BYTES:
            raise ValueError(f"value exceeds {MAX_VALUE_BYTES} bytes")
        return cls(InternalKey(user_key, sequence, OpType.PUT), bytes(value))

    @classmethod
    def delete(cls, user_key: bytes, sequence: int) -> InternalEntry:
        if len(user_key) > MAX_KEY_BYTES:
            raise ValueError(f"key exceeds {MAX_KEY_BYTES} bytes")
        return cls(InternalKey(user_key, sequence, OpType.DELETE), b"")

    @property
    def user_key(self) -> bytes:
        return self.key.user_key

    @property
    def sequence(self) -> int:
        return self.key.sequence

    @property
    def op(self) -> OpType:
        return self.key.op

    @property
    def sort_key(self) -> tuple[bytes, int]:
        return self.key.sort_key

    @property
    def is_tombstone(self) -> bool:
        return self.key.op is OpType.DELETE

    def heap_bytes(self) -> int:
        """Approximate resident cost; used for memtable flush accounting."""
        return len(self.key.user_key) + len(self.value) + 32


@dataclass(frozen=True, slots=True)
class KeyRange:
    """Half-open-by-default user-key window used by scans and file overlap tests."""

    start: bytes | None = None
    end: bytes | None = None
    start_inclusive: bool = True
    end_exclusive: bool = True

    def contains(self, user_key: bytes) -> bool:
        if self.start is not None:
            if self.start_inclusive:
                if user_key < self.start:
                    return False
            elif user_key <= self.start:
                return False
        if self.end is not None:
            if self.end_exclusive:
                if user_key >= self.end:
                    return False
            elif user_key > self.end:
                return False
        return True

    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None

    def overlaps(self, min_key: bytes, max_key: bytes) -> bool:
        """True when ``[min_key, max_key]`` intersects this window."""
        if self.start is not None and max_key < self.start:
            return False
        if self.end is not None:
            if self.end_exclusive and min_key >= self.end:
                return False
            if not self.end_exclusive and min_key > self.end:
                return False
        return True


@dataclass(frozen=True, slots=True)
class ReadOptions:
    """Per-read knobs. Never affects durability, only verification strictness."""

    verify_checksums: bool = True


@dataclass(frozen=True, slots=True)
class WriteOptions:
    """Per-write knobs. ``sync`` decides whether the WAL hits stable storage."""

    sync: bool = True


@dataclass(frozen=True, slots=True)
class EngineOptions:
    """Engine-wide configuration resolved once at open time."""

    create_if_missing: bool = True
    error_if_exists: bool = False
    memtable_bytes_threshold: int = 4 << 20
    default_write_sync: bool = True
    level0_slowdown_trigger: int = 8
    level0_file_num_compaction_trigger: int = 4
    max_levels: int = 6
    level_size_multiplier: int = 10
    l1_bytes_trigger: int = 8 << 20
    block_size_bytes: int = 4096
    block_restart_interval: int = 16
    bloom_false_positive_rate: float = 0.01

    def __post_init__(self) -> None:
        if self.memtable_bytes_threshold <= 0:
            raise ValueError("memtable_bytes_threshold must be positive")
        if self.max_levels < 2:
            raise ValueError("max_levels must be at least 2")
        if self.level_size_multiplier < 1:
            raise ValueError("level_size_multiplier must be >= 1")
        if not 0.0 < self.bloom_false_positive_rate < 1.0:
            raise ValueError("bloom_false_positive_rate must be in (0, 1)")

    def target_bytes_for_level(self, level: int) -> int:
        """Capacity budget of ``level`` under the leveled size multiplier."""
        if level <= 0:
            return self.l1_bytes_trigger
        budget: int = self.l1_bytes_trigger * (self.level_size_multiplier**level)
        return budget


@dataclass(slots=True)
class EngineStats:
    """Mutable counter block surfaced by the CLI and the benchmark harness."""

    puts: int = 0
    deletes: int = 0
    gets: int = 0
    get_hits: int = 0
    scans: int = 0
    flushes: int = 0
    compactions: int = 0
    bloom_rejections: int = 0
    wal_bytes_written: int = 0
    sstable_bytes_written: int = 0

    def copy(self) -> EngineStats:
        return EngineStats(
            puts=self.puts,
            deletes=self.deletes,
            gets=self.gets,
            get_hits=self.get_hits,
            scans=self.scans,
            flushes=self.flushes,
            compactions=self.compactions,
            bloom_rejections=self.bloom_rejections,
            wal_bytes_written=self.wal_bytes_written,
            sstable_bytes_written=self.sstable_bytes_written,
        )
