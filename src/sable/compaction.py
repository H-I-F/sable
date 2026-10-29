"""Compaction: reclaiming space held by versions nobody can read any more.

Two things become garbage once a tree has been written to for a while:

* **Superseded versions.** Once a key has been rewritten, every older version is
  unreadable — the read path stops at the first version it meets, so elder ones
  are dead weight.
* **Tombstones.** A deletion marker exists only to mask elder versions. Once
  everything it masks has been merged away, the marker itself is dead weight.

Compaction is where that weight is actually removed, and it is the only place
where bytes leave the system.

Leveled policy
--------------
L0 files can overlap each other, because each is just a flushed memtable. Levels
1 and deeper are maintained overlap-free. Two triggers drive the planner:

* **L0 file count** — past ``level0_file_num_compaction_trigger``, all L0 files
  (plus any overlapping L1 files) merge into L1.
* **Level byte budget** — a level above its ``target_bytes_for_level`` budget
  contributes its oldest file, merged with whatever overlaps in the next level.

Tombstone retention
-------------------
A tombstone may only be dropped when the output lands in the deepest level,
because a shallower output could still be hiding something below it. Dropping a
marker that still has work to do would resurrect deleted data — one of the few
mistakes here that silently corrupts visible state rather than merely wasting
space.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sable.sstable import SSTableReader, SSTableWriter
from sable.types import EngineOptions, InternalEntry, SSTableMeta

__all__ = [
    "CompactionJob",
    "level_bytes",
    "plan_compaction",
    "run_compaction",
    "visible_versions",
]


@dataclass(frozen=True)
class CompactionJob:
    """One unit of compaction work: inputs in, outputs one level down.

    Attributes:
        level: Level the inputs are drawn from.
        target_level: Level the produced files belong to.
        inputs: Files to merge. For an L0 job this is every L0 file plus any
            overlapping L1 files; otherwise one file plus its overlaps below.
    """

    level: int
    target_level: int
    inputs: tuple[SSTableMeta, ...]

    @property
    def input_ids(self) -> frozenset[int]:
        return frozenset(meta.file_id for meta in self.inputs)

    @property
    def input_bytes(self) -> int:
        return sum(meta.file_size for meta in self.inputs)


def level_bytes(level: Sequence[SSTableMeta]) -> int:
    """Total bytes held by one level."""
    return sum(meta.file_size for meta in level)


def _overlaps(meta: SSTableMeta, others: Sequence[SSTableMeta]) -> bool:
    """True when ``meta``'s key span intersects any of ``others``."""
    for other in others:
        if meta.smallest_key <= other.largest_key and other.smallest_key <= meta.largest_key:
            return True
    return False


def plan_compaction(
    levels: Sequence[Sequence[SSTableMeta]], options: EngineOptions
) -> CompactionJob | None:
    """Choose the next compaction, or ``None`` when the tree is balanced.

    Args:
        levels: Per-level file lists, newest first within each level.
        options: Thresholds and level budget configuration.

    Returns:
        A :class:`CompactionJob`, or ``None`` if nothing needs doing.
    """
    if not levels:
        return None

    # L0 first: overlapping files there multiply point-read cost directly, so
    # this trigger is about latency rather than about space.
    if len(levels[0]) >= options.level0_file_num_compaction_trigger:
        l0 = tuple(levels[0])
        overlapping = tuple(meta for meta in levels[1] if _overlaps(meta, l0))
        return CompactionJob(level=0, target_level=1, inputs=l0 + overlapping)

    for level_index in range(1, len(levels) - 1):
        current = levels[level_index]
        if not current or level_bytes(current) <= options.target_bytes_for_level(level_index):
            continue
        oldest = min(current, key=lambda meta: meta.file_id)
        overlapping = tuple(
            meta for meta in levels[level_index + 1] if _overlaps(meta, [oldest])
        )
        return CompactionJob(
            level=level_index, target_level=level_index + 1, inputs=(oldest, *overlapping)
        )
    return None


def visible_versions(
    streams: Iterable[Iterator[InternalEntry]], *, drop_tombstones: bool
) -> Iterator[InternalEntry]:
    """Reduce ordered streams to the versions actually worth writing.

    Every input already orders by ``(user_key asc, sequence desc)``, so the merge
    delivers the newest version of a key first and elder ones are recognised by
    key identity alone — no sequence bookkeeping needed.

    Args:
        streams: Per-file iterators, each already in internal-key order.
        drop_tombstones: Whether deletion markers may be discarded outright.

    Yields:
        The surviving version of each key.
    """
    previous_key: bytes | None = None
    for entry in heapq.merge(*streams, key=lambda item: item.sort_key):
        if previous_key is not None and entry.user_key == previous_key:
            continue
        previous_key = entry.user_key
        if entry.is_tombstone and drop_tombstones:
            continue
        yield entry


def run_compaction(
    job: CompactionJob,
    home: Path,
    options: EngineOptions,
    allocate_file_id: Callable[[], int],
) -> list[SSTableMeta]:
    """Merge ``job.inputs`` into new tables one level down.

    Args:
        job: Work item from :func:`plan_compaction`.
        home: Database directory the inputs live in.
        options: Block geometry and tombstone-retention policy.
        allocate_file_id: Source of fresh file identifiers.

    Returns:
        Metadata for each produced table. Empty when every input entry was
        eliminated, which is legal and simply means the inputs vanish.
    """
    readers = [SSTableReader.open(home / meta.file_name) for meta in job.inputs]
    try:
        # A marker can only be discarded once nothing below it can be resurrected
        # by its absence, which is true only at the deepest level.
        drop_tombstones = job.target_level >= options.max_levels - 1
        writer: SSTableWriter | None = None

        for entry in visible_versions(
            [reader.iter_all() for reader in readers], drop_tombstones=drop_tombstones
        ):
            if writer is None:
                file_id = allocate_file_id()
                target = home / f"{file_id:06d}.sst"
                writer = SSTableWriter(
                    target, options, file_id=file_id, level=job.target_level
                )
            writer.add_entry(entry)

        if writer is None:
            return []
        return [writer.finish()]
    finally:
        for reader in readers:
            reader.close()
