"""A physical snapshot of a store: memtables plus the leveled file set.

The engine's in-memory state and its on-disk files tell one story, but you have to
assemble it from three places to read it: the active memtable, the queue of
sealed-but-unflushed memtables, and every SSTable spread across L0..Ln. This
module gathers them into one immutable :class:`Topology` and renders it as a tree,
so an operator can answer "how much is in memory, how deep is the tree, and where
do these keys live?" without opening a debugger.

Two entry points, deliberately separated:

* :func:`build_topology` reads an engine that is already open, touching no disk
  and triggering no recovery.
* :func:`inspect_home` opens the directory itself. That replays the WAL, which is
  the only way to see a store's recovered memtable — and, on a torn tail, the
  only side effect this tool can have.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from sable.engine import Engine
from sable.types import EngineOptions, SSTableMeta

__all__ = [
    "FileInfo",
    "LevelInfo",
    "Topology",
    "build_topology",
    "inspect_home",
    "main",
    "render_topology",
]


@dataclass(slots=True)
class FileInfo:
    """One SSTable as it sits in the version set."""

    file_name: str
    file_id: int
    level: int
    file_size: int
    num_entries: int
    num_blocks: int
    smallest_key: bytes
    largest_key: bytes
    min_sequence: int
    max_sequence: int
    refs: int

    @classmethod
    def from_meta(cls, meta: SSTableMeta) -> FileInfo:
        """Project an :class:`SSTableMeta` into a plain inspection record."""
        return cls(
            file_name=meta.file_name,
            file_id=meta.file_id,
            level=meta.level,
            file_size=meta.file_size,
            num_entries=meta.num_entries,
            num_blocks=meta.num_blocks,
            smallest_key=meta.smallest_key,
            largest_key=meta.largest_key,
            min_sequence=meta.min_sequence,
            max_sequence=meta.max_sequence,
            refs=meta.refs,
        )


@dataclass(slots=True)
class LevelInfo:
    """Aggregate view of one level in the tree."""

    level: int
    files: list[FileInfo]
    total_bytes: int
    num_entries: int
    smallest_key: bytes
    largest_key: bytes


@dataclass(slots=True)
class Topology:
    """The complete physical picture of a store at one instant."""

    home: Path
    active_bytes: int
    active_versions: int
    active_smallest_key: bytes | None
    active_largest_key: bytes | None
    immutable_count: int
    immutable_bytes: int
    levels: list[LevelInfo]
    total_files: int
    total_bytes: int
    last_sequence: int
    live_snapshots: tuple[int, ...]


def build_topology(engine: Engine) -> Topology:
    """Capture the physical topology of an already-open ``engine``.

    Args:
        engine: An open engine. Nothing is written; only in-memory metadata and
            the cached file descriptors are read.

    Returns:
        A :class:`Topology`. The active and immutable memtable figures come from
        the engine's own accounting, so they reflect exactly what the writer sees.
    """
    active = engine._active
    immutable = engine._immutable

    levels: list[LevelInfo] = []
    for level_index, metas in enumerate(engine.level_files):
        files = [FileInfo.from_meta(meta) for meta in metas]
        keys = [meta.smallest_key for meta in metas if meta.smallest_key]
        tops = [meta.largest_key for meta in metas if meta.largest_key]
        levels.append(
            LevelInfo(
                level=level_index,
                files=files,
                total_bytes=sum(file.file_size for file in files),
                num_entries=sum(file.num_entries for file in files),
                smallest_key=min(keys) if keys else b"",
                largest_key=max(tops) if tops else b"",
            )
        )

    return Topology(
        home=engine.home,
        active_bytes=active.approximate_bytes,
        active_versions=active.version_count,
        active_smallest_key=active.smallest_key,
        active_largest_key=active.largest_key,
        immutable_count=len(immutable),
        immutable_bytes=sum(table.approximate_bytes for table in immutable),
        levels=levels,
        total_files=sum(len(level.files) for level in levels),
        total_bytes=sum(level.total_bytes for level in levels),
        last_sequence=engine.last_sequence,
        live_snapshots=engine.live_snapshots,
    )


def inspect_home(home: Path, engine_options: EngineOptions | None = None) -> Topology:
    """Open ``home``, capture its topology and close it again.

    Args:
        home: Database directory.
        engine_options: Optional :class:`sable.types.EngineOptions` used while
            opening, so callers can mirror the store's real configuration.

    Returns:
        The captured :class:`Topology`. Opening replays the WAL, so this is the
        entry point that reflects a store recovered from disk rather than one
        currently held in memory.
    """
    engine = Engine.open(Path(home), engine_options)
    try:
        return build_topology(engine)
    finally:
        engine.close()


def _human_bytes(count: int) -> str:
    """Render a byte count with a binary unit suffix."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{count} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{count} B"  # pragma: no cover - loop always returns


def _short_key(key: bytes, limit: int = 20) -> str:
    """Render a key compactly for the tree."""
    if not key:
        return "-"
    if len(key) <= limit:
        return repr(key)
    return repr(key[: limit // 2])[:-1] + "..." + repr(key[-(limit // 2) :])[1:]


def render_topology(topology: Topology) -> str:
    """Render a :class:`Topology` as a plain-text tree.

    Args:
        topology: Result of :func:`build_topology` or :func:`inspect_home`.

    Returns:
        A multi-line string. Levels are listed in order with their files nested
        beneath, followed by a totals line.
    """
    lines: list[str] = [f"store: {topology.home}"]
    lines.append(
        f"├── active MemTable    {_human_bytes(topology.active_bytes)}   "
        f"({topology.active_versions} versions)   "
        f"keys=[{_short_key(topology.active_smallest_key or b'')} .. "
        f"{_short_key(topology.active_largest_key or b'')}]"
    )
    lines.append(
        f"├── immutable tables   {topology.immutable_count} table(s)   "
        f"({_human_bytes(topology.immutable_bytes)})"
    )

    for position, level in enumerate(topology.levels):
        last_level = position == len(topology.levels) - 1
        connector = "└──" if last_level else "├──"
        lines.append(
            f"{connector} L{level.level}   {len(level.files)} file(s)   "
            f"{_human_bytes(level.total_bytes)}   {level.num_entries} entries   "
            f"keys=[{_short_key(level.smallest_key)} .. {_short_key(level.largest_key)}]"
        )
        child_prefix = "    " if last_level else "│   "
        for file_position, file in enumerate(level.files):
            last_file = file_position == len(level.files) - 1
            branch = "└──" if last_file else "├──"
            lines.append(
                f"{child_prefix}{branch} {file.file_name}   {_human_bytes(file.file_size)}   "
                f"{file.num_entries} entries   seq=[{file.min_sequence}..{file.max_sequence}]"
            )

    lines.append(
        f"└── total   {topology.total_files} file(s)   {_human_bytes(topology.total_bytes)}   "
        f"last_sequence={topology.last_sequence}   "
        f"snapshots={list(topology.live_snapshots)}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print the topology tree for a store directory.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` on success.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sable.topology",
        description="Print the physical topology (memtables + leveled SSTables) of a store.",
    )
    parser.add_argument("path", type=Path, help="path to a sable data directory")
    args = parser.parse_args(argv)
    print(render_topology(inspect_home(args.path)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
