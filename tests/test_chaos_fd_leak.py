"""Chaos: file descriptors stay bounded under churn, and return home on close.

An LSM store opens a descriptor per live SSTable plus one for the WAL, and caches
readers so a hot file is not reopened per lookup. The danger is not the steady
state — it is the transitions: compaction retires files under a reader's feet,
snapshots defer unlinking, and every scan pins then unpins. If any of those paths
forgets to close what it opened, the process bleeds descriptors until it dies.

These tests watch ``/proc/self/fd`` directly rather than trusting the reader cache
to report on itself. Reads are measured as deltas from a baseline, so unrelated
descriptors held by the test runner do not skew the result.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from helpers import db_path, key_for, value_for
from sable.engine import Engine

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"), reason="descriptor accounting requires /proc/self/fd"
)


def _fd_count() -> int:
    """Number of file descriptors this process currently holds open."""
    return len(os.listdir("/proc/self/fd"))


def test_fd_count_returns_to_baseline_after_close(tmp_path: Path) -> None:
    """Every descriptor the engine opened is released by ``close``."""
    baseline = _fd_count()
    engine = Engine.open(db_path(tmp_path))
    for index in range(30):
        engine.put(key_for(index), value_for(index))
    engine.flush()
    assert engine.get(key_for(0)) == value_for(0)  # force a reader open

    assert _fd_count() > baseline  # the engine really is holding descriptors

    engine.close()

    assert _fd_count() <= baseline + 2


def test_fd_count_bounded_under_write_flush_compact_churn(tmp_path: Path) -> None:
    """Descriptor growth tracks live files, not the number of operations."""
    engine = Engine.open(db_path(tmp_path))
    try:
        opened = _fd_count()
        for batch in range(8):
            for index in range(40):
                key = key_for(batch * 40 + index)
                engine.put(key, value_for(batch * 40 + index))
            engine.flush()
            while engine.compact():
                pass
            for index in range(0, 320, 5):
                engine.get(key_for(index))
            assert sum(1 for _ in engine.scan()) >= 0

        live_files = sum(len(level) for level in engine.level_files)
        assert _fd_count() <= opened + live_files + 4
    finally:
        engine.close()


def test_reader_cache_holds_only_live_files(tmp_path: Path) -> None:
    """After compaction, no cached reader survives for a retired file id."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for batch in range(5):
            for index in range(80):
                engine.put(key_for(index), value_for(batch * 80 + index))
            engine.flush()
        # Touch every live file so the cache is populated before compaction.
        for level in engine.level_files:
            for meta in level:
                engine.get(meta.smallest_key)
        while engine.compact():
            pass
        for level in engine.level_files:
            for meta in level:
                engine.get(meta.smallest_key)

        live_ids = {meta.file_id for level in engine.level_files for meta in level}
        cached_ids = set(engine._readers)
        assert cached_ids <= live_ids
    finally:
        engine.close()


def test_repeated_scans_are_descriptor_stable(tmp_path: Path) -> None:
    """A long run of scans reuses cached readers instead of opening new ones."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for index in range(60):
            engine.put(key_for(index), value_for(index))
        engine.flush()
        for _ in range(3):  # warm the reader cache
            assert sum(1 for _ in engine.scan()) == 60

        stable = _fd_count()
        for _ in range(200):
            assert sum(1 for _ in engine.scan()) == 60
        assert _fd_count() == stable
    finally:
        engine.close()


def test_concurrent_readers_keep_descriptors_bounded(tmp_path: Path) -> None:
    """Readers racing a live compactor neither leak descriptors nor raise."""
    engine = Engine.open(db_path(tmp_path))
    try:
        for index in range(100):
            engine.put(key_for(index), value_for(index))
        engine.flush()

        errors: list[BaseException] = []
        stop = threading.Event()

        def _reader(offset: int) -> None:
            try:
                while not stop.is_set():
                    for step in range(0, 100, 7):
                        engine.get(key_for((step + offset) % 100))
                    for _ in engine.scan(limit=5):
                        pass
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        opened = _fd_count()
        threads = [threading.Thread(target=_reader, args=(slot,)) for slot in range(3)]
        for thread in threads:
            thread.start()
        for batch in range(3):
            for index in range(40):
                engine.put(key_for(1000 + batch * 40 + index), value_for(index))
            engine.flush()
            while engine.compact():
                pass
        stop.set()
        for thread in threads:
            thread.join(timeout=15)

        assert not any(thread.is_alive() for thread in threads), "a reader thread hung"
        assert not errors, f"a concurrent read raised: {errors[:3]}"

        live_files = sum(len(level) for level in engine.level_files)
        assert _fd_count() <= opened + live_files + 8
    finally:
        engine.close()
