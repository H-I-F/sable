"""Engine topology: memtable accounting and leveled file distribution.

The topology must be a faithful projection of the engine's own state, so the
strongest assertion here is direct equivalence with ``engine.level_files`` at every
level, alongside the aggregate figures the tree renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import db_path, key_for, value_for
from sable.engine import Engine
from sable.topology import build_topology, inspect_home, main, render_topology


def test_empty_store_reports_zero_everywhere(tmp_path: Path) -> None:
    """A fresh directory has no memtable payload, no files and sequence -1."""
    engine = Engine.open(db_path(tmp_path))
    try:
        topology = build_topology(engine)
    finally:
        engine.close()

    assert topology.active_bytes == 0
    assert topology.active_versions == 0
    assert topology.immutable_count == 0
    assert topology.total_files == 0
    assert topology.total_bytes == 0
    assert topology.last_sequence == -1
    assert all(not level.files for level in topology.levels)


def test_topology_matches_engine_levels_after_flush(tmp_path: Path) -> None:
    """Every level's file list is a projection of ``engine.level_files``."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for index in range(50):
            engine.put(key_for(index), value_for(index))
        engine.flush()
        topology = build_topology(engine)

        engine_levels = engine.level_files
        assert len(topology.levels) == len(engine_levels)
        for level_info, metas in zip(topology.levels, engine_levels, strict=True):
            assert len(level_info.files) == len(metas)
            assert level_info.num_entries == sum(meta.num_entries for meta in metas)
            assert level_info.total_bytes == sum(meta.file_size for meta in metas)

        assert topology.total_files == 1
        assert topology.total_bytes > 0
        assert topology.levels[0].num_entries == 50
        assert topology.levels[0].smallest_key == key_for(0)
        assert topology.levels[0].largest_key == key_for(49)
    finally:
        engine.close()


def test_topology_tracks_files_through_compaction(tmp_path: Path) -> None:
    """File placement stays consistent with the engine while levels are rewritten."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for batch in range(5):
            for index in range(20):
                key = key_for(batch * 20 + index)
                engine.put(key, value_for(batch * 20 + index))
            engine.flush()
        while engine.compact():
            pass

        topology = build_topology(engine)

        assert topology.total_files >= 1
        assert topology.total_files == sum(len(level.files) for level in topology.levels)
        assert topology.total_files == sum(len(level) for level in engine.level_files)
        for level_info, metas in zip(topology.levels, engine.level_files, strict=True):
            assert [file.file_id for file in level_info.files] == [meta.file_id for meta in metas]
    finally:
        engine.close()


def test_render_draws_every_node(tmp_path: Path) -> None:
    """The tree names the store, the memtable, the levels and the totals."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for index in range(10):
            engine.put(key_for(index), value_for(index))
        engine.flush()
        rendered = render_topology(build_topology(engine))
    finally:
        engine.close()

    assert "store:" in rendered
    assert "active MemTable" in rendered
    assert "immutable tables" in rendered
    assert "L0" in rendered
    assert "total" in rendered
    assert ".sst" in rendered


def test_inspect_home_recovers_from_disk(tmp_path: Path) -> None:
    """Closing and reopening shows the flushed table and an empty active memtable."""
    home = db_path(tmp_path)
    engine = Engine.open(home)
    for index in range(30):
        engine.put(key_for(index), value_for(index))
    engine.flush()
    engine.close()

    topology = inspect_home(home)

    assert topology.total_files == 1
    assert topology.active_versions == 0  # everything moved into the SSTable
    assert topology.last_sequence == 29


def test_main_prints_tree_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints the tree and exits zero."""
    home = db_path(tmp_path)
    engine = Engine.open(home)
    engine.put(key_for(0), value_for(0))
    engine.flush()
    engine.close()

    assert main([str(home)]) == 0

    captured = capsys.readouterr().out
    assert "store:" in captured
    assert "total" in captured
