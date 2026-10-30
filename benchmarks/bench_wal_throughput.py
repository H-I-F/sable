"""WAL throughput: what a durable write actually costs.

Every acknowledged ``put`` in this engine ends in an ``fsync``. That is the
entire durability story, and it is also the entire performance story: one
`fsync` per write puts a hard ceiling on throughput that has nothing to do with
the code between the calls.

This harness measures three modes so the ceiling and the ways around it are
visible in one table:

* **durable** — ``sync=True`` on every write. The number that matters.
* **deferred** — writes appended without syncing, one ``fsync`` every
  ``--group`` records. The group-commit shape, showing what batching buys and
  what window of loss it opens.
* **unsafe** — ``sync=False`` throughout. Not a durability guarantee; present so
  the cost of the other two is legible as a ratio.

Each mode reports latency percentiles as well as throughput, because a mean
would hide the thing that actually hurts: durable writes are not uniformly slow,
they are bimodal, with the writes that cross a memtable budget also paying for a
flush.

Run with::

    python -m benchmarks.bench_wal_throughput
    python -m benchmarks.bench_wal_throughput --iters 2000 --value-bytes 256 --markdown
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import shutil
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sable  # noqa: E402
from sable import Engine, EngineOptions  # noqa: E402
from sable.types import WriteOptions  # noqa: E402

DEFAULT_ITERATIONS = 1_000
DEFAULT_VALUE_BYTES = 64
DEFAULT_GROUP = 32

MODE_DURABLE = "put (durable, fsync each)"
MODE_DEFERRED = "put (grouped, one fsync per batch)"
MODE_UNSAFE = "put (fsync disabled)"


@contextmanager
def elapsed_clock() -> Iterator[Callable[[], float]]:
    """Yield a callable returning seconds since the block was entered."""
    start = time.perf_counter()
    yield lambda: time.perf_counter() - start


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of collected durations.

    Args:
        samples: Durations in seconds, unsorted and possibly empty.
        fraction: Position in ``(0, 1]``.

    Returns:
        Seconds, or zero when nothing was collected.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def throughput_options() -> EngineOptions:
    """A memtable budget large enough that no flush fires inside a measurement.

    A flush mid-run would charge one unlucky write for an SSTable publish and
    show up as a latency outlier that has nothing to do with the log. Flush cost
    is measured by ``bench_engine`` and is not what this script is for.
    """
    return EngineOptions(memtable_bytes_threshold=64 << 20, block_size_bytes=4096)


def make_keys(count: int, *, offset: int = 0) -> list[bytes]:
    """Fresh key space per mode, so no mode inherits another's tree."""
    return [f"k-{offset + index:08d}".encode() for index in range(count)]


def measure_writes(
    home: Path, keys: Sequence[bytes], value: bytes, *, mode: str, group: int
) -> dict[str, object]:
    """Time one write mode and return its row plus the raw samples.

    Args:
        home: Database directory, fresh per mode.
        keys: Key space to write.
        value: Payload for every write.
        mode: One of the three mode labels.
        group: Records per fsync in deferred mode.

    Returns:
        A row dict. ``samples`` carries the per-write durations so the caller can
        report the distribution rather than only its mean.
    """
    engine = Engine.open(home, throughput_options())
    samples: list[float] = []
    try:
        with elapsed_clock() as seconds:
            for index, key in enumerate(keys):
                if mode == MODE_DURABLE:
                    options = WriteOptions(sync=True)
                elif mode == MODE_UNSAFE:
                    options = WriteOptions(sync=False)
                else:
                    # Batch commit: the frame is appended every time, but the
                    # fsync is paid once per group. Everything since the last
                    # sync is what a crash would take.
                    options = WriteOptions(sync=(index % group == group - 1))
                began = time.perf_counter()
                engine.put(key, value, options)
                samples.append(time.perf_counter() - began)
        elapsed = seconds()
        wal_bytes = engine.stats.wal_bytes_written
    finally:
        engine.close()

    payload_bytes = (len(keys[0]) + len(value)) * len(keys) if keys else 0
    return {
        "workload": mode,
        "records": len(keys),
        "seconds": elapsed,
        "records_per_second": len(keys) / elapsed if elapsed else 0.0,
        "mb_per_second": (payload_bytes / (1 << 20)) / elapsed if elapsed else 0.0,
        "p50_ms": percentile(samples, 0.50) * 1_000,
        "p95_ms": percentile(samples, 0.95) * 1_000,
        "p99_ms": percentile(samples, 0.99) * 1_000,
        "wal_bytes": wal_bytes,
        "samples": samples,
    }


def render(rows: Sequence[dict[str, object]], *, markdown: bool) -> str:
    """Render the throughput table; Markdown is what a README embeds verbatim."""
    header = ("workload", "records", "seconds", "rec/sec", "p50 ms", "p95 ms", "p99 ms")
    lines = []
    if markdown:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
    for item in rows:
        cells = (
            str(item["workload"]),
            f"{int(item['records']):,}",
            f"{float(item['seconds']):.3f}",
            f"{float(item['records_per_second']):,.0f}",
            f"{float(item['p50_ms']):.3f}",
            f"{float(item['p95_ms']):.3f}",
            f"{float(item['p99_ms']):.3f}",
        )
        lines.append("| " + " | ".join(cells) + " |" if markdown else "  ".join(cells))
    return "\n".join(lines)


def environment() -> list[tuple[str, str]]:
    """Host facts, because an fsync number without a disk is meaningless."""
    return [
        ("Engine", f"sable {sable.__version__}"),
        ("Python", f"{platform.python_version()} ({sysconfig.get_platform()})"),
        ("OS", f"{platform.system()} {platform.release()}"),
        ("CPU count", str(os.cpu_count() or 1)),
        ("Durability", "fsync before put() returns, unless the mode says otherwise"),
    ]


def main(argv: list[str] | None = None) -> int:
    """Measure all three modes and print the report.

    Args:
        argv: Process arguments. ``--iters`` sets the record count per mode,
            ``--group`` the batch size for the deferred mode.

    Returns:
        Zero on success. The durable figure is the headline; the other two exist
        to give it a scale.
    """
    parser = argparse.ArgumentParser(prog="bench_wal_throughput", description=__doc__)
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--value-bytes", type=int, default=DEFAULT_VALUE_BYTES)
    parser.add_argument("--group", type=int, default=DEFAULT_GROUP)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--keep-home", type=Path, default=None)
    args = parser.parse_args(argv)

    scratch = Path(tempfile.mkdtemp(prefix="sable-wal-"))
    value = b"x" * args.value_bytes

    print("# sable WAL throughput")
    print()
    for label, fact in environment():
        print(f"- {label}: {fact}")
    print(f"- Records per mode: {args.iters:,}, value size {args.value_bytes} bytes")
    print()

    rows: list[dict[str, object]] = []
    modes = (
        (MODE_DURABLE, 0),
        (MODE_DEFERRED, args.iters),
        (MODE_UNSAFE, args.iters * 2),
    )
    for mode, offset in modes:
        home = (args.keep_home or scratch) / f"mode-{len(rows)}"
        keys = make_keys(args.iters, offset=offset)
        rows.append(measure_writes(home, keys, value, mode=mode, group=args.group))

    print(render(rows, markdown=args.markdown))
    print()

    durable = float(rows[0]["records_per_second"])
    grouped = float(rows[1]["records_per_second"])
    unsafe = float(rows[2]["records_per_second"])
    if durable:
        print(f"grouped / durable : {grouped / durable:.2f}x")
        print(f"unsafe / durable  : {unsafe / durable:.2f}x")
    print()
    print(
        f"durable ceiling: {durable:,.0f} records/sec, i.e. "
        f"{1_000 / durable:.2f} ms per acknowledged write"
        if durable
        else "no durable writes were measured"
    )

    # The point of the log is that a reopen finds the data. Recovering the
    # durable database is the check that the throughput figure was not obtained
    # by quietly skipping the fsync.
    durable_home = (args.keep_home or scratch) / "mode-0"
    reopened = Engine.open(durable_home, throughput_options())
    try:
        missing = [
            key for key in make_keys(args.iters) if reopened.get(key) != value
        ]
        assert not missing, f"recovery lost {len(missing)} acknowledged writes"
    finally:
        reopened.close()
    print("recovery: every acknowledged write replayed")

    if args.keep_home is None:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
