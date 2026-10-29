"""Compaction: physical reclamation of versions and tombstones.

The interesting assertions here are about bytes leaving the system, not about
what reads return — reads were already correct before compaction existed, since
version resolution is decided by descending order. What compaction must prove is
that superseded versions and spent tombstones stop occupying disk.
"""

from __future__ import annotations

from pathlib import Path

from helpers import db_path, key_for
from sable import Engine, EngineOptions
from sable.compaction import (
    CompactionJob,
    level_bytes,
    plan_compaction,
    run_compaction,
    visible_versions,
)
from sable.sstable import SSTableReader
from sable.types import InternalEntry, SSTableMeta


def small_options(**kwargs: int) -> EngineOptions:
    """Thresholds sized so a handful of writes is enough to trigger work."""
    defaults: dict[str, int] = {
        "block_size_bytes": 64,
        "level0_file_num_compaction_trigger": 4,
        "l1_bytes_trigger": 64,
        "max_levels": 4,
    }
    defaults.update(kwargs)
    return EngineOptions(**defaults)


def records_in(path: Path) -> list[InternalEntry]:
    with SSTableReader.open(path) as reader:
        return list(reader.iter_all())


def test_compaction_physically_drops_tombstone(tmp_path: Path) -> None:
    """Mandated case: a spent tombstone stops existing on disk."""
    home = db_path(tmp_path)
    options = small_options()
    engine = Engine.open(home, options)
    engine.put(b"vanished", b"lived-once")
    engine.put(b"survivor", b"stays")
    engine.flush()
    engine.delete(b"vanished")
    engine.flush()

    inputs = list(engine.level_files[0])
    assert len(inputs) == 2
    assert any(
        entry.is_tombstone for meta in inputs for entry in records_in(home / meta.file_name)
    ), "the tombstone must exist on disk before compaction can be said to remove it"

    # Land the output in the deepest level, where nothing below it can be
    # resurrected by the marker's absence - the only place dropping is legal.
    job = CompactionJob(level=0, target_level=options.max_levels - 1, inputs=tuple(inputs))
    next_id = iter(range(500, 600))
    outputs = run_compaction(job, home, options, lambda: next(next_id))

    assert len(outputs) == 1
    produced = records_in(home / outputs[0].file_name)

    assert [entry.user_key for entry in produced] == [b"survivor"], (
        "the deleted key and its marker must both be gone from disk"
    )
    assert not any(entry.is_tombstone for entry in produced)
    assert not (home / inputs[-1].file_name).name.endswith(".tmp-sable")
    engine.close()


def test_tombstone_is_retained_above_the_deepest_level(tmp_path: Path) -> None:
    """A marker with levels below it still has work to do; it must be kept."""
    home = db_path(tmp_path)
    options = small_options()
    engine = Engine.open(home, options)
    engine.put(b"masked", b"value")
    engine.flush()
    engine.delete(b"masked")
    engine.flush()

    job = CompactionJob(level=0, target_level=1, inputs=tuple(engine.level_files[0]))
    outputs = run_compaction(job, home, options, lambda: next(iter([700])))

    kept = records_in(home / outputs[0].file_name)
    assert len(kept) == 1
    assert kept[0].is_tombstone, "dropping here could resurrect elder versions below"
    engine.close()


def test_auto_compaction_reaches_the_deepest_level(tmp_path: Path) -> None:
    """With a tiny budget the planner keeps working until the marker is gone."""
    home = db_path(tmp_path)
    options = small_options(l1_bytes_trigger=1, level0_file_num_compaction_trigger=2)
    engine = Engine.open(home, options)
    engine.put(b"vanished", b"lived-once")
    engine.flush()
    engine.delete(b"vanished")
    engine.flush()

    rounds = 0
    while engine.compact():
        rounds += 1
        assert rounds < 20, "compaction failed to converge"

    assert rounds >= 3, "the tree never reached the level where dropping is legal"
    assert engine.get(b"vanished") is None

    deepest = max(
        (index for index, level in enumerate(engine.level_files) if level), default=-1
    )
    surviving = [
        entry
        for meta in engine.level_files[deepest]
        for entry in records_in(home / meta.file_name)
    ]
    assert surviving == [], "the key and its marker should no longer occupy any bytes"
    engine.close()


def test_compaction_collapses_superseded_versions(tmp_path: Path) -> None:
    engine = Engine.open(db_path(tmp_path), small_options())
    for generation in range(5):
        engine.put(b"churned", f"v{generation}".encode())
        engine.flush()

    engine.compact()

    versions = [
        entry
        for level in engine.level_files
        for meta in level
        for entry in records_in(engine.home / meta.file_name)
        if entry.user_key == b"churned"
    ]
    assert len(versions) == 1, f"expected one surviving version, found {len(versions)}"
    assert versions[0].value == b"v4"
    assert engine.get(b"churned") == b"v4"
    engine.close()


