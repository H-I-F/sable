"""WAL frame layout, integrity verification and torn-tail recovery.

These tests pin down the contract every later durability claim rests on:
whatever :meth:`WriteAheadLog.append` returned from was fsync-ed, and therefore
must reappear during replay even if the process died one instruction later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import count_fsyncs, entry, flip_bit, key_for, sequential_puts, value_for, wal_path
from sable.errors import ChecksumMismatchError, ClosedEngineError, CorruptWALError
from sable.types import InternalEntry, OpType
from sable.wal import (
    FRAME_HEADER_SIZE,
    WriteAheadLog,
    encode_frame,
    max_sequence,
    scan_wal_bytes,
)


def build_image(count: int) -> bytes:
    return b"".join(encode_frame(item) for item in sequential_puts(count))


def test_frame_layout_is_self_describing() -> None:
    frame = encode_frame(InternalEntry.put(b"alpha", 3, b"beta"))

    assert len(frame) == FRAME_HEADER_SIZE + len(b"alpha") + len(b"beta")
    # 21-byte header: crc32 | sequence | op | klen | vlen, little-endian, no padding.
    assert FRAME_HEADER_SIZE == 21
    assert int.from_bytes(frame[4:12], "little") == 3
    assert frame[12] == int(OpType.PUT)
    assert int.from_bytes(frame[13:17], "little") == len(b"alpha")
    assert int.from_bytes(frame[17:21], "little") == len(b"beta")
    assert frame[21:26] == b"alpha"
    assert frame[26:] == b"beta"


def test_roundtrip_preserves_order_values_and_ops() -> None:
    frames = [
        InternalEntry.put(b"a", 0, b"1"),
        InternalEntry.delete(b"b", 1),
        InternalEntry.put(b"c", 2, b""),
    ]

    image = b"".join(encode_frame(frame) for frame in frames)
    result = scan_wal_bytes(image)

    assert [item.user_key for item in result.entries] == [b"a", b"b", b"c"]
    assert [item.sequence for item in result.entries] == [0, 1, 2]
    assert [item.op for item in result.entries] == [OpType.PUT, OpType.DELETE, OpType.PUT]
    assert result.entries[2].value == b""
    assert not result.torn_tail
    assert result.discarded_bytes == 0


def test_recovery_returns_three_records_when_fourth_is_truncated() -> None:
    """Mandated case: three durable frames plus a partial fourth replay cleanly."""
    image = build_image(4)
    # Cut inside the fourth frame's payload, leaving its key partially written.
    torn = image[: len(image) - 3]

    result = scan_wal_bytes(torn)

    assert result.frame_count == 3, "only fully written frames may be replayed"
    assert result.torn_tail is True
    assert result.discarded_bytes == len(torn) - result.valid_bytes
    assert [item.sequence for item in result.entries] == [0, 1, 2]


def test_truncation_mid_header_is_treated_as_torn_tail() -> None:
    image = build_image(2)
    complete_frame = FRAME_HEADER_SIZE + len(key_for(0)) + len(value_for(0))

    # Leave the second frame with only 4 bytes of its 21-byte header.
    result = scan_wal_bytes(image[: complete_frame + 4])

    assert result.frame_count == 1, "a trailing header fragment carries no frame"
    assert result.torn_tail is True
    assert result.discarded_bytes == 4


def test_truncated_file_can_be_repaired_idempotently(tmp_path: Path) -> None:
    path = wal_path(tmp_path)
    log = WriteAheadLog.open(path)
    for item in sequential_puts(3):
        log.append(item)
    log.append(sequential_puts(4)[3], sync=False)
    log.close()

    before = path.stat().st_size
    # The OS left the fourth frame 5 bytes short of its advertised length.
    torn_bytes = 5
    with path.open("r+b") as handle:
        handle.truncate(before - torn_bytes)
        handle.flush()

    scan = WriteAheadLog.recover(path)
    assert scan.frame_count == 3
    assert scan.torn_tail is True

    removed = WriteAheadLog.truncate_torn_tail(path, scan.valid_bytes)
    frame_bytes = FRAME_HEADER_SIZE + len(key_for(3)) + len(value_for(3))
    assert removed == frame_bytes - torn_bytes == scan.discarded_bytes
    assert path.stat().st_size == scan.valid_bytes

    # Second repair is a no-op, which is what makes retries safe.
    assert WriteAheadLog.truncate_torn_tail(path, scan.valid_bytes) == 0

    repaired = WriteAheadLog.recover(path)
    assert repaired.frame_count == 3
    assert repaired.torn_tail is False
    assert repaired.discarded_bytes == 0


def test_append_single_frame_is_synchronously_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = count_fsyncs(monkeypatch)

    log = WriteAheadLog.open(wal_path(tmp_path))
    log.append(InternalEntry.put(b"hot", 0, b"value"))

    assert len(calls) == 1, "a sync append must reach stable storage exactly once"
    log.close()


def test_append_without_sync_defers_fsync_until_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = count_fsyncs(monkeypatch)

    log = WriteAheadLog.open(wal_path(tmp_path), sync=False)
    log.append(InternalEntry.put(b"warm", 0, b"value"))
    assert calls == [], "buffered writes must not pay for an fsync"

    log.sync()
    assert len(calls) == 1
    log.close()


def test_recover_reads_back_what_was_written(tmp_path: Path) -> None:
    path = wal_path(tmp_path)
    log = WriteAheadLog.open(path)
    written = sequential_puts(5)
    for item in written:
        log.append(item)
    log.close()

    result = WriteAheadLog.recover(path)

    assert result.frame_count == 5
    assert [(item.user_key, item.value) for item in result.entries] == [
        (item.user_key, item.value) for item in written
    ]
    assert not result.torn_tail


def test_recover_of_missing_file_yields_empty_result(tmp_path: Path) -> None:
    result = WriteAheadLog.recover(tmp_path / "absent.log")

    assert result.frame_count == 0
    assert result.valid_bytes == 0
    assert not result.torn_tail


def test_flipped_bit_in_value_is_caught_by_crc32() -> None:
    image = build_image(3)
    # Skip 3 whole frames, then corrupt the first byte of the fourth.
    corrupted = image[: FRAME_HEADER_SIZE * 3 + 4] + flip_bit(image[FRAME_HEADER_SIZE * 3 + 4 :], 0)

    with pytest.raises(ChecksumMismatchError):
        scan_wal_bytes(corrupted)


def test_terminal_frame_crc_mismatch_is_treated_as_torn_tail_by_default() -> None:
    image = build_image(3)
    corrupted = flip_bit(image, len(image) - 1)

    permissive = scan_wal_bytes(corrupted)
    assert permissive.frame_count == 2
    assert permissive.torn_tail is True

    with pytest.raises(ChecksumMismatchError):
        scan_wal_bytes(corrupted, strict_tail_crc=True)


def test_impossible_declared_lengths_raise_corruption() -> None:
    image = bytearray(build_image(1))
    # klen field lives at bytes 13..17 of the first frame.
    image[13:17] = (1 << 24).to_bytes(4, "little")

    with pytest.raises(CorruptWALError, match="impossible lengths"):
        scan_wal_bytes(bytes(image))


def test_unknown_op_byte_raises_corruption() -> None:
    image = bytearray(build_image(1))
    image[12] = 0x7F

    with pytest.raises(CorruptWALError, match="unknown op byte"):
        scan_wal_bytes(bytes(image))


def test_max_sequence_resumes_after_the_highest_replayed_frame() -> None:
    assert max_sequence(sequential_puts(4)) == 4
    assert max_sequence([]) == 0
    assert max_sequence([InternalEntry.put(b"z", 41, b"v")]) == 42


def test_closed_log_rejects_further_appends(tmp_path: Path) -> None:
    log = WriteAheadLog.open(wal_path(tmp_path))
    log.append(entry(b"k", 0, b"v"))
    log.close()

    with pytest.raises(ClosedEngineError):
        log.append(entry(b"k2", 1, b"v"))


def test_context_manager_closes_and_accounts_bytes(tmp_path: Path) -> None:
    path = wal_path(tmp_path)
    with WriteAheadLog.open(path) as log:
        log.append(entry(b"k", 0, b"v1"))
        log.append(entry(b"k", 1, b"v2"))
        assert log.frames_appended == 2

    assert log.closed
    assert log.bytes_appended == path.stat().st_size
    assert WriteAheadLog.recover(path).frame_count == 2
