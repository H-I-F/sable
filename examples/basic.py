"""A guided tour of the whole surface, runnable end to end.

    python examples/basic.py [home-directory]

Every step prints what it did and why it matters, so the output doubles as a
transcript of how the pieces interact: WAL -> memtable -> L0 -> compaction, with
snapshot isolation standing in for a concurrent reader.

The store is created fresh each run and left on disk afterwards, because the
point of a key-value store is that closing it does not lose anything. Pass a
throwaway path if you would rather not keep it.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sable import Engine, EngineOptions  # noqa: E402


def banner(text: str) -> None:
    """Print a section header."""
    print(f"\n== {text}")


def main(argv: list[str]) -> int:
    """Run the walkthrough.

    Args:
        argv: Process arguments; ``argv[0]`` is unused, ``argv[1]`` optionally
            supplies the database directory.

    Returns:
        Zero on success.
    """
    if len(argv) > 1:
        home = Path(argv[1]).resolve()
    else:
        home = Path(tempfile.mkdtemp(prefix="sable-basic-"))
    if len(argv) <= 1:
        print(f"home (temporary): {home}")
    if home.exists():
        shutil.rmtree(home)

    options = EngineOptions(
        block_size_bytes=512,
        memtable_bytes_threshold=256,
        level0_file_num_compaction_trigger=2,
        l1_bytes_trigger=64,
        max_levels=4,
    )
    engine = Engine.open(home, options)

    banner("write")
    for index in range(64):
        engine.put(f"user:{index:04d}".encode(), f"payload-{index}".encode())
    reported = engine.stats
    print(f"puts acknowledged: {reported.puts}")
    print(f"last sequence: {engine.last_sequence}")

    banner("read")
    print(f"user:0007 -> {engine.get(b'user:0007')!r}")
    print(f"user:9999 -> {engine.get(b'user:9999')!r}")

    banner("snapshot isolation")
    view = engine.snapshot()
    engine.delete(b"user:0007")
    print(f"latest view sees: {engine.get(b'user:0007')!r}")
    print(f"pinned view sees: {view.get(b'user:0007')!r}")
    view.release()

    banner("durability")
    engine.flush()
    print(f"files per level: {[len(level) for level in engine.level_files]}")
    print(f"flushes: {reported.flushes}")

    banner("compaction")
    rounds = 0
    while engine.compact() and rounds < 20:
        rounds += 1
    print(f"compaction rounds: {rounds}")
    print(f"files per level: {[len(level) for level in engine.level_files]}")
    print(f"bytes on disk: {sum(meta.file_size for level in engine.level_files for meta in level)}")
    engine.close()

    banner("recovery")
    reopened = Engine.open(home, options)
    print(f"reopened last sequence: {reopened.last_sequence}")
    print(f"reopened user:0063 -> {reopened.get(b'user:0063')!r}")
    print(f"visible keys: {sum(1 for _ in reopened.scan())}")
    reopened.close()

    if len(argv) <= 1:
        shutil.rmtree(home, ignore_errors=True)
        print(f"\nremoved temporary home {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
