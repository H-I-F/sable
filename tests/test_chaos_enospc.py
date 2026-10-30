"""Chaos: disk-space exhaustion must fail loudly and leave no half-state behind.

Two failure points decide whether a store stays consistent when the disk fills:

1. **WAL append.** If the write handle refuses bytes, the mutation must be
   rejected *before* it reaches the memtable. ``_stage_mutation`` is ordered
   precisely so this holds: the WAL frame is written first, the memtable is
   updated second. A failed frame therefore leaves the tree exactly as it was —
   no ghost key, no resurrectable bytes — which is what lets a later replay
   agree with what callers were told.

2. **SSTable publish.** The writer assembles a sibling temp file, fsyncs it,
   renames it into place, then fsyncs the directory. If the FS reports ENOSPC
   while the bytes are being made durable, the temp file must be unlinked and the
   level left untouched; a half-written ``.sst`` in the version set would be read
   as truth.

ENOSPC is injected at the two points where a real filesystem reports it: the
WAL's write handle, and the ``fsync`` that flushes an SSTable's temp file (many
filesystems report ENOSPC at writeback rather than at write time).
"""

from __future__ import annotations

import errno
from pathlib import Path
from unittest import mock

import pytest

from helpers import db_path, key_for, value_for
from sable.engine import Engine


class EnospcWriter:
    """A WAL handle wrapper that refuses every write with ENOSPC.

    Flush/fileno/close are delegated so the surrounding ``WriteAheadLog`` keeps
    working; only the byte-writing path fails, exactly as a full disk would.
    """

    def __init__(self, real: object) -> None:
        self._real = real

    def write(self, _data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self) -> None:
        self._real.flush()  # type: ignore[attr-defined]

    def fileno(self) -> int:
        return self._real.fileno()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._real.close()  # type: ignore[attr-defined]


def test_wal_append_enospc_rejects_write_and_keeps_memtable_clean(tmp_path: Path) -> None:
    """A refused WAL write is never applied, never persisted, never resurrected."""
    home = db_path(tmp_path)
    engine = Engine.open(home)
    real_handle = engine._wal._handle  # WAL is opened eagerly at bootstrap
    engine._wal._handle = EnospcWriter(real_handle)
    try:
        with pytest.raises(OSError) as excinfo:
            engine.put(b"k-enospc", b"value")
        assert excinfo.value.errno == errno.ENOSPC

        # Rejected before visibility: not readable, memtable untouched, and the
        # rejected frame never reached the file on disk.
        assert engine.get(b"k-enospc") is None
        assert engine._active.is_empty
        assert engine._active.approximate_bytes == 0
        assert list(engine.scan()) == []
        assert engine._wal.path.stat().st_size == 0
    finally:
        engine._wal._handle = real_handle

    # Once the disk has room again the engine keeps working, and the earlier
    # failure cannot reappear on replay because nothing was ever written.
    engine.put(b"k-ok", b"1")
    assert engine.get(b"k-ok") == b"1"
    engine.close()

    reopened = Engine.open(home)
    try:
        assert reopened.get(b"k-enospc") is None
        assert reopened.get(b"k-ok") == b"1"
    finally:
        reopened.close()


def test_sstable_flush_enospc_leaves_no_partial_file_and_level_clean(tmp_path: Path) -> None:
    """A failed SSTable publish leaves no temp residue and does not enter a level."""
    home = db_path(tmp_path)
    engine = Engine.open(home)
    try:
        for index in range(20):
            engine.put(key_for(index), value_for(index))

        assert set(home.glob("*.sst")) == set()

        def _boom(_fd: int) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("os.fsync", side_effect=_boom):
            with pytest.raises(OSError) as excinfo:
                engine.flush()
            assert excinfo.value.errno == errno.ENOSPC

        # No published table, no leftover temp sibling, and every level stays empty.
        assert set(home.glob("*.sst")) == set()
        assert not list(home.glob("*.tmp-sable"))
        assert all(not level for level in engine.level_files)

        # The data is still where it was: the sealed memtable never left memory,
        # so the flush failure cost durability of nothing that was acknowledged.
        assert engine.get(key_for(0)) == value_for(0)
        assert len(list(engine.scan())) == 20
    finally:
        engine.close()


def test_failed_flush_preserves_data_via_wal_across_reopen(tmp_path: Path) -> None:
    """A failed publish never rotated the WAL, so a reopen still sees every key."""
    home = db_path(tmp_path)
    engine = Engine.open(home)
    try:
        for index in range(16):
            engine.put(key_for(index), value_for(index))

        def _boom(_fd: int) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("os.fsync", side_effect=_boom), pytest.raises(OSError):
            engine.flush()

        # The flush failed before the WAL was rotated, so the acknowledged writes
        # are still only in the log. Closing proves they survive a restart.
        assert all(not level for level in engine.level_files)
    finally:
        engine.close()

    reopened = Engine.open(home)
    try:
        assert len(list(reopened.scan())) == 16
        assert reopened.get(key_for(0)) == value_for(0)
        assert reopened.level_files[0] == []
    finally:
        reopened.close()

