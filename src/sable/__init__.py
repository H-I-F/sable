"""``sable`` — a crash-safe, pure-Python LSM-tree key-value storage engine.

This package is a reference implementation: chronological WAL, sorted memtable,
immutable binary SSTables, Bloom-filtered reads and generation-counted
compaction, with every durability decision pinned to explicit filesystem
invariants rather than to incidental runtime behaviour.

The top-level namespace intentionally exposes only the durable domain
vocabulary. Heavyweight subsystems are imported from their own modules so that
embedding stays cheap:

    >>> from sable.types import InternalEntry
    >>> entry = InternalEntry.put(b"alpha", sequence=1, value=b"one")
    >>> entry.is_tombstone
    False
    >>> entry.sort_key == (b"alpha", -1)
    True
"""

from __future__ import annotations

from sable.engine import Engine
from sable.errors import (
    ChecksumMismatchError,
    ClosedEngineError,
    CorruptionError,
    CorruptManifestError,
    CorruptSSTableError,
    CorruptWALError,
    InvalidArgumentError,
    SableError,
    TornWriteError,
)
from sable.types import (
    EngineOptions,
    EngineStats,
    InternalEntry,
    InternalKey,
    KeyRange,
    OpType,
    ReadOptions,
    WriteOptions,
)

__version__ = "0.1.1"

__all__ = [
    "ChecksumMismatchError",
    "ClosedEngineError",
    "CorruptManifestError",
    "CorruptSSTableError",
    "CorruptWALError",
    "CorruptionError",
    "Engine",
    "EngineOptions",
    "EngineStats",
    "InternalEntry",
    "InternalKey",
    "InvalidArgumentError",
    "KeyRange",
    "OpType",
    "ReadOptions",
    "SableError",
    "TornWriteError",
    "WriteOptions",
    "__version__",
]
