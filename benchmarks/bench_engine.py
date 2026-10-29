"""Measured cost of every engine primitive, printed as a Markdown table.

    python -m benchmarks.bench_engine [--iters N] [--value-bytes N] [--markdown]

Two decisions keep these numbers informative rather than decorative:

* **Every write is durable by default.** The write workload fsyncs per operation,
  so what it reports includes the cost of surviving a crash — which is the whole
  point of the write path. The final row repeats it with fsync disabled, and the
  ratio between the two is simply what stable storage costs on this machine.
* **The miss workload measures the Bloom filter.** Without it, an absent key costs
  a sparse-index search plus a block read; with it the answer comes from a bit
  vector and nothing touches the data blocks. The gap between the hit and miss
  rows is that saving, made visible.

Nothing here is tuned for a headline figure. Read the ratios between rows, not the
absolute throughput.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sable  # noqa: E402
from sable import Engine, EngineOptions  # noqa: E402
from sable.types import WriteOptions  # noqa: E402

DEFAULT_ITERATIONS = 5_000
DEFAULT_VALUE_BYTES = 64
DEFAULT_SCAN_ROUNDS = 20
DEFAULT_COMPACTION_ROUNDS = 5
RANDOM_SEED = 20261030


@contextmanager
def elapsed_clock() -> Iterator[Callable[[], float]]:
    """Yield a callable returning seconds since the block was entered."""
    start = time.perf_counter()
    yield lambda: time.perf_counter() - start


def benchmark_options() -> EngineOptions:
    """Shared configuration.

    The memtable budget is deliberately large so the write workload is not also
    measuring flushes that happen to fire mid-run; flush and merge costs are
    measured separately by their own rows.
    """
    return EngineOptions(
        memtable_bytes_threshold=8 << 20,
        block_size_bytes=4096,
        level0_file_num_compaction_trigger=4,
    )


def make_keys(count: int) -> list[bytes]:
    """Deterministic, lexicographically ordered key set shared by every workload."""
    return [f"k-{index:08d}".encode() for index in range(count)]


def bench_puts(home: Path, keys: list[bytes], value: bytes, *, sync: bool) -> dict[str, object]:
    """Sequential writes, one fsync each unless ``sync`` is disabled."""
    engine = Engine.open(home, benchmark_options())
    try:
        with elapsed_clock() as seconds:
            for key in keys:
                engine.put(key, value, WriteOptions(sync=sync))
        duration = seconds()
    finally:
        engine.close()
    label = "put (durable, fsync each)" if sync else "put (fsync disabled)"
    return row(label, len(keys), duration, len(value) * len(keys))


def publish_sstables(home: Path) -> dict[str, object]:
    """Freeze the memtable into level 0 so later reads exercise the SSTable path.

    Point reads against an un-flushed memtable would measure the skip list, not
    the storage engine; the whole point of the read rows is what happens *after*
    the data has become files with index blocks and Bloom filters.
    """
    engine = Engine.open(home, benchmark_options())
    try:
        entries = sum(1 for _ in engine.scan())
        with elapsed_clock() as seconds:
            engine.flush()
        duration = seconds()
    finally:
        engine.close()
    return row("flush -> L0", entries, duration, 0)


def bench_point_reads(home: Path, keys: list[bytes], *, hits: bool) -> dict[str, object]:
    """Random point hits, or random probes for keys that were never written."""
    engine = Engine.open(home, benchmark_options())
    try:
        probes = list(keys)
        # Shuffled only so the probe order is not the insertion order; the seed is
        # fixed so two runs probe the same sequence.
        random.Random(RANDOM_SEED).shuffle(probes)  # noqa: S311 - ordering, not security
        if not hits:
            probes = [key + b"-absent" for key in probes]
        resolved = 0
        with elapsed_clock() as seconds:
            for key in probes:
                if engine.get(key) is not None:
                    resolved += 1
        duration = seconds()
    finally:
        engine.close()
    return row("get hit" if hits else "get miss", len(probes), duration, 0, resolved=resolved)


def bench_scan(home: Path, rounds: int) -> dict[str, object]:
    """Whole-range walks, which stress the multi-level merge rather than seeks."""
    engine = Engine.open(home, benchmark_options())
    try:
        with elapsed_clock() as seconds:
            for _ in range(rounds):
                sum(1 for _ in engine.scan())
        duration = seconds()
    finally:
        engine.close()
    return row("full scan", rounds, duration, 0)


def bench_compaction(home: Path, keys: list[bytes], value: bytes, rounds: int) -> dict[str, object]:
    """Overwrite everything ``rounds`` times, then measure reclaiming the waste.

    Writing every key again and flushing produces overlapping L0 files holding
    superseded versions - exactly the mess compaction exists to clean up - so the
    merge measured afterwards has real work rather than nothing to do.
    """
    engine = Engine.open(home, benchmark_options())
    try:
        for _ in range(rounds):
            for key in keys:
                engine.put(key, value, WriteOptions(sync=False))
            engine.flush()
        inputs = sum(len(level) for level in engine.level_files)
        jobs = 0
        with elapsed_clock() as seconds:
            while engine.compact() and jobs < 12:
                jobs += 1
        duration = seconds()
    finally:
        engine.close()
    return row("compaction (jobs)", max(jobs, 1), duration, 0, note=f"{inputs} L0 files in")


def bench_reopen(home: Path, openings: int = 10) -> dict[str, object]:
    """Recovery cost: WAL replay plus manifest verification, repeated."""
    with elapsed_clock() as seconds:
        for _ in range(openings):
            Engine.open(home, benchmark_options()).close()
    return row("reopen + replay", openings, seconds(), 0)


def _megabytes_per_second(payload_bytes: int, seconds: float) -> float:
    """Throughput in MiB/s, or 0 when the workload carried no payload."""
    if not seconds or not payload_bytes:
        return 0.0
    return (payload_bytes / (1 << 20)) / seconds


def row(
    name: str,
    iterations: int,
    seconds: float,
    payload_bytes: int,
    *,
    resolved: int = 0,
    note: str = "",
) -> dict[str, object]:
    """Normalise one measurement into the shape the renderer expects."""
    return {
        "workload": name,
        "iterations": iterations,
        "seconds": seconds,
        "ops_per_second": iterations / seconds if seconds else 0.0,
        "mb_per_second": _megabytes_per_second(payload_bytes, seconds),
        "resolved": resolved,
        "note": note,
    }


def environment() -> list[tuple[str, str]]:
    """Host facts a reader needs before any throughput figure means anything."""
    return [
        ("Engine", f"sable {sable.__version__}"),
        ("Python", f"{platform.python_version()} ({sysconfig.get_platform()})"),
        ("OS", f"{platform.system()} {platform.release()}"),
        ("CPU count", str(os.cpu_count() or 1)),
        ("Durability", "fsync per acknowledged write"),
    ]


def render(rows: list[dict[str, object]], *, markdown: bool) -> str:
    """Render rows as a table; Markdown is what the READMEs embed verbatim."""
    header = ("workload", "iterations", "seconds", "ops/sec", "MiB/s")
    lines = []
    if markdown:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
    for item in rows:
        cells = (
            str(item["workload"]),
            f"{int(item['iterations']):,}",
            f"{float(item['seconds']):.3f}",
            f"{float(item['ops_per_second']):,.0f}",
            f"{float(item['mb_per_second']):.2f}",
        )
        lines.append("| " + " | ".join(cells) + " |" if markdown else "  ".join(cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run every workload and print the report.

    Args:
        argv: Process arguments. ``--iters`` scales the write/read workloads,
            ``--markdown`` emits a paste-ready table.

    Returns:
        Zero: this script reports measurements, it does not grade them.
    """
    parser = argparse.ArgumentParser(prog="bench_engine", description=__doc__)
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--value-bytes", type=int, default=DEFAULT_VALUE_BYTES)
    parser.add_argument("--scan-rounds", type=int, default=DEFAULT_SCAN_ROUNDS)
    parser.add_argument("--compaction-rounds", type=int, default=DEFAULT_COMPACTION_ROUNDS)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--keep-home", type=Path, default=None)
    args = parser.parse_args(argv)

    scratch = Path(tempfile.mkdtemp(prefix="sable-bench-"))
    home = args.keep_home or scratch / "data"
    keys = make_keys(args.iters)
    value = b"x" * args.value_bytes

    print("# sable benchmark")
    print()
    for label, fact in environment():
        print(f"- {label}: {fact}")
    print()

    rows = [
        bench_puts(home, keys, value, sync=True),
        publish_sstables(home),
        bench_point_reads(home, keys, hits=True),
        bench_point_reads(home, keys, hits=False),
        bench_scan(home, args.scan_rounds),
        bench_compaction(home, keys, value, args.compaction_rounds),
        bench_reopen(home),
        bench_puts(home, keys, value, sync=False),
    ]
    print(render(rows, markdown=args.markdown))
    print()

    hits = float(rows[2]["ops_per_second"])
    misses = float(rows[3]["ops_per_second"])
    durable = float(rows[0]["ops_per_second"])
    unsafe = float(rows[7]["ops_per_second"])
    assert int(rows[2]["resolved"]) == args.iters, "the hit probe missed data"
    assert int(rows[3]["resolved"]) == 0, "the miss probe found data"
    print(f"get miss / get hit      : {misses / hits:.2f}x (Bloom pruning vs. a block read)")
    print(f"fsync disabled / durable: {unsafe / durable:.2f}x (the price of surviving a crash)")

    if args.keep_home is None:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
