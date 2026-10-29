"""Read and write paths: durability order, recovery, and version resolution.

This file grows across PR 05 (read path) and PR 06 (write path). The writing
tests pin down the direction of causality: nothing becomes visible until it is
durable, and everything acknowledged must reappear.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import db_path, key_for
from sable import Engine, EngineOptions
from sable.errors import ClosedEngineError, InvalidArgumentError
from sable.memtable import MemTable
from sable.types import InternalEntry, OpType, WriteOptions


def small_engine(tmp_path: Path, **kwargs: int) -> Engine:
    return Engine.open(db_path(tmp_path), EngineOptions(block_size_bytes=64, **kwargs))


def promote_to_level(engine: Engine, level: int) -> None:
    """Relocate every L0 table one level deeper, simulating post-compaction state.

    Compaction itself lands in PR 10; this exercises the same descent order the
    read path uses once levels above L0 hold data.
    """
    moved = engine._levels[0]
    engine._levels[0] = []
    engine._levels[level].extend(moved)
    for meta in moved:
        object.__setattr__(meta, "level", level)


def test_get_reads_value_from_active_memtable(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"alpha", b"one")

    assert engine.get(b"alpha") == b"one"
    assert engine.get(b"missing") is None
    engine.close()


def test_empty_value_is_distinct_from_absence(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"blank", b"")

    assert engine.get(b"blank") == b""
    assert engine.get(b"untouched") is None
    engine.close()


def test_get_reads_value_from_flushed_l0_file(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"persisted", b"on-disk")
    engine.flush()

    assert engine.get(b"persisted") == b"on-disk"
    assert len(engine.level_files[0]) == 1
    engine.close()


def test_latest_version_wins_over_flushed_elder(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"versioned", b"v1")
    engine.flush()
    engine.put(b"versioned", b"v2")

    assert engine.get(b"versioned") == b"v2"
    engine.close()


def test_get_shadows_tombstone_across_levels(tmp_path: Path) -> None:
    """Mandated case: a tombstone masks elder versions living below it."""
    engine = small_engine(tmp_path)
    engine.put(b"doomed", b"first")
    engine.flush()
    # Move the elder version out of L0 so the tombstone must beat a *file*,
    # not merely the same memtable.
    promote_to_level(engine, 1)

    engine.delete(b"doomed")
    engine.flush()

    assert engine.get(b"doomed") is None, "tombstone must shadow the L1 version"

    shadow = engine._lookup_entry(b"doomed")
    assert shadow is not None, "the tombstone itself is what hides elder data"
    assert shadow.is_tombstone and shadow.op is OpType.DELETE
    engine.close()


def test_resurrection_after_tombstone_is_visible(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"phoenix", b"ashes")
    engine.flush()
    engine.delete(b"phoenix")
    engine.flush()
    engine.put(b"phoenix", b"reborn")

    assert engine.get(b"phoenix") == b"reborn"
    engine.close()


def test_newer_l0_file_beats_older_one(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"racing", b"older")
    engine.flush()
    engine.put(b"racing", b"newer")
    engine.flush()

    assert engine.get(b"racing") == b"newer"
    l0 = engine.level_files[0]
    assert [meta.file_id for meta in l0] == sorted(
        (meta.file_id for meta in l0), reverse=True
    ), "L0 must be searched newest file first"
    engine.close()


def test_reopen_preserves_flushed_state(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions(block_size_bytes=64))
    for index in range(30):
        engine.put(key_for(index), f"v{index}".encode())
    engine.flush()
    engine.close()

    reopened = Engine.open(home, EngineOptions(block_size_bytes=64))
    assert reopened.get(key_for(13)) == b"v13"
    assert reopened.get(key_for(29)) == b"v29"
    assert len(reopened.level_files[0]) == 1
    reopened.close()


def test_reopened_sequence_never_regresses(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    for index in range(5):
        engine.put(key_for(index), b"v")
    engine.flush()
    engine.close()

    reopened = Engine.open(home, EngineOptions())
    reopened.put(key_for(99), b"v"), reopened.put(key_for(99), b"v2")
    assert reopened._last_sequence > 5
    reopened.close()


def test_missing_home_without_permission_to_create(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentError, match="creation was not requested"):
        Engine.open(tmp_path / "absent", EngineOptions(create_if_missing=False))


def test_error_if_exists_rejects_populated_home(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    engine.put(b"k", b"v")
    engine.flush()
    engine.close()

    with pytest.raises(InvalidArgumentError, match="already holds a database"):
        Engine.open(home, EngineOptions(error_if_exists=True))


def test_reads_after_close_are_rejected(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"k", b"v")
    engine.close()

    with pytest.raises(ClosedEngineError):
        engine.get(b"k")


def test_non_bytes_keys_are_rejected(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)

    with pytest.raises(InvalidArgumentError, match="key must be bytes"):
        engine.get("not-bytes")
    with pytest.raises(InvalidArgumentError, match="value must be bytes"):
        engine.put(b"k", "not-bytes")
    engine.close()


def test_flush_without_writes_is_refused(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)

    with pytest.raises(Exception, match="nothing to flush"):
        engine.flush()
    engine.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"k", b"v")
    engine.close()
    engine.close()

    assert engine.closed is True


def test_put_is_durable_before_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mandated case: fsync-before-apply, with nothing becoming visible early."""
    engine = small_engine(tmp_path)
    timeline: list[str] = []
    real_fsync = os.fsync
    real_apply = MemTable.add

    def trace_fsync(fd: int) -> None:
        timeline.append("fsync")
        real_fsync(fd)

    def trace_apply(self: MemTable, entry: InternalEntry) -> None:
        timeline.append("apply")
        real_apply(self, entry)

    monkeypatch.setattr(os, "fsync", trace_fsync)
    monkeypatch.setattr(MemTable, "add", trace_apply)

    engine.put(b"durable", b"value")

    # Nothing is visible until the log has reached stable storage; a crash one
    # instruction after the fsync still replays this write.
    assert timeline == ["fsync", "apply"], timeline
    assert engine.get(b"durable") == b"value"
    engine.close()