def test_compaction_reduces_file_count_and_bytes(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    engine = Engine.open(home, small_options())
    for index in range(40):
        engine.put(key_for(index), b"payload")
        if index % 4 == 0:
            engine.flush()
    for index in range(0, 40, 2):
        engine.put(key_for(index), b"rewritten")

    files_before = sum(len(level) for level in engine.level_files)
    bytes_before = sum(meta.file_size for level in engine.level_files for meta in level)

    engine.compact()

    files_after = sum(len(level) for level in engine.level_files)
    bytes_after = sum(meta.file_size for level in engine.level_files for meta in level)

    assert files_after < files_before
    assert bytes_after < bytes_before
    assert engine.get(key_for(7)) == b"payload"
    assert engine.get(key_for(8)) == b"rewritten"
    engine.close()


def test_deleted_keys_disappear_from_scan_after_deep_compaction(tmp_path: Path) -> None:
    engine = Engine.open(db_path(tmp_path), small_options())
    for index in range(12):
        engine.put(key_for(index), b"v")
    engine.flush()
    for index in range(0, 12, 3):
        engine.delete(key_for(index))

    for _ in range(3):
        engine.compact()

    scanned = [key for key, _ in engine.scan()]
    assert key_for(0) not in scanned
    assert key_for(1) in scanned
    assert len(scanned) == 8
    engine.close()


def test_planner_is_quiet_for_a_balanced_tree(tmp_path: Path) -> None:
    engine = Engine.open(db_path(tmp_path), small_options())
    engine.put(b"only", b"value")
    engine.flush()

    assert plan_compaction(engine.level_files, engine.options) is None
    assert engine.compact() is False
    engine.close()


def test_planner_triggers_on_l0_file_count(tmp_path: Path) -> None:
    options = small_options()
    engine = Engine.open(db_path(tmp_path), options)
    for index in range(options.level0_file_num_compaction_trigger):
        engine.put(key_for(index), b"v")
        engine.flush()

    job = plan_compaction(engine.level_files, options)

    assert job is not None
    assert job.level == 0
    assert job.target_level == 1
    assert len(job.inputs) == options.level0_file_num_compaction_trigger
    engine.close()


def test_planner_merges_only_overlapping_files(tmp_path: Path) -> None:
    options = small_options(max_levels=4, level_size_multiplier=1)
    below = [
        SSTableMeta(file_id=1, level=1, smallest_key=b"a", largest_key=b"c", file_size=128),
        SSTableMeta(file_id=2, level=1, smallest_key=b"x", largest_key=b"z", file_size=128),
    ]
    overlapping = SSTableMeta(
        file_id=3, level=2, smallest_key=b"a", largest_key=b"c", file_size=128
    )
    unrelated = SSTableMeta(
        file_id=4, level=2, smallest_key=b"m", largest_key=b"n", file_size=128
    )
    levels = [[], below, [overlapping, unrelated]]

    job = plan_compaction(levels, options)

    # Level 1 is over its 64-byte budget, so its oldest file participates; only
    # the L2 file whose key span actually intersects should join it.
    assert job is not None
    assert job.inputs[0].file_id == 1
    assert overlapping.file_id in job.input_ids
    assert unrelated.file_id not in job.input_ids


def test_visible_versions_keeps_newest_and_filters_tombstones() -> None:
    stream_a = [InternalEntry.put(b"k", 1, b"old"), InternalEntry.put(b"z", 2, b"z")]
    stream_b = [InternalEntry.put(b"k", 5, b"new"), InternalEntry.delete(b"q", 6)]

    kept = list(visible_versions([iter(stream_a), iter(stream_b)], drop_tombstones=False))

    assert [entry.user_key for entry in kept] == [b"k", b"q", b"z"]
    assert kept[0].value == b"new"
    assert kept[1].is_tombstone

    dropped = list(visible_versions([iter(stream_a), iter(stream_b)], drop_tombstones=True))
    assert [entry.user_key for entry in dropped] == [b"k", b"z"]


def test_run_compaction_returns_nothing_when_all_entries_vanish(tmp_path: Path) -> None:
    home = db_path(tmp_path)
    options = small_options()
    engine = Engine.open(home, options)
    engine.put(b"doomed", b"v")
    engine.flush()
    engine.delete(b"doomed")
    engine.flush()
    metas = list(engine.level_files[0])
    engine.close()

    job = CompactionJob(level=0, target_level=options.max_levels - 1, inputs=tuple(metas))
    next_id = iter(range(900, 999))

    outputs = run_compaction(job, home, options, lambda: next(next_id))

    assert outputs == []


def test_compaction_preserves_live_keys_that_were_never_overwritten(
    tmp_path: Path,
) -> None:
    engine = Engine.open(db_path(tmp_path), small_options())
    for index in range(20):
        engine.put(key_for(index), f"value-{index}".encode())
    engine.flush()
    engine.delete(key_for(5))
    engine.flush()

    while engine.compact():
        pass

    for index in range(20):
        expected = None if index == 5 else f"value-{index}".encode()
        assert engine.get(key_for(index)) == expected, f"key {index} lost its value"
    engine.close()


def test_level_bytes_reports_level_weight() -> None:
    assert level_bytes([]) == 0
    assert level_bytes([SSTableMeta(file_id=1, level=0, file_size=10)]) == 10
