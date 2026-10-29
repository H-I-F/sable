"""Read path: how versions in memory and on disk resolve to one answer.

The engine descends newest-first and stops at the first version it meets, even
when that version is a tombstone. Every test in this file pins down one
consequence of that rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import db_path, key_for
from sable import Engine, EngineOptions
from sable.errors import ClosedEngineError, InvalidArgumentError
from sable.types import InternalEntry, OpType


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


def test_internal_lookup_reveals_tombstone_not_absence(tmp_path: Path) -> None:
    engine = small_engine(tmp_path)
    engine.put(b"ghost", b"body")
    engine.delete(b"ghost")

    found = engine._lookup_entry(b"ghost")

    assert isinstance(found, InternalEntry)
    assert found.is_tombstone is True
    assert engine.get(b"ghost") is None
    engine.close()
