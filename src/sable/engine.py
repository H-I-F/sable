"""The engine: version bookkeeping and the read path.

Reads descend freshest-first and stop at the first version found, regardless of
whether that version is live or a tombstone:

    active memtable -> immutable memtables -> L0 files -> L1 .. Ln files

Stopping *at* a tombstone rather than skipping past it is the whole point: the
tombstone is what masks elder versions sitting in lower levels, so returning it
to the caller is not an error condition, it is the hiding mechanism.

Version state
-------------
Every mutation to the file set is published through ``MANIFEST-*`` files whose
path is recorded in ``CURRENT``. Rather than appending deltas forever, each
version change rewrites the manifest as a complete image and swaps it in
atomically via :func:`sable.fsutil.atomic_write_bytes`. The cost is O(files) per
change, dwarfed by the compaction that triggered it, and the benefit is that a
recovery needs no replay semantics: the last visible manifest is the truth, not
a log whose meaning depends on every frame preceding it.
Snapshot concurrency
--------------------
Three distinct things are in flight at once once writers, readers and a
compaction all share one engine, and each needs a different guarantee:

* **A file being read must not vanish.** Readers take a *lease*: every table they
  consult has its reference count raised for exactly as long as they might touch
  it. A compaction may drop a file from the version set immediately, but only
  unlinks it once the last lease is released.
* **A version being read must not be merged away.** Pinning files cannot help
  here — the replacement file simply lacks the elder version — so the merge itself
  is snapshot-aware (see :mod:`sable.compaction`).
* **A structure being walked must not mutate under the walker.** Memtable written
  runs are materialised at acquisition time, and the merge/flush of a compaction
  holds no engine lock at all, so the expensive phases overlap real work instead
  of stalling every reader behind them.

Locking order is single and therefore deadlock-free: ``_compaction_lock`` and
``_flush_lock`` are leaf scopes that never outlive an engine state section, and
``_lock`` is always the innermost of the three. Nothing acquires them in any
other order, and no engine method calls another while holding more than ``_lock``.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import struct
import threading
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from sable.compaction import CompactionJob, plan_compaction, run_compaction
from sable.errors import (
    ClosedEngineError,
    CorruptManifestError,
    InvalidArgumentError,
    SableError,
)
from sable.fsutil import atomic_write_bytes, fsync_directory
from sable.memtable import MemTable
from sable.sstable import SSTableReader, SSTableWriter, looks_like_temporary
from sable.types import (
    MAX_KEY_BYTES,
    EngineOptions,
    EngineStats,
    InternalEntry,
    KeyRange,
    ReadOptions,
    SSTableMeta,
    WriteOptions,
)
from sable.wal import WriteAheadLog

__all__ = ["Engine", "HOME_FILE_LAYOUT", "Snapshot"]

MANIFEST_MAGIC = b"SMAN"
MANIFEST_VERSION = 1
_MANIFEST_HEADER = struct.Struct("<4sI")

CURRENT_FILE = "CURRENT"
MANIFEST_PREFIX = "MANIFEST-"
WAL_PREFIX = "wal-"
WAL_SUFFIX = ".log"
_EMPTY_SEQUENCE = -1

HOME_FILE_LAYOUT = (CURRENT_FILE, MANIFEST_PREFIX)


class Snapshot:
    """A read-only window onto the tree, pinned at one sequence number.

    A snapshot stores exactly one integer, and that is enough to make concurrent
    writes invisible to it:

    * reads through the view ignore every version newer than ``sequence``, so
      later writes cannot change what the view observes;
    * compaction retains elder versions inside this view's visibility band and
      defers unlinking any file the view has leased;
    * nothing is copied, so acquisition is O(1) no matter how large the tree is.

    The view is a *resource*, not a result: it must be :meth:`release`d, or used
    as a context manager, otherwise every version it protects is retained
    forever and the tree can never converge.
    """

    __slots__ = ("_engine", "_released", "_sequence")

    def __init__(self, engine: Engine, sequence: int) -> None:
        self._engine = engine
        self._sequence = sequence
        self._released = False

    @property
    def sequence(self) -> int:
        """Upper bound on the versions this view can resolve to."""
        return self._sequence

    @property
    def released(self) -> bool:
        return self._released

    def get(self, user_key: bytes) -> bytes | None:
        """Resolve ``user_key`` as of this view's sequence."""
        self._require_live()
        return self._engine._get_at(user_key, self._sequence)

    def scan(
        self,
        start: bytes | None = None,
        end: bytes | None = None,
        *,
        start_inclusive: bool = True,
        end_exclusive: bool = True,
        limit: int | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """Walk ``[start, end)`` as of this view's sequence."""
        self._require_live()
        return self._engine._scan_at(
            start,
            end,
            start_inclusive=start_inclusive,
            end_exclusive=end_exclusive,
            limit=limit,
            visible_sequence=self._sequence,
        )

    def release(self) -> None:
        """Drop the view. Idempotent, and required for the tree to converge."""
        if self._released:
            return
        self._released = True
        self._engine._release_snapshot(self)

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "released" if self._released else "live"
        return f"Snapshot(sequence={self._sequence}, {state})"

    def _require_live(self) -> None:
        if self._released:
            raise ClosedEngineError("snapshot has been released")


def _is_visible(entry: InternalEntry, visible_sequence: int | None) -> bool:
    """True when ``entry`` may be resolved by a view at ``visible_sequence``.

    Args:
        entry: Candidate version.
        visible_sequence: Upper sequence bound, or ``None`` for "latest wins".
    """
    return visible_sequence is None or entry.sequence <= visible_sequence


def _visible_stream(
    stream: Iterator[InternalEntry], visible_sequence: int | None
) -> Iterator[InternalEntry]:
    """Filter versions a view cannot see, preserving the source's ordering."""
    if visible_sequence is None:
        return stream
    return (entry for entry in stream if entry.sequence <= visible_sequence)


def _table_overlaps(meta: SSTableMeta, key_range: KeyRange) -> bool:
    """True when ``meta``'s key span can contribute anything to ``key_range``."""
    if not meta.largest_key:
        return False
    return key_range.overlaps(meta.smallest_key, meta.largest_key)


class Engine:
    """A single-node LSM-tree key-value store rooted at one directory."""

    def __init__(self, home: Path, options: EngineOptions) -> None:
        self._home = Path(home)
        self._options = options
        self._lock = threading.RLock()
        self._readers: dict[int, SSTableReader] = {}
        self._active = MemTable()
        self._immutable: list[MemTable] = []
        self._levels: list[list[SSTableMeta]] = [[] for _ in range(options.max_levels)]
        self._next_file_id = 1
        self._last_sequence = _EMPTY_SEQUENCE
        self._stats = EngineStats()
        self._closed = False
        self._wal: WriteAheadLog | None = None
        self._wal_generation = 0
        self._pinned_obsolete: list[SSTableMeta] = []
        self._snapshots: list[Snapshot] = []
        # Three locks, three jobs. ``_lock`` guards every field above; the two
        # operation locks serialise one kind of maintenance each so that the
        # expensive part of either may run outside ``_lock`` altogether.
        self._flush_lock = threading.Lock()
        self._compaction_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, home: Path, options: EngineOptions | None = None) -> Engine:
        """Create or recover a database directory.

        Args:
            home: Directory holding ``CURRENT``, the manifest, WAL segments and
                SSTable files.
            options: Engine configuration; defaults are used when omitted.

        Returns:
            An open engine that the caller must later :meth:`close`.

        Raises:
            InvalidArgumentError: The directory is missing when creation was not
                requested, or already holds a database when that was forbidden.
            CorruptManifestError: The visible manifest is unreadable or names
                files that no longer exist.
        """
        home = Path(home)
        engine = cls(home, options or EngineOptions())
        engine._bootstrap()
        return engine

    def close(self) -> None:
        """Release every descriptor. Idempotent, and therefore safe on exit."""
        with self._flush_lock, self._compaction_lock, self._lock:
            if self._closed:
                return
            self._closed = True
            if self._wal is not None:
                self._wal.close()
                self._wal = None
            self._snapshots.clear()
            for reader in self._readers.values():
                reader.close()
            self._readers.clear()
            self._discard_retired_files()

    @property
    def home(self) -> Path:
        return self._home

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def options(self) -> EngineOptions:
        return self._options

    @property
    def stats(self) -> EngineStats:
        return self._stats.copy()

    @property
    def last_sequence(self) -> int:
        """Highest sequence admitted so far; -1 for a brand-new store."""
        return self._last_sequence

    @property
    def level_files(self) -> list[list[SSTableMeta]]:
        """Per-level table metadata, newest first inside each level."""
        return [list(level) for level in self._levels]

    # -- reads -------------------------------------------------------------

    def get(self, user_key: bytes, read_options: ReadOptions | None = None) -> bytes | None:
        """Resolve the visible value of ``user_key``.

        Args:
            user_key: Exact key to resolve.
            read_options: Verification strictness; ignored apart from being
                forwarded to SSTable readers.

        Returns:
            The live value, or ``None`` if the key is absent or deleted.
        """
        del read_options
        return self._get_at(user_key, None)

    def snapshot(self) -> Snapshot:
        """Capture a read-only view pinned at the current sequence.

        Nothing is copied. Subsequent writes simply carry sequences above the
        captured one, so every later read through the view filters them out.

        Returns:
            A :class:`Snapshot` the caller owns. It must be released — directly
            or by using it as a context manager — because until it is, the tree
            retains every version it could still observe.
        """
        with self._lock:
            self._require_open()
            view = Snapshot(self, self._last_sequence)
            self._snapshots.append(view)
            return view

    @property
    def live_snapshots(self) -> tuple[int, ...]:
        """Sequence numbers of every unreleased view, ascending."""
        with self._lock:
            return tuple(sorted(view.sequence for view in self._snapshots))

    def scan(
        self,
        start: bytes | None = None,
        end: bytes | None = None,
        *,
        start_inclusive: bool = True,
        end_exclusive: bool = True,
        limit: int | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """Walk every visible key in ``[start, end)`` in ascending order.

        Args:
            start: Lower bound; unbounded when ``None``.
            end: Upper bound; unbounded when ``None``.
            start_inclusive: Whether ``start`` itself is included.
            end_exclusive: Whether ``end`` is excluded.
            limit: Stop after this many visible pairs.

        Yields:
            ``(key, value)`` pairs. Deleted keys are absent from the output, not
            yielded with a sentinel: the tombstone was consumed during the merge.

        Raises:
            InvalidArgumentError: A bound is not bytes, or ``limit`` is negative.
        """
        return self._scan_at(
            start,
            end,
            start_inclusive=start_inclusive,
            end_exclusive=end_exclusive,
            limit=limit,
            visible_sequence=None,
        )

    def _get_at(self, user_key: bytes, visible_sequence: int | None) -> bytes | None:
        """Point lookup at one sequence, or at the latest one when ``None``."""
        self._require_live(user_key)
        entry = self._lookup_entry(user_key, visible_sequence=visible_sequence)
        with self._lock:
            self._stats.gets += 1
            hit = entry is not None and not entry.is_tombstone
            if hit:
                self._stats.get_hits += 1
        return None if entry is None or entry.is_tombstone else entry.value

    def _scan_at(
        self,
        start: bytes | None,
        end: bytes | None,
        *,
        start_inclusive: bool,
        end_exclusive: bool,
        limit: int | None,
        visible_sequence: int | None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """Range walk at one sequence, or at the latest one when ``None``."""
        self._require_open()
        for bound in (start, end):
            if bound is not None and not isinstance(bound, (bytes, bytearray)):
                raise InvalidArgumentError("scan bounds must be bytes")
        if limit is not None and limit < 0:
            raise InvalidArgumentError("limit must be non-negative")

        key_range = KeyRange(start, end, start_inclusive, end_exclusive)
        with self._lock:
            self._stats.scans += 1

        emitted = 0
        for entry in self._versioned_scan(key_range, visible_sequence):
            if entry.is_tombstone:
                continue
            yield entry.user_key, entry.value
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    def _versioned_scan(
        self, key_range: KeyRange, visible_sequence: int | None
    ) -> Iterator[InternalEntry]:
        """Merge every source into one ordered stream of winning versions.

        Sources are the active memtable, each immutable memtable, and every
        SSTable at every level. ``heapq.merge`` over ``InternalEntry.sort_key``
        produces the same (key asc, sequence desc) order each source already
        guarantees internally, so the newest version of a key necessarily arrives
        first and elder versions are dropped by identity comparison alone.

        The whole walk runs inside a lease. That lease is the reason a concurrent
        compaction cannot unlink anything this reader may still reach: a file
        dropped from the version set halfway through a scan stays on disk because
        its final reference belongs to this iterator.
        """
        with self._lease_view() as (tables, metas):
            streams: list[Iterator[InternalEntry]] = [
                _visible_stream(table.iter_range(key_range), visible_sequence)
                for table in tables
            ]
            for meta in metas:
                if not _table_overlaps(meta, key_range):
                    continue
                if visible_sequence is not None and meta.min_sequence > visible_sequence:
                    # Every version here postdates the view; consulting it could
                    # only produce entries the merge would filter anyway.
                    continue
                reader = self._reader_for(meta)
                streams.append(_visible_stream(reader.iter_slice(key_range), visible_sequence))

            previous_key: bytes | None = None
            for entry in heapq.merge(*streams, key=lambda item: item.sort_key):
                if previous_key is not None and entry.user_key == previous_key:
                    continue
                previous_key = entry.user_key
                yield entry

    def _lookup_entry(
        self, user_key: bytes, *, visible_sequence: int | None = None
    ) -> InternalEntry | None:
        """Return the first visible version, tombstones included.

        Search stops at the first *visible* version found. A tombstone is
        therefore returned rather than skipped: it is what masks elder versions
        in lower levels, so yielding it is the hiding mechanism, not a miss.
        """
        with self._lease_view() as (tables, metas):
            for table in tables:
                found = table.get(user_key)
                if found is not None and _is_visible(found, visible_sequence):
                    return found

            for meta in metas:
                if not meta.covers(user_key):
                    continue
                if visible_sequence is not None and meta.min_sequence > visible_sequence:
                    continue
                hit = self._reader_for(meta).get_entry(user_key, visible_sequence=visible_sequence)
                if hit is not None:
                    # A file may hold several versions of one key once retention
                    # has kept them, so the bound is applied inside the file too
                    # rather than by skipping the file wholesale.
                    return hit
        return None

    @contextlib.contextmanager
    def _lease_view(self) -> Iterator[tuple[list[MemTable], list[SSTableMeta]]]:
        """Capture the readable sources and pin every file in one atomic step.

        Grabbing both lists raises no opinion; doing it inside the same critical
        section as the ``refs`` increment is the whole point. Otherwise a
        compaction could slip between "this reader resolved that file" and "this
        reader pinned it", observe zero references, and unlink the file a read is
        about to consult.

        Yields:
            ``(memtables, metas)`` in newest-first order, every meta pinned. The
            lease is released when the block exits, including when the caller is
            abandoned mid-iteration and the generator is closed by the GC.
        """
        with self._lock:
            tables = [self._active, *self._immutable]
            metas = [meta for level in self._levels for meta in level]
            for meta in metas:
                meta.pin()
        try:
            yield tables, metas
        finally:
            expired = [meta for meta in metas if meta.unpin() == 0]
            if expired:
                self._release_retired(expired)

    def _release_retired(self, expired: Sequence[SSTableMeta]) -> None:
        """Drop readers and bytes for retired files nobody references now."""
        unlinked = self._discard_retired_files(expired)
        with self._lock:
            for meta in unlinked:
                reader = self._readers.pop(meta.file_id, None)
                if reader is not None:
                    reader.close()
        if unlinked:
            self._seal_home_directory()

    def _release_snapshot(self, view: Snapshot) -> None:
        """Forget an unreleased view so its protected versions become droppable."""
        with self._lock:
            if view in self._snapshots:
                self._snapshots.remove(view)

    def _snapshot_sequences(self) -> list[int]:
        """Ascending sequences of live views, for snapshot-aware merging."""
        with self._lock:
            return sorted(view.sequence for view in self._snapshots)

    def _discard_retired_files(
        self, candidates: Sequence[SSTableMeta] | None = None
    ) -> list[SSTableMeta]:
        """Unlink retired files whose last lease has just expired.

        Args:
            candidates: Files whose reference count was decremented. ``None``
                means "every retired file", used during shutdown.

        Returns:
            The files actually unlinked, so the caller can fsync the directory
            once for the batch instead of once per deletion.
        """
        with self._lock:
            if candidates is None:
                unlinked = [meta for meta in self._pinned_obsolete if meta.refs == 0]
                self._pinned_obsolete = [meta for meta in self._pinned_obsolete if meta.refs]
            else:
                expired = {id(meta): meta for meta in candidates}
                unlinked = [meta for meta in self._pinned_obsolete if id(meta) in expired]
                self._pinned_obsolete = [
                    meta for meta in self._pinned_obsolete if id(meta) not in expired
                ]
            for meta in unlinked:
                (self._home / meta.file_name).unlink(missing_ok=True)
            return unlinked

    # -- writes ------------------------------------------------------------

    def put(
        self, user_key: bytes, value: bytes, write_options: WriteOptions | None = None
    ) -> None:
        """Persist a live value.

        The write follows append-before-apply: the frame is allocated a sequence
        number, reaches stable storage in the WAL, and only then becomes visible
        in the memtable. A crash between the two loses the write but never makes
        it visible-then-lost, which is what lets "acknowledged" mean "durable".

        Args:
            user_key: Key to write.
            value: Payload; may be empty, which is distinct from absence.
            write_options: Per-call override for fsync behaviour.

        Raises:
            InvalidArgumentError: Key or value is malformed.
        """
        self._require_live(user_key)
        if not isinstance(value, (bytes, bytearray)):
            raise InvalidArgumentError("value must be bytes")
        self._stage_mutation(
            lambda sequence: InternalEntry.put(user_key, sequence, bytes(value)),
            write_options,
        )
        with self._lock:
            self._stats.puts += 1

    def delete(self, user_key: bytes, write_options: WriteOptions | None = None) -> None:
        """Persist a tombstone, following the same append-before-apply order."""
        self._require_live(user_key)
        self._stage_mutation(
            lambda sequence: InternalEntry.delete(user_key, sequence), write_options
        )
        with self._lock:
            self._stats.deletes += 1

    def _stage_mutation(
        self,
        factory: Callable[[int], InternalEntry],
        write_options: WriteOptions | None,
    ) -> None:
        """Sequence -> durable WAL frame -> visible memtable entry.

        Only the publish keeps ``_lock``; housekeeping runs after it is released,
        so one slow writer never holds the tree's state hostage to an fsync of
        someone else's bytes.
        """
        sync = None if write_options is None else write_options.sync
        with self._lock:
            self._require_open()
            entry = factory(self._allocate_sequence())
            self._append_frame(entry, sync=sync)
            self._active.add(entry)
            over_budget = (
                self._active.approximate_bytes >= self._options.memtable_bytes_threshold
            )
        if over_budget:
            self._seal_over_budget()
            self._maybe_compact()

    def _append_frame(self, entry: InternalEntry, *, sync: bool | None) -> None:
        if self._wal is None:
            self._wal = self._open_wal(self._wal_generation or 1)
        self._wal.append(entry, sync=sync)
        self._stats.wal_bytes_written = self._wal.bytes_appended

    def flush(self) -> None:
        """Freeze the active memtable and publish it as a new L0 table.

        Raises:
            SableError: Nothing has been written since the last flush.
        """
        with self._flush_lock:
            with self._lock:
                self._require_open()
                frozen = self._active
                if frozen.is_empty:
                    raise SableError("nothing to flush")
                frozen.freeze()
                self._immutable.insert(0, frozen)
                self._active = MemTable()
            self._seal_one_immutable()
            # Compaction is deliberately *not* triggered from here. An explicit
            # flush is an operator action with an observable result; folding a
            # merge into it would make level state depend on timing rather than
            # on what the caller asked for. The budget-driven path below owns
            # compaction, the way a background compactor would.

    # -- internals ---------------------------------------------------------

    def _seal_over_budget(self) -> None:
        """Freeze proactively once the memtable crosses its budget.

        Flushing inline (rather than on a background thread) keeps failure modes
        observable: crossing the threshold either publishes successfully or the
        write that triggered it raises. There is no hidden queue to lose, and no
        separate scheduler whose failure could go unnoticed.

        Concurrent writers lose the race instead of blocking on it. Whoever fails
        to take ``_flush_lock`` simply returns: the winner's work covers their
        entries too, so waiting would buy nothing.
        """
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                self._require_open()
                table = self._active
                if table.is_empty:
                    return
                if table.approximate_bytes < self._options.memtable_bytes_threshold:
                    return
                table.freeze()
                self._immutable.insert(0, table)
                self._active = MemTable()
            self._seal_one_immutable()
        finally:
            self._flush_lock.release()

    def _seal_one_immutable(self) -> None:
        """Publish the newest immutable table as one L0 file.

        Callers hold ``_flush_lock``, which is what makes the "is it still mine
        to publish?" check below race-free. The Sealed table deliberately stays
        in ``_immutable`` until its replacement is linked: dropping visibility
        before durable bytes exist would make entries vanish from a concurrent
        reader's view.
        """
        with self._lock:
            self._require_open()
            if not self._immutable:
                return
            table = self._immutable[0]
            file_id = self._allocate_file_id()

        # The write itself holds no engine lock. SSTable bytes are invisible
        # until the manifest names them, so the only cost of excluding this from
        # the critical section is that readers keep using the memtable - which
        # they could do anyway, and which costs them nothing.
        meta = self._write_table(table, level=0, file_id=file_id)

        with self._lock:
            if table in self._immutable:
                self._immutable.remove(table)
            self._levels[0].insert(0, meta)
            self._stats.flushes += 1
            self._publish_manifest()
        # Only now that the table is visible in the manifest is it safe to
        # retire the log that carried those same entries.
        self._rotate_wal()

    # -- write-ahead log ---------------------------------------------------

    def _wal_path(self, generation: int) -> Path:
        return self._home / f"{WAL_PREFIX}{generation:06d}{WAL_SUFFIX}"

    def _open_wal(self, generation: int) -> WriteAheadLog:
        log = WriteAheadLog.open(
            self._wal_path(generation), sync=self._options.default_write_sync
        )
        self._wal_generation = generation
        return log

    def _recover_wal(self) -> None:
        """Replay every surviving segment and reopen the newest for appending."""
        segments = sorted(
            self._home.glob(f"{WAL_PREFIX}*{WAL_SUFFIX}"),
            key=lambda path: path.stat().st_size,
        )
        segments.sort(key=lambda path: path.name)

        for segment in segments:
            result = WriteAheadLog.recover(segment)
            if result.torn_tail:
                WriteAheadLog.truncate_torn_tail(segment, result.valid_bytes)
            for entry in result.entries:
                self._observe_sequence(entry.sequence)
                self._active.add(entry)

        generation = self._parse_generation(segments[-1]) if segments else 1
        self._wal = self._open_wal(generation)
        self._discard_wals(keep=self._wal.path)

    def _parse_generation(self, segment: Path) -> int:
        digits = segment.name[len(WAL_PREFIX) : -len(WAL_SUFFIX)]
        return int(digits)

    def _rotate_wal(self) -> None:
        """Open the next generation, then retire everything it supersedes."""
        superseded = self._wal
        self._wal = self._open_wal(self._wal_generation + 1)
        if superseded is not None:
            superseded.close()
        self._discard_wals(keep=self._wal.path)

    def _discard_wals(self, keep: Path) -> None:
        for segment in self._home.glob(f"{WAL_PREFIX}*{WAL_SUFFIX}"):
            if segment.name == keep.name:
                continue
            segment.unlink(missing_ok=True)
        self._seal_home_directory()

    def _observe_sequence(self, sequence: int) -> None:
        self._last_sequence = max(self._last_sequence, sequence)

    def _require_live(self, user_key: bytes) -> None:
        self._require_open()
        if not isinstance(user_key, (bytes, bytearray)):
            raise InvalidArgumentError("key must be bytes")
        if len(user_key) > MAX_KEY_BYTES:
            raise InvalidArgumentError("key exceeds the supported maximum")

    def _require_open(self) -> None:
        if self._closed:
            raise ClosedEngineError("engine is closed")

    def _allocate_sequence(self) -> int:
        self._last_sequence += 1
        return self._last_sequence

    def compact(self) -> bool:
        """Run one compaction if the tree needs one.

        Returns:
            ``True`` when work was done. A caller wiring this into a maintenance
            loop can drive the tree to balance by repeating until ``False``.
        """
        with self._compaction_lock:
            return self._compact_once()

    def _maybe_compact(self) -> None:
        """Compact opportunistically, without queueing behind another writer.

        Losing the race is free: whoever currently holds the compactor slot is
        already rewriting exactly the files this caller would have chosen.
        """
        if not self._compaction_lock.acquire(blocking=False):
            return
        try:
            self._compact_once()
        finally:
            self._compaction_lock.release()

    def _compact_once(self) -> bool:
        """Plan, merge and install exactly one job; caller holds the compactor.

        Only the plan and the install take ``_lock``. The merge itself reads
        immutable inputs and writes a file nobody has named yet, so it can run
        while other threads read and write. Serialising it would hide the very
        overlap this design exists to survive.
        """
        with self._lock:
            self._require_open()
            job = plan_compaction(self._levels, self._options)
            if job is None:
                return False
            # Captured now rather than after the merge: a view acquired mid-merge
            # is entitled to fewer versions, but not to none at all.
            snapshots = self._snapshot_sequences()

        outputs = run_compaction(
            job, self._home, self._options, self._allocate_file_id, snapshots=snapshots
        )
        self._install_compaction(job, outputs)
        with self._lock:
            self._stats.compactions += 1
        return True

    def _install_compaction(self, job: CompactionJob, outputs: list[SSTableMeta]) -> None:
        """Swap merged outputs in for their inputs and retire what is dead.

        Args:
            job: The completed work item.
            outputs: Tables produced at ``job.target_level``.
        """
        retired_ids = job.input_ids
        for level_index, level in enumerate(self._levels):
            self._levels[level_index] = [
                meta for meta in level if meta.file_id not in retired_ids
            ]
        target_level = self._levels[job.target_level]
        target_level.extend(outputs)
        target_level.sort(key=lambda meta: meta.file_id, reverse=True)
        self._publish_manifest()

        for meta in job.inputs:
            if meta.refs > 0:
                # A live reader still holds this file. The version set has already
                # forgotten it - dropping it from the view cannot affect future
                # reads - so only the bytes have to wait, and they wait on the
                # last lease rather than on a timeout.
                self._pinned_obsolete.append(meta)
                continue
            reader = self._readers.pop(meta.file_id, None)
            if reader is not None:
                reader.close()
            (self._home / meta.file_name).unlink(missing_ok=True)
        self._seal_home_directory()

    def _allocate_file_id(self) -> int:
        """Reserve the next file identifier.

        The counter is guarded because two maintenance paths allocate
        concurrently: a flush publishes a table while holding only
        ``_flush_lock``, and a compaction allocates its output ids from the merge
        phase, which deliberately holds nothing at all. Read-modify-write on the
        counter is not atomic, so without this lock both callers can be handed
        the same id.

        Two tables sharing an id share a filename, and the second publish
        atomically replaces the first at that path. Readers key their cache on
        ``file_id``, so they would then consult one table's index against
        another table's bytes — which surfaces as a block CRC failure under
        concurrency, not as anything recognisable. ``_lock`` is reentrant, so
        callers that already hold it are unaffected.

        Returns:
            A file id never previously issued by this engine instance.
        """
        with self._lock:
            file_id = self._next_file_id
            self._next_file_id += 1
            return file_id

    def _write_table(self, table: MemTable, *, level: int, file_id: int) -> SSTableMeta:
        target = self._home / f"{file_id:06d}.sst"
        writer = SSTableWriter(target, self._options, file_id=file_id, level=level)
        for entry in table.iter_latest():
            writer.add_entry(entry)
        return writer.finish()

    def _reader_for(self, meta: SSTableMeta) -> SSTableReader:
        """Return the cached reader for ``meta``, opening it on first use."""
        reader = self._readers.get(meta.file_id)
        if reader is None:
            path = self._home / meta.file_name
            if not path.exists():
                raise CorruptManifestError(
                    f"manifest references {meta.file_name} which is missing from disk"
                )
            reader = SSTableReader.open(path)
            self._readers[meta.file_id] = reader
        return reader

    # -- version bookkeeping ----------------------------------------------

    def _bootstrap(self) -> None:
        exists = self._home.exists() and any(self._home.iterdir()) if self._home.exists() else False
        if not self._home.exists():
            if not self._options.create_if_missing:
                raise InvalidArgumentError(
                    f"{self._home} does not exist and creation was not requested"
                )
            self._home.mkdir(parents=True, exist_ok=True)
        elif exists and self._options.error_if_exists:
            raise InvalidArgumentError(f"{self._home} already holds a database")

        manifest = self._read_current()
        if manifest is not None and manifest.exists():
            self._load_manifest(manifest)
        self._recover_wal()

    def _manifest_path(self) -> Path:
        manifest = self._read_current()
        if manifest is None:
            manifest = self._home / f"{MANIFEST_PREFIX}000001"
        return manifest

    def _read_current(self) -> Path | None:
        current = self._home / CURRENT_FILE
        if not current.exists():
            return None
        name = current.read_text(encoding="utf-8").strip()
        return self._home / name if name else None

    def _publish_manifest(self) -> None:
        """Write a complete version image and point CURRENT at it atomically."""
        files = [
            {
                "id": meta.file_id,
                "level": meta.level,
                "size": meta.file_size,
                "entries": meta.num_entries,
                "blocks": meta.num_blocks,
                "min_key": meta.smallest_key.hex(),
                "max_key": meta.largest_key.hex(),
                "min_seq": meta.min_sequence,
                "max_seq": meta.max_sequence,
            }
            for level in self._levels
            for meta in level
        ]
        document = {
            "version": MANIFEST_VERSION,
            "next_file_id": self._next_file_id,
            "last_sequence": self._last_sequence,
            "files": files,
        }
        blob = json.dumps(document, separators=(",", ":")).encode()
        payload = _MANIFEST_HEADER.pack(MANIFEST_MAGIC, _crc32(blob)) + blob

        generation = self._home / f"{MANIFEST_PREFIX}{(self._next_file_id):06d}"
        atomic_write_bytes(generation, payload)
        atomic_write_bytes(self._home / CURRENT_FILE, generation.name.encode())
        self._expire_old_manifests(keep=generation)

    def _expire_old_manifests(self, keep: Path) -> None:
        """Delete superseded manifests now that ``keep`` is safely linked.

        Args:
            keep: The generation CURRENT points at; never removed.
        """
        for candidate in sorted(self._home.glob(f"{MANIFEST_PREFIX}*")):
            if candidate.name == keep.name or looks_like_temporary(candidate):
                continue
            candidate.unlink()
        self._seal_home_directory()

    def _load_manifest(self, manifest: Path) -> None:
        raw = manifest.read_bytes()
        if len(raw) < _MANIFEST_HEADER.size:
            raise CorruptManifestError(f"{manifest.name} is too short to hold a header")
        magic, stored_crc = _MANIFEST_HEADER.unpack_from(raw, 0)
        blob = raw[_MANIFEST_HEADER.size :]
        if magic != MANIFEST_MAGIC or stored_crc != _crc32(blob):
            raise CorruptManifestError(f"{manifest.name} failed CRC32 verification")

        try:
            document = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise CorruptManifestError(f"{manifest.name} is not valid JSON: {exc}") from exc
        if document.get("version") != MANIFEST_VERSION:
            raise CorruptManifestError(f"{manifest.name} uses an unsupported version")

        for record in document["files"]:
            meta = SSTableMeta(
                file_id=record["id"],
                level=record["level"],
                file_size=record["size"],
                num_entries=record["entries"],
                num_blocks=record["blocks"],
                smallest_key=bytes.fromhex(record["min_key"]),
                largest_key=bytes.fromhex(record["max_key"]),
                min_sequence=record["min_seq"],
                max_sequence=record["max_seq"],
            )
            self._reader_for(meta)
            self._levels[record["level"]].append(meta)

        for level in self._levels:
            level.sort(key=lambda meta: meta.file_id, reverse=True)

        known_ids = [meta.file_id for level in self._levels for meta in level]
        known_sequences = [meta.max_sequence for level in self._levels for meta in level]
        self._next_file_id = max(document["next_file_id"], max(known_ids, default=0) + 1)
        self._last_sequence = max(
            document["last_sequence"], max(known_sequences, default=_EMPTY_SEQUENCE)
        )

    def _seal_home_directory(self) -> None:
        fsync_directory(self._home)


def _crc32(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF
