"""In-memory write buffer: the sorted view every read consults first.

A memtable is a skip list keyed by ``(user_key, -sequence)``. Skip lists give
expected-O(log n) insert and point lookup while remaining trivially iterable in
sorted order — exactly what a background flush needs when it hands Block-format
entries to the SSTable writer. No locking lives here: the engine owns
concurrency, and every mutating caller serialises through it.

Three properties are enforced structurally rather than by convention:

* **Structural serialisation.** One lock guards linking and traversal. A reader
  materialises the node run it means to walk and releases the lock immediately,
  so a writer is never blocked for the duration of a scan, while a traverser can
  never observe a half-linked tower. The engine still owns durability and version
  ordering; this lock protects nothing but the shape of one table.

* **Strictly increasing sequences.** Each table remembers the highest sequence
  it has admitted. A caller that tries to insert out of order — which would
  silently corrupt read-your-writes semantics by letting a stale version win —
  gets an error instead of a corrupted tree.
* **Newest-version-first within a user key.** Negated sequences mean the newest
  version of a key is always the *first* node reached, so ``get`` is a single
  descent and tombstone shadowing needs no extra bookkeeping.
* **Append-only growth.** Nothing is ever removed from a live table, which is
  why ``smallest_key``/``largest_key`` can be maintained by monotone comparison
  instead of rescanning.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from sable.errors import InvalidArgumentError
from sable.types import InternalEntry, InternalKey, KeyRange, OpType

__all__ = ["MAX_SKIP_LEVEL", "MemTable"]

MAX_SKIP_LEVEL: int = 12


class _SkipNode:
    """One tower in the skip list. ``next`` is indexed bottom-to-top."""

    __slots__ = ("entry", "next")

    def __init__(self, entry: InternalEntry, height: int) -> None:
        self.entry = entry
        self.next: list[_SkipNode | None] = [None] * height

    @property
    def sort_key(self) -> tuple[bytes, int]:
        return self.entry.sort_key


class _LevelSource:
    """Deterministic xorshift32 driven level generator.

    Using a reproducible PRNG instead of :mod:`random` keeps tree shape a pure
    function of insertion history, so a structural bug is reproducible from the
    same write sequence rather than hiding behind nondeterminism.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFF or 0x9E3779B9

    def next_level(self) -> int:
        level = 1
        while level < MAX_SKIP_LEVEL and (self._next() & 0x1) == 0:
            level += 1
        return level

    def _next(self) -> int:
        x = self._state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self._state = x & 0xFFFFFFFF
        return self._state