def test_wal_survives_reopen_without_flush(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    for index in range(25):
        engine.put(key_for(index), f"v{index}".encode())
    # Deliberately no flush: nothing has reached an SSTable yet.
    engine.close()

    recovered = Engine.open(home, EngineOptions())
    for index in range(25):
        assert recovered.get(key_for(index)) == f"v{index}".encode()
    recovered.close()


def test_unflushed_delete_survives_reopen(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    engine.put(b"present", b"v")
    engine.flush()
    engine.delete(b"present")
    engine.close()

    recovered = Engine.open(home, EngineOptions())
    assert recovered.get(b"present") is None
    recovered.close()


def test_wal_is_retired_after_flush(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    for index in range(10):
        engine.put(key_for(index), b"payload")
    engine.flush()

    active = [path.name for path in home.glob("wal-*.log")]
    assert len(active) == 1, "the superseded segment must be retired"
    assert active[0] == "wal-000002.log", "the successor generation must take over"

    # Retiring the log is safe only because the data already landed in a table.
    assert len(engine.level_files[0]) == 1
    for index in range(10):
        assert engine.get(key_for(index)) == b"payload"
    engine.close()


def test_unsynced_writes_still_replay_after_clean_close(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions(default_write_sync=False))
    for index in range(5):
        engine.put(key_for(index), b"v", WriteOptions(sync=False))
    engine.close()

    recovered = Engine.open(home, EngineOptions())
    assert recovered.get(key_for(0)) == b"v"
    recovered.close()


def test_memtable_threshold_triggers_automatic_flush(tmp_path: Path) -> None:
    engine = Engine.open(db_path(tmp_path), EngineOptions(memtable_bytes_threshold=512))
    for index in range(60):
        engine.put(key_for(index), b"x" * 64)

    total_files = sum(len(level) for level in engine.level_files)
    assert total_files >= 1, "crossing the budget must publish a table without asking"
    assert engine.get(key_for(59)) == b"x" * 64
    engine.close()


def test_torn_wal_tail_is_repaired_on_open(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, EngineOptions())
    engine.put(b"complete", b"v1")
    engine.put(b"partial", b"v2")
    engine.close()

    wal = sorted(home.glob("wal-*.log"))[0]
    complete = wal.read_bytes()
    wal.write_bytes(complete[: len(complete) - 4])

    reopened = Engine.open(home, EngineOptions())
    assert reopened.get(b"complete") == b"v1"
    assert reopened.get(b"partial") is None, "unacknowledged frame is legitimately absent"
    # Subsequent writes must append cleanly past the repaired boundary.
    reopened.put(b"after", b"v3")
    assert reopened.get(b"after") == b"v3"
    reopened.close()

    final = Engine.open(home, EngineOptions())
    assert final.get(b"complete") == b"v1"
    assert final.get(b"after") == b"v3"
    final.close()


def test_internal_lookup_reveals_tombstone_not_absence(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"ghost", b"body")
    engine.delete(b"ghost")

    found = engine._lookup_entry(b"ghost")

    assert isinstance(found, InternalEntry)
    assert found.is_tombstone is True
    assert engine.get(b"ghost") is None
    engine.close()
