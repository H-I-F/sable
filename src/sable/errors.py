"""Error taxonomy for the ``sable`` engine.

The hierarchy is deliberately shallow so callers can choose granularity: catch
``SableError`` for anything engine-owned, or catch``CorruptionError`` when the
response is to quarantine and rebuild from the WAL.
"""

from __future__ import annotations

__all__ = [
    "SableError",
    "InvalidArgumentError",
    "ClosedEngineError",
    "CorruptionError",
    "ChecksumMismatchError",
    "CorruptWALError",
    "CorruptSSTableError",
    "CorruptManifestError",
    "TornWriteError",
]


class SableError(Exception):
    """Base class for every error raised by the engine."""


class InvalidArgumentError(SableError, ValueError):
    """Caller supplied a key, value or option outside the supported envelope."""


class ClosedEngineError(SableError, RuntimeError):
    """Operation attempted after :meth:`sable.engine.Engine.close`."""


class CorruptionError(SableError):
    """On-disk bytes violated a structural or checksum invariant."""


class ChecksumMismatchError(CorruptionError):
    """A frame-level CRC32 did not match its payload."""


class CorruptWALError(CorruptionError):
    """Write-ahead log stream contains an unparseable mid-stream frame."""


class TornWriteError(CorruptionError):
    """Trailing frame was partially written; recoverable via truncation."""


class CorruptSSTableError(CorruptionError):
    """SSTable header, block or footer invariants were violated."""


class CorruptManifestError(CorruptionError):
    """MANIFEST record stream is truncated or fails its checksum."""
