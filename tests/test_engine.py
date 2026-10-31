"""Read and write paths: durability order, recovery, and version resolution.

This file grows across PR 05 (read path) and PR 06 (write path). The writing
tests pin down the direction of causality: nothing becomes visible until it is
durable, and everything acknowledged must reappear.
"""

from __future__ import annotations

import os
from collections.abc import Iterator as IteratorABC
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


def test_scan_merges_levels_and_filters_tombstones(tmp_path: Path) -> None:
    """Mandated case: one ordered stream across every tier, tombstones consumed."""
    engine = small_engine(tmp_path)
    for index in range(30):
        engine.put(key_for(index), b"gen0")

    engine.flush()
    promote_to_level(engine, 1)          # elder generation now lives below L0

    for index in range(0, 30, 2):        # overwrite half of them
        engine.put(key_for(index), b"gen1")
    engine.put(b"m-after", b"newly-inserted")
    engine.flush()

    engine.delete(key_for(4))            # tombstone over a twice-written key
    engine.delete(key_for(21))           # tombstone over a single-write key
    engine.flush()

    engine.put(key_for(0), b"gen2")      # newest version never leaves the memtable
    engine.put(b"m-latest", b"memtable-only")

    observed = list(engine.scan())

    expected_keys = [key_for(i) for i in range(30) if i not in (4, 21)]
    expected_keys += [b"m-after", b"m-latest"]
    expected_keys.sort()

    assert [key for key, _ in observed] == expected_keys
    assert dict(observed)[key_for(0)] == b"gen2"
    assert dict(observed)[key_for(2)] == b"gen1"
    assert dict(observed)[key_for(1)] == b"gen0"
    assert key_for(4) not in dict(observed), "tombstoned keys must not appear at all"
    assert key_for(21) not in dict(observed)
    engine.close()


def test_scan_returns_generator_not_materialised_list(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    for index in range(20):
        engine.put(key_for(index), b"v")

    stream = engine.scan()

    assert isinstance(stream, IteratorABC)
    assert len(list(stream)) == 20
    engine.close()


def test_scan_honours_window_bounds(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    for index in range(20):
        engine.put(key_for(index), b"v")
    engine.flush()

    assert [k for k, _ in engine.scan(key_for(5), key_for(9))] == [
        key_for(i) for i in range(5, 9)
    ]
    assert [k for k, _ in engine.scan(end=key_for(3))] == [key_for(i) for i in range(3)]
    assert len([k for k, _ in engine.scan(start=key_for(18))]) == 2

    inclusive = list(engine.scan(key_for(0), key_for(2), end_exclusive=False))
    assert [k for k, _ in inclusive] == [key_for(i) for i in range(3)]

    exclusive_start = list(engine.scan(key_for(0), start_inclusive=False))
    assert exclusive_start[0][0] == key_for(1)
    engine.close()


def test_scan_limit_stops_after_requested_pairs(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    for index in range(50):
        engine.put(key_for(index), b"v")

    assert len(list(engine.scan(limit=7))) == 7
    engine.close()


def test_scan_is_consistent_with_point_reads(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    for index in range(40):
        engine.put(key_for(index), f"value-{index}".encode())
        if index % 7 == 0:
            engine.flush()
    engine.delete(key_for(13))

    for key, value in engine.scan():
        assert engine.get(key) == value
    assert engine.get(key_for(13)) is None
    engine.close()


def test_scan_rejects_malformed_arguments(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)

    with pytest.raises(InvalidArgumentError, match="scan bounds must be bytes"):
        list(engine.scan(start=" textual "))
    with pytest.raises(InvalidArgumentError, match="limit must be non-negative"):
        list(engine.scan(limit=-1))
    engine.close()


def test_scan_over_empty_store_yields_nothing(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)

    assert list(engine.scan()) == []
    engine.close()


def test_internal_lookup_reveals_tombstone_not_absence(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"ghost", b"body")
    engine.delete(b"ghost")

    found = engine._lookup_entry(b"ghost")

    assert isinstance(found, InternalEntry)
    assert found.is_tombstone is True
    assert engine.get(b"ghost") is None
    engine.close()


def test_snapshot_point_lookup_resolves_newest_visible_version_in_memtable(tmp_path: Path) -> None:
    """A pinned view must see the newest version at or below its sequence.

    When a key holds a newer, post-snapshot version beside an older one still
    visible to the view, a point lookup and a range scan must agree on what the
    pinned sequence resolves to. Both versions staying in the active memtable
    (no flush between them) is the case the scan path handled but the point
    path did not: ``get`` returned the newest version and, finding it invisible,
    reported absence instead of falling back to the older visible one.

    Regression guard for the memtable visibility fix.
    """
    engine = small_engine(tmp_path)
    engine.put(b"K", b"A")
    engine.put(b"_pad", b"x")  # advance the sequence past K's first write
    view = engine.snapshot()    # pinned at the sequence before K is overwritten
    engine.put(b"K", b"B")      # overwrite K at a higher sequence, still in memtable

    assert view.get(b"K") == b"A"
    assert list(view.scan(b"K", b"L")) == [(b"K", b"A")]
    view.release()
    engine.close()


def test_open_snapshots_counts_acquire_and_release(tmp_path: Path) -> None:
    """The public snapshot count lets a caller prove acquire/release balanced."""
    engine = small_engine(tmp_path)
    base = engine.open_snapshots

    view = engine.snapshot()
    assert engine.open_snapshots == base + 1

    view.release()
    assert engine.open_snapshots == base
    view.release()  # release is idempotent, so a stacked finally is safe
    assert engine.open_snapshots == base
    engine.close()