class MemTable:
    """Mutable write buffer, or read-only after :meth:`freeze`."""

    _instance_seeds = 0

    def __init__(self, *, first_sequence: int = 0) -> None:
        sentinel = InternalEntry(InternalKey(b"", 0, OpType.PUT), b"")
        self._head = _SkipNode(sentinel, MAX_SKIP_LEVEL)
        self._height = 1
        self._version_count = 0
        self._payload_bytes = 0
        # Seeding below the first expected sequence makes an under-shoot look
        # exactly like an in-table regression, so both are rejected by one rule.
        self._last_sequence = first_sequence - 1
        self._first_sequence = first_sequence
        self._frozen = False
        self._smallest_key: bytes | None = None
        self._largest_key: bytes | None = None
        self._mutation = threading.Lock()

        MemTable._instance_seeds += 1
        self._levels = _LevelSource(MemTable._instance_seeds * 0x2545F491)

    # -- introspection -----------------------------------------------------

    def __len__(self) -> int:
        """Number of stored versions, including superseded ones."""
        return self._version_count

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def version_count(self) -> int:
        return self._version_count

    @property
    def approximate_bytes(self) -> int:
        """Upper bound on resident payload bytes, driving flush thresholds."""
        return self._payload_bytes

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def first_sequence(self) -> int:
        """Sequence of the earliest admitted entry, or the offset when empty."""
        return self._first_sequence

    @property
    def smallest_key(self) -> bytes | None:
        return self._smallest_key

    @property
    def largest_key(self) -> bytes | None:
        return self._largest_key

    @property
    def is_empty(self) -> bool:
        return self._version_count == 0

    # -- mutation ----------------------------------------------------------

    def add(self, entry: InternalEntry) -> None:
        """Insert one versioned mutation.

        Args:
            entry: Mutation to stage. Its sequence must exceed every sequence
                already admitted by this table.

        Raises:
            InvalidArgumentError: The table is frozen, or the sequence
                regressed — both indicate a caller bug that would otherwise
                surface far away as a stale read.
        """
        with self._mutation:
            if self._frozen:
                raise InvalidArgumentError("cannot mutate a frozen memtable")
            if entry.sequence <= self._last_sequence:
                raise InvalidArgumentError(
                    f"sequence {entry.sequence} does not strictly increase past "
                    f"{self._last_sequence}"
                )

            self._link(entry)
            self._version_count += 1
            self._last_sequence = entry.sequence
            self._payload_bytes += entry.heap_bytes()
            self._widen_bounds(entry.user_key)

    def put(self, user_key: bytes, sequence: int, value: bytes) -> InternalEntry:
        """Stage a live value and return the entry actually stored."""
        entry = InternalEntry.put(user_key, sequence, value)
        self.add(entry)
        return entry

    def delete(self, user_key: bytes, sequence: int) -> InternalEntry:
        """Stage a tombstone and return the entry actually stored."""
        entry = InternalEntry.delete(user_key, sequence)
        self.add(entry)
        return entry

    def freeze(self) -> None:
        """Switch to read-only; the flush thread now owns the contents."""
        with self._mutation:
            self._frozen = True

    # -- reads -------------------------------------------------------------

    def get(self, user_key: bytes) -> InternalEntry | None:
        """Return the newest version of ``user_key``, tombstones included.

        Args:
            user_key: Exact key to resolve.

        Returns:
            The highest-sequence entry, or ``None`` when the table has never
            seen the key. Callers decide visibility: a tombstone result means
            "deleted here", which shadows anything in lower levels.
        """
        with self._mutation:
            node = self._descend_to(user_key)
            candidate = None if node is None else node.entry
        if candidate is None or candidate.user_key != user_key:
            return None
        return candidate

    def get_visible(
        self, user_key: bytes, visible_sequence: int | None
    ) -> InternalEntry | None:
        """Return the newest version of ``user_key`` the view may resolve to.

        A key may hold a newer, post-snapshot version beside an older one the
        view is still entitled to. A bare ``get`` returns only the newest version
        and would therefore hide the older visible one from a snapshot read,
        leaving ``get`` and ``scan`` to disagree on what a pinned sequence number
        resolves to. Walking the version run newest-first and stopping at the
        first ``sequence <= visible_sequence`` returns the same entry a range
        scan would, so the two read paths stay consistent.

        Args:
            user_key: Exact key to resolve.
            visible_sequence: Upper sequence bound, or ``None`` for "latest wins".

        Returns:
            The highest-sequence entry at or below ``visible_sequence`` (tombstones
            included), or ``None`` when the table has never seen ``user_key``.
        """
        with self._mutation:
            node = self._descend_to(user_key)
            while node is not None:
                candidate = node.entry
                if candidate.user_key != user_key:
                    break
                if visible_sequence is None or candidate.sequence <= visible_sequence:
                    return candidate
                node = node.next[0]
        return None

    def iter_range(self, key_range: KeyRange | None = None) -> Iterator[InternalEntry]:
        """Yield every stored version inside ``key_range``, newest-first per key.

        Args:
            key_range: Inclusive window; unbounded when omitted.

        Returns:
            An iterator over a snapshot of the matching nodes. The run is
            materialised under the structural lock and then walked lock-free, so
            concurrent writes neither block nor observe mid-scan interference.
        """
        with self._mutation:
            nodes = list(self._nodes_in(key_range))
        return (node.entry for node in nodes)

    def iter_latest(self, key_range: KeyRange | None = None) -> Iterator[InternalEntry]:
        """Yield only the newest version of each key in range.

        Args:
            key_range: Same window semantics as :meth:`iter_range`.

        Yields:
            One entry per distinct user key. Because versions within a key are
            ordered newest-first, superseded versions are simply skipped — which
            is what lets a flush collapse a hot key to a single block record.
        """
        seen: bytes | None = None
        for entry in self.iter_range(key_range):
            if seen is not None and entry.user_key == seen:
                continue
            seen = entry.user_key
            yield entry

    def key_edge_slice(self) -> tuple[bytes | None, bytes | None]:
        """Return ``(smallest_key, largest_key)`` for SSTable metadata."""
        with self._mutation:
            return self._smallest_key, self._largest_key

    # -- internals ---------------------------------------------------------

    def _link(self, entry: InternalEntry) -> None:
        target = entry.sort_key
        update: list[_SkipNode] = [self._head] * MAX_SKIP_LEVEL
        current = self._head

        for level in range(self._height - 1, -1, -1):
            forward = current.next[level]
            while forward is not None and forward.sort_key < target:
                current = forward
                forward = current.next[level]
            update[level] = current

        node = _SkipNode(entry, self._levels.next_level())
        height = len(node.next)
        # Levels above the current table height were never traversed; the head
        # sentinel is therefore the correct predecessor for each of them.
        for level in range(self._height, height):
            update[level] = self._head

        for level in range(height):
            slot = update[level]
            node.next[level] = slot.next[level]
            slot.next[level] = node

        self._height = max(self._height, height)

    def _descend_to(self, user_key: bytes) -> _SkipNode | None:
        """Find the first node whose sort key ``>= (user_key, -infinity)``."""
        target = (user_key, -(2**62))
        current = self._head
        for level in range(self._height - 1, -1, -1):
            forward = current.next[level]
            while forward is not None and forward.sort_key < target:
                current = forward
                forward = current.next[level]
        return current.next[0]

    def _nodes_in(self, key_range: KeyRange | None) -> Iterator[_SkipNode]:
        node = self._head.next[0]
        if key_range is None:
            while node is not None:
                yield node
                node = node.next[0]
            return

        while node is not None and not key_range.contains(node.entry.user_key):
            node = node.next[0]
        while node is not None:
            if not key_range.contains(node.entry.user_key):
                return
            yield node
            node = node.next[0]

    def _widen_bounds(self, user_key: bytes) -> None:
        if self._smallest_key is None or user_key < self._smallest_key:
            self._smallest_key = user_key
        if self._largest_key is None or user_key > self._largest_key:
            self._largest_key = user_key
