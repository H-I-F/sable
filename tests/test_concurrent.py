"""Concurrency: what a reader keeps while writers and a compaction run.

Every worker here starts on a :class:`threading.Barrier` instead of a
``time.sleep``. A sleep makes a race merely *unlikely*; a barrier makes the
overlap *certain*, which is the only way a bug either shows up every run or is
proven absent.

Three separate guarantees are exercised, because they are three different
mechanisms and one does not imply another:

* **Snapshot isolation** — a captured view never observes work admitted after it.
* **Lease-protected files** — a compaction may retire a file's *visibility*
  immediately, but the bytes survive until the last walker lets go.
* **Version retention** — no merge discards a version a live view could still
  read, even though the file holding it is going away.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from helpers import db_path, key_for, value_for
from sable import ClosedEngineError, Engine, EngineOptions
from sable.compaction import visible_versions
from sable.sstable import SSTableReader
from sable.types import InternalEntry, WriteOptions

THREAD_TIMEOUT_SECONDS = 20.0
# Fixtures that shape the tree by hand need to decide when a flush happens;
# leaving the tiny default budget in place would flush behind their back.
NO_AUTO_FLUSH = 1 << 20


def threaded_options(**kwargs: int) -> EngineOptions:
    """Thresholds small enough that a few hundred writes trigger real work.

    The tiny memtable budget is deliberate: it forces flushes and therefore
    compactions to happen *inside* the workers rather than being something the
    test drives explicitly, so the maintenance path is exercised concurrently
    with reads instead of around them.
    """
    defaults: dict[str, int] = {
        "block_size_bytes": 64,
        "level0_file_num_compaction_trigger": 2,
        # One byte: with fixtures this small, any other budget would let the tree
        # settle above the deepest level and quietly stop short of the only place
        # where tombstone reclamation is legal.
        "l1_bytes_trigger": 1,
        "max_levels": 4,
        "memtable_bytes_threshold": 256,
    }
    defaults.update(kwargs)
    return EngineOptions(**defaults)


def spawn(targets: Sequence[Callable[[], None]]) -> None:
    """Run every worker simultaneously and surface whatever any of them raises.

    Args:
        targets: Zero-argument callables to run in parallel.

    Raises:
        AssertionError: A worker outlived ``THREAD_TIMEOUT_SECONDS``, which means
            someone is waiting on a lock nobody will release — the one failure
            mode here that would otherwise hang the entire suite.
        The first exception any worker raised. Exceptions inside threads do not
        propagate, so without this a failing concurrent assertion would quietly
        become a green test.
    """
    failures: list[BaseException] = []
    failure_lock = threading.Lock()

    def guarded(target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - re-raised below, not swallowed
            with failure_lock:
                failures.append(exc)

    threads = [threading.Thread(target=guarded, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT_SECONDS)

    hung = [thread.name for thread in threads if thread.is_alive()]
    assert not hung, f"workers deadlocked: {hung}"
    assert not failures, f"worker failed: {failures[0]!r}"


def versions_of(engine: Engine, user_key: bytes) -> int:
    """Count physical versions of ``user_key`` across every file, memtable aside."""
    total = 0
    for level in engine.level_files:
        for meta in level:
            with SSTableReader.open(engine.home / meta.file_name) as reader:
                total += sum(1 for entry in reader.iter_all() if entry.user_key == user_key)
    return total


def compact_to_balanced(engine: Engine, *, limit: int = 20) -> int:
    """Drive one repeated merge sequence, returning how many jobs ran."""
    rounds = 0
    while engine.compact() and rounds < limit:
        rounds += 1
    assert rounds < limit, "compaction failed to converge"
    return rounds


def test_snapshot_is_isolated_from_concurrent_writes(tmp_path: Path) -> None:
    """A view keeps answering from the sequence it captured, not from now."""
    engine = Engine.open(db_path(tmp_path), threaded_options())
    original = {key_for(index): value_for(index) for index in range(24)}
    for key, value in original.items():
        engine.put(key, value)

    view = engine.snapshot()
    gate = threading.Barrier(2)

    def writer() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        for key in sorted(original):
            engine.put(key, b"overwritten")

    def reader() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        for _ in range(25):
            assert dict(view.scan()) == original, "the view moved underneath its owner"

    spawn([writer, reader])

    assert dict(view.scan()) == original
    assert engine.get(key_for(0)) == b"overwritten"
    view.release()
    assert dict(engine.scan()) == dict.fromkeys(original, b"overwritten")
    engine.close()


def test_scan_completes_while_compaction_retires_its_files(tmp_path: Path) -> None:
    """Mandated overlap: a half-consumed walk finishes across a merge.

    The barrier is placed after the first pair is pulled, so compaction provably
    starts with the iterator suspended mid-file. Any request-path state taken from
    the version set after that point — buffers, block offsets, cached readers —
    belongs to files the version set is about to forget.
    """
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    expected: dict[bytes, bytes] = {}
    rows_per_file = 40
    for file_index in range(6):
        for row in range(rows_per_file):
            index = file_index * rows_per_file + row
            expected[key_for(index)] = value_for(index)
            engine.put(key_for(index), value_for(index))
        engine.flush()

    gate = threading.Barrier(2)
    pairs: list[tuple[bytes, bytes]] = []

    def scanner() -> None:
        iterator = engine.scan()
        pairs.append(next(iterator))
        gate.wait(THREAD_TIMEOUT_SECONDS)
        pairs.extend(iterator)

    def compactor() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        rounds = 0
        while engine.compact() and rounds < 10:
            rounds += 1
        assert rounds > 0, "nothing to compact, so the test raced nothing"

    spawn([scanner, compactor])

    assert len(pairs) == len(expected), "the walk lost or duplicated keys mid-compaction"
    assert dict(pairs) == expected
    engine.close()


def test_retired_file_outlives_the_walk_that_leased_it(tmp_path: Path) -> None:
    """Reference counting is what keeps an unlink from happening too early."""
    home = db_path(tmp_path)
    engine = Engine.open(home, threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH))
    expected = {key_for(index): value_for(index) for index in range(80)}
    for key, value in expected.items():
        engine.put(key, value)
        if len(expected) and key == key_for(19):
            engine.flush()
    engine.flush()

    inputs = {meta.file_id: meta.file_name for meta in engine.level_files[0]}
    assert len(inputs) >= 2, "fixture did not produce multiple L0 files"

    iterator = engine.scan()
    first = next(iterator)
    assert engine.compact() is True

    survivors = [name for name in inputs.values() if (home / name).exists()]
    assert sorted(survivors) == sorted(inputs.values()), (
        "compaction unlinked a file an open walk still holds a lease on"
    )

    rest = list(iterator)
    assert dict([first, *rest]) == expected

    assert not any((home / name).exists() for name in inputs.values()), (
        "the retired bytes outlived their last lease"
    )
    engine.close()


def test_concurrent_readers_see_monotonic_values_per_key(tmp_path: Path) -> None:
    """Writers and readers share one memtable; a key must never go backwards."""
    engine = Engine.open(db_path(tmp_path), threaded_options())
    keys = [key_for(index) for index in range(12)]
    rounds = 40

    def generation_for(round_index: int) -> bytes:
        return f"gen-{round_index:03d}".encode()

    for key in keys:
        engine.put(key, generation_for(0))

    gate = threading.Barrier(3)

    def writer() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        for round_index in range(1, rounds + 1):
            for key in keys:
                engine.put(key, generation_for(round_index))

    def reader() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        watermark: dict[bytes, int] = dict.fromkeys(keys, 0)
        for _ in range(20):
            scanned = dict(engine.scan())
            assert sorted(scanned) == sorted(keys), "a key vanished mid-rewrite"
            for key, value in scanned.items():
                seen = int(value.decode().rsplit("-", 1)[1])
                assert seen >= watermark[key], f"{key!r} went backwards: {seen}"
                watermark[key] = seen

    spawn([writer, reader, reader])

    for key in keys:
        assert engine.get(key) == generation_for(rounds)
    engine.close()


def test_compaction_keeps_versions_a_live_snapshot_can_read(tmp_path: Path) -> None:
    """Mandated: retention decides which *versions* survive, pinning cannot."""
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    engine.put(b"churning", b"first")
    engine.flush()
    view = engine.snapshot()
    engine.put(b"churning", b"second")
    engine.flush()

    gate = threading.Barrier(2)

    def compactor() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        assert compact_to_balanced(engine) > 0

    def reader() -> None:
        gate.wait(THREAD_TIMEOUT_SECONDS)
        for _ in range(25):
            assert view.get(b"churning") == b"first", "the merge rewrote history"

    spawn([compactor, reader])

    assert view.get(b"churning") == b"first"
    assert engine.get(b"churning") == b"second"
    assert versions_of(engine, b"churning") == 2, (
        "the elder version is still visible to someone, it cannot be reclaimed"
    )

    view.release()
    assert engine.live_snapshots == ()
    engine.close()


def test_versions_are_reclaimed_once_nothing_protects_them(tmp_path: Path) -> None:
    """Same workload, no view: retention must not accidentally become permanent."""
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    engine.put(b"churning", b"first")
    engine.flush()
    engine.put(b"churning", b"second")
    engine.flush()

    compact_to_balanced(engine)

    assert versions_of(engine, b"churning") == 1
    assert engine.get(b"churning") == b"second"
    engine.close()


def test_live_snapshot_keeps_a_deleted_key_masked(tmp_path: Path) -> None:
    """A tombstone may not be dropped while an older view still reads the value."""
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    engine.put(b"masked", b"value")
    engine.flush()
    view = engine.snapshot()
    engine.delete(b"masked")
    engine.flush()

    compact_to_balanced(engine)

    assert view.get(b"masked") == b"value", "the view must still see the pre-delete state"
    assert engine.get(b"masked") is None, "current readers must still see the deletion"
    assert b"masked" not in [key for key, _ in engine.scan()]

    view.release()
    engine.close()


def test_tombstone_leaves_nothing_once_no_snapshot_predates_it(tmp_path: Path) -> None:
    """Without an elder view both the marker and its victim leave the system."""
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    engine.put(b"masked", b"value")
    engine.flush()
    engine.delete(b"masked")
    engine.flush()

    compact_to_balanced(engine)

    assert versions_of(engine, b"masked") == 0
    assert engine.get(b"masked") is None
    engine.close()


def test_concurrent_writers_survive_reopen(tmp_path: Path) -> None:
    """Append-before-apply has to hold when several threads interleave appends."""
    home = db_path(tmp_path)
    engine = Engine.open(home, threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH))
    writer_count = 4
    rows_per_writer = 25

    def owner_key(writer_index: int, row: int) -> bytes:
        return f"w{writer_index}-{row:04d}".encode()

    gate = threading.Barrier(writer_count)

    def writer(index: int) -> Callable[[], None]:
        def body() -> None:
            gate.wait(THREAD_TIMEOUT_SECONDS)
            for row in range(rows_per_writer):
                engine.put(owner_key(index, row), value_for(index * rows_per_writer + row))

        return body

    spawn([writer(index) for index in range(writer_count)])

    expected = {
        owner_key(index, row): value_for(index * rows_per_writer + row)
        for index in range(writer_count)
        for row in range(rows_per_writer)
    }
    assert dict(engine.scan()) == expected
    engine.close()

    reopened = Engine.open(home, threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH))
    assert dict(reopened.scan()) == expected, "a durable write did not survive recovery"
    reopened.close()


def test_snapshot_context_manager_releases_on_exit(tmp_path: Path) -> None:
    """The view is a resource: using it after release has to fail loudly."""
    options = threaded_options(memtable_bytes_threshold=NO_AUTO_FLUSH)
    engine = Engine.open(db_path(tmp_path), options)
    engine.put(b"alpha", b"one")
    engine.flush()

    with engine.snapshot() as view:
        assert view.get(b"alpha") == b"one"
        assert engine.live_snapshots == (view.sequence,)
    assert view.released
    assert engine.live_snapshots == ()
    view.release()

    try:
        view.get(b"alpha")
    except ClosedEngineError:
        pass
    else:
        raise AssertionError("a released view answered a read")

    assert repr(view).startswith("Snapshot(")
    engine.close()


def test_visible_versions_honours_snapshot_bands() -> None:
    """The retention rule, stated as data: who keeps what, and why."""
    elder = InternalEntry.put(b"k", 1, b"elder")
    middle = InternalEntry.put(b"k", 5, b"middle")
    newest = InternalEntry.put(b"k", 9, b"newest")

    def kept_with(snapshots: Sequence[int]) -> list[int]:
        streams = [iter([newest, middle, elder])]
        return [
            entry.sequence
            for entry in visible_versions(
                streams, drop_tombstones=False, snapshots=snapshots
            )
        ]

    # Nobody looking: only the newest version has any business existing.
    assert kept_with(()) == [9]

    # A view at 6 resolves to sequence 5, so that version cannot go.
    assert kept_with((6,)) == [9, 5]

    # Two views, two bands, so every version is somebody's answer.
    assert kept_with((2, 6)) == [9, 5, 1]


def test_visible_versions_keeps_a_tombstone_that_still_masks_something() -> None:
    """A marker younger than the oldest live view must never be dropped."""
    value = InternalEntry.put(b"k", 1, b"value")
    marker = InternalEntry.delete(b"k", 5)

    def sequences(snapshots: Sequence[int]) -> list[int]:
        return [
            entry.sequence
            for entry in visible_versions(
                [iter([marker, value])], drop_tombstones=True, snapshots=snapshots
            )
        ]

    # No elder view: both the marker and its victim are garbage.
    assert sequences(()) == []

    # The oldest view predates the marker, so the version below it survives and
    # the marker has to stay to hide that survivor from everybody else.
    assert sequences((2,)) == [5, 1]

    # A view younger than the marker sees a deletion either way, so nothing needs
    # to be kept: its own victim disappears together with the marker.
    assert sequences((7,)) == []


def test_concurrent_maintenance_issues_distinct_file_ids(tmp_path: Path) -> None:
    """Two maintenance paths allocating at once must never share a filename.

    A flush publishes a table while holding only ``_flush_lock``; a compaction
    allocates its output ids from the merge phase, which deliberately holds
    nothing. If the counter is not guarded, both are handed the same id, and two
    different tables get published at the same path — the second replacing the
    first atomically.

    Readers key their cache on ``file_id``, so the observable consequence is one
    table's index being read against another table's bytes: a block CRC failure
    under concurrency, which is about the last shape anyone would expect an
    allocation race to take.
    """
    options = threaded_options(
        block_size_bytes=256,
        memtable_bytes_threshold=512,
        level0_file_num_compaction_trigger=2,
        l1_bytes_trigger=512,
    )
    engine = Engine.open(db_path(tmp_path), options)
    gate = threading.Barrier(2)

    def writer() -> None:
        gate.wait(timeout=THREAD_TIMEOUT_SECONDS)
        for index in range(300):
            engine.put(key_for(index), value_for(index), WriteOptions(sync=False))

    def compactor() -> None:
        gate.wait(timeout=THREAD_TIMEOUT_SECONDS)
        for _ in range(8):
            engine.compact()

    spawn([writer, compactor])

    identifiers = [meta.file_id for level in engine.level_files for meta in level]
    assert len(identifiers) == len(set(identifiers)), (
        f"two tables were published under one file id: {sorted(identifiers)}"
    )

    # Reading every surviving table at its own offsets is exactly what a
    # duplicate id breaks, and it is the check that fails loudly rather than
    # returning plausible-looking wrong data.
    observed = list(engine.scan())
    assert observed, "the concurrent run left nothing readable"
    for user_key, value in observed:
        assert engine.get(user_key) == value
    engine.close()
