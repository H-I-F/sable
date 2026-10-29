"""Chronological write-ahead log: frame layout, integrity and torn-tail recovery.

Frame layout (little-endian, tightly packed, no padding)::

    +----------+----------+---------+-----------+-----------+------+-------+
    | crc32    | sequence | op      | klen      | vlen      | key  | value |
    | uint32   | uint64   | uint8   | uint32    | uint32    | klen | vlen  |
    +----------+----------+---------+-----------+-----------+------+-------+
      4 bytes    8 bytes    1 byte    4 bytes     4 bytes

The 21-byte ``<IQBII`` header is fully self-describing: given any byte offset,
the reader can determine exactly where the next frame begins without consulting
an external index. ``crc32`` covers everything *after* itself (sequence..value),
so a flipped bit anywhere in the payload is detectable.

Recovery semantics
------------------
A crash can leave a partially written trailing frame. Because the engine follows
*append-before-apply*, any frame that did not get fsync-ed was never
acknowledged to a caller, so discarding it preserves the durability contract
rather than violating it. The scanner therefore distinguishes two situations:

* **Truncated tail** — fewer bytes remain than the header/klen/vlen advertise.
  Unambiguously a partial write; scanning stops and the bytes are droppable.
* **Bad CRC in a non-terminal frame** — real corruption, because surviving
  frames follow it. The engine refuses to guess and raises
  :class:`~sable.errors.ChecksumMismatchError`.
* **Bad CRC in the terminal frame** — indistinguishable from a partial write
  that happened to have its length advertised faithfully, so it is treated as a
  torn tail. ``strict_tail_crc=True`` opts into failing loudly instead.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sable.errors import (
    ChecksumMismatchError,
    ClosedEngineError,
    CorruptWALError,
    InvalidArgumentError,
)
from sable.fsutil import fsync_directory
from sable.types import MAX_KEY_BYTES, MAX_VALUE_BYTES, InternalEntry, InternalKey, OpType

__all__ = [
    "FRAME_HEADER_SIZE",
    "RecoveryResult",
    "WriteAheadLog",
    "encode_frame",
    "max_sequence",
    "scan_wal_bytes",
]

# crc32 | sequence | op | klen | vlen — no alignment padding with "<".
FRAME_HEADER = struct.Struct("<IQBII")
# sequence | op | klen | vlen — the CRC-covered tail of the header.
FRAME_TAIL_STRUCT = struct.Struct("<QBII")
FRAME_HEADER_SIZE = FRAME_HEADER.size


def encode_frame(entry: InternalEntry) -> bytes:
    """Serialize one entry into a self-describing WAL frame.

    Args:
        entry: Mutation to serialize.

    Returns:
        Header followed by key and value bytes, with the leading CRC32 already
        computed over the remainder of the frame.
    """
    key = entry.user_key
    value = entry.value
    tail = FRAME_TAIL_STRUCT.pack(entry.sequence, int(entry.op), len(key), len(value))
    crc = zlib.crc32(tail + key + value) & 0xFFFFFFFF
    return FRAME_HEADER.pack(crc, entry.sequence, int(entry.op), len(key), len(value)) + key + value


@dataclass(slots=True)
class RecoveryResult:
    """Outcome of replaying a WAL file.

    Attributes:
        entries: Every fully verified frame, in chronological order.
        valid_bytes: Offset immediately past the last verified frame.
        discarded_bytes: Bytes dropped from the tail (partial or unverifiable).
        torn_tail: True when the stream ended in a partial write. A well-shut
            down database never reports this.
        truncated_file: True when truncation actually rewrote the file.
    """

    entries: list[InternalEntry] = field(default_factory=list)
    valid_bytes: int = 0
    discarded_bytes: int = 0
    torn_tail: bool = False
    truncated_file: bool = False

    @property
    def frame_count(self) -> int:
        return len(self.entries)


def scan_wal_bytes(data: bytes, *, strict_tail_crc: bool = False) -> RecoveryResult:
    """Validate a WAL image and return every frame that survived intact.

    Args:
        data: Complete byte image of a WAL segment.
        strict_tail_crc: When True, a CRC mismatch in the final frame raises
            instead of being classified as a torn tail. Useful for auditing a
            file whose producer is known to always publish complete frames.

    Returns:
        A :class:`RecoveryResult`. The caller is expected to treat any
        ``discarded_bytes`` as never-acknowledged data.

    Raises:
        ChecksumMismatchError: A frame whose CRC disagrees with its payload and
            is followed by further frames, or a terminal-frame mismatch under
            ``strict_tail_crc``.
        CorruptWALError: Header fields are structurally impossible (declared
            length beyond limits, unknown op byte).
    """
    result = RecoveryResult()
    offset = 0
    total = len(data)

    while offset < total:
        remaining = total - offset
        if remaining < FRAME_HEADER_SIZE:
            result.discarded_bytes += remaining
            result.torn_tail = True
            break

        stored_crc, sequence, op_raw, klen, vlen = FRAME_HEADER.unpack_from(data, offset)
        if klen > MAX_KEY_BYTES or vlen > MAX_VALUE_BYTES:
            raise CorruptWALError(
                f"frame at offset {offset} declares impossible lengths "
                f"(klen={klen}, vlen={vlen})"
            )
        if op_raw not in (int(OpType.PUT), int(OpType.DELETE)):
            raise CorruptWALError(f"frame at offset {offset} has unknown op byte {op_raw}")

        frame_bytes = FRAME_HEADER_SIZE + klen + vlen
        if remaining < frame_bytes:
            result.discarded_bytes += remaining
            result.torn_tail = True
            break

        actual_crc = zlib.crc32(data[offset + 4 : offset + frame_bytes]) & 0xFFFFFFFF
        if actual_crc != stored_crc:
            is_terminal = offset + frame_bytes == total
            if not is_terminal or strict_tail_crc:
                raise ChecksumMismatchError(
                    f"frame at offset {offset} failed CRC32 "
                    f"(stored={stored_crc:#010x} computed={actual_crc:#010x}, "
                    f"terminal={is_terminal})"
                )
            result.discarded_bytes += remaining
            result.torn_tail = True
            break

        key_start = offset + FRAME_HEADER_SIZE
        key = data[key_start : key_start + klen]
        value_start = key_start + klen
        value = data[value_start : value_start + vlen]
        result.entries.append(InternalEntry(InternalKey(key, sequence, OpType(op_raw)), value))
        offset += frame_bytes

    result.valid_bytes = offset
    return result


def max_sequence(entries: Sequence[InternalEntry]) -> int:
    """Return the next sequence number to allocate after replaying ``entries``.

    Args:
        entries: Frames recovered from the log, chronologically ordered.

    Returns:
        ``max(sequence) + 1``, or ``0`` when there is nothing to replay. Sequence
        numbers are zero-based, so an empty log starts at 0.
    """
    if not entries:
        return 0
    return max(entry.sequence for entry in entries) + 1


class WriteAheadLog:
    """Append-only durability log for memtable mutations.

    Every acknowledged write passes through :meth:`append` *before* it becomes
    visible in the memtable (append-before-apply). With syncing enabled the
    frame reaches stable storage before the call returns, which is what lets a
    ``SIGKILL``-based test assert that acknowledged data is always replayable.
    """

    def __init__(self, path: Path, *, sync: bool = True) -> None:
        self._path = Path(path)
        self._sync = sync
        self._closed = False
        self._frames_appended = 0
        self._bytes_appended = 0
        self._handle = self._path.open("ab")

    @classmethod
    def open(cls, path: Path, *, sync: bool = True) -> WriteAheadLog:
        """Create-or-append to ``path`` and return a ready log handle."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(path, sync=sync)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def frames_appended(self) -> int:
        return self._frames_appended

    @property
    def bytes_appended(self) -> int:
        return self._bytes_appended

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, entry: InternalEntry, sync: bool | None = None) -> int:
        """Append one frame.

        Args:
            entry: Mutation to persist.
            sync: Overrides the instance default. When true, the frame is
                flushed and fsync-ed before returning, which is precisely what
                "acknowledged" means in this engine.

        Returns:
            Number of bytes committed for this frame.

        Raises:
            ClosedEngineError: The log was already closed.
            InvalidArgumentError: The entry exceeds the supported envelope.
        """
        if self._closed:
            raise ClosedEngineError("WAL is closed")
        if len(entry.user_key) > MAX_KEY_BYTES or len(entry.value) > MAX_VALUE_BYTES:
            raise InvalidArgumentError("entry exceeds supported key/value size")

        frame = encode_frame(entry)
        self._handle.write(frame)
        self._frames_appended += 1
        self._bytes_appended += len(frame)

        if self._sync if sync is None else sync:
            self.sync()
        return len(frame)

    def sync(self) -> None:
        """Flush Python buffers and push them to stable storage."""
        if self._closed:
            raise ClosedEngineError("WAL is closed")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        """Flush, fsync, close the descriptor and fsync the containing directory."""
        if self._closed:
            return
        self.sync()
        self._handle.close()
        self._closed = True
        fsync_directory(self._path.parent)

    @staticmethod
    def recover(path: Path, *, strict_tail_crc: bool = False) -> RecoveryResult:
        """Scan an existing log without mutating it.

        Args:
            path: Segment to inspect; a missing file yields an empty result.
            strict_tail_crc: Forwarded to :func:`scan_wal_bytes`.

        Returns:
            Populated :class:`RecoveryResult`.
        """
        path = Path(path)
        if not path.exists():
            return RecoveryResult()
        return scan_wal_bytes(path.read_bytes(), strict_tail_crc=strict_tail_crc)

    @staticmethod
    def truncate_torn_tail(path: Path, valid_bytes: int) -> int:
        """Drop the unrecoverable tail of ``path`` in place.

        Args:
            path: Segment to repair.
            valid_bytes: Byte offset to retain, as reported by a prior scan.

        Returns:
            Bytes actually removed.
        """
        path = Path(path)
        size = path.stat().st_size
        if valid_bytes >= size:
            return 0
        with path.open("r+b") as handle:
            handle.truncate(valid_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        return size - valid_bytes

    def __enter__(self) -> WriteAheadLog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
