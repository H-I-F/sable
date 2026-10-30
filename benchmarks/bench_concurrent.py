"""Concurrent read stress: point lookups and range scans against a compacting tree.

Two questions this harness exists to answer, because neither is answered by a
single-threaded benchmark:

1. **Does a concurrent compaction break a reader?** Reference-counted file leases
   are supposed to keep a retired SSTable alive until the last walk releases it.
   If that mechanism is wrong, the failure is a ``FileNotFoundError`` raised
   somewhere inside ``get`` or ``scan`` — nondeterministically, at a rate that
   scales with reader count. So every worker records exceptions rather than
   letting them escape, and the run asserts that none occurred.
2. **What does that safety cost?** The same workload is measured twice, with and
   without a compactor running, and the latency percentiles are reported side by
   side. Deferred reclamation should cost throughput, never correctness.

The workload is deliberately shaped so compaction has real work: several
overlapping L0 files are built up front, and a compactor thread churns a slice of
the key space to keep producing new ones. A run in which the compactor found
nothing to do would report a comfortable number and prove nothing, so the number
of completed jobs is asserted to be non-zero.

Run with::

    python -m benchmarks.bench_concurrent
    python -m benchmarks.bench_concurrent --readers 8 --window 5 --markdown
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
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sable  # noqa: E402
from sable import Engine, EngineOptions  # noqa: E402
from sable.types import WriteOptions  # noqa: E402

DEFAULT_KEYS = 4_000
DEFAULT_VALUE_BYTES = 64
DEFAULT_READERS = 4
DEFAULT_SCANNERS = 2
DEFAULT_WINDOW_SECONDS = 2.0
DEFAULT_L0_FILES = 6
DEFAULT_SCAN_SPAN = 250
DEFAULT_CHURN_KEYS = 256

# A barrier that outlives its timeout is a hang, which is worse than a failure.
BARRIER_TIMEOUT_SECONDS = 30.0
JOIN_TIMEOUT_SECONDS = 30.0


@dataclass
class WorkerResult:
    """What one worker thread produced.

    Attributes:
        samples: Per-operation durations in seconds.
        errors: Exception reprs captured inside the worker. Non-empty means the
            concurrency mechanism failed; the run asserts on this.
        ops: Operations completed.
    """

    samples: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ops: int = 0


@contextmanager
def elapsed_clock() -> Iterator[Callable[[], float]]:
    """Yield a callable returning seconds since the block was entered."""
    start = time.perf_counter()
    yield lambda: time.perf_counter() - start


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of already-collected durations.

    Args:
        samples: Durations in seconds, unsorted and possibly empty.
        fraction: Position in ``(0, 1]``.

    Returns:
        Seconds. Zero for an empty sample set, so a phase that did no work
        renders as a zero rather than raising.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def stress_options() -> EngineOptions:
    """Configuration that keeps flushes explicit and compaction busy.

    The memtable budget is large so flushes happen only where this script asks
    for them — a background flush triggered by a write would fire inside the
    measured window and turn a latency sample into a file publish. The L0
    trigger is low so the compactor always has candidates.
    """
    return EngineOptions(
        memtable_bytes_threshold=8 << 20,
        block_size_bytes=4096,
        level0_file_num_compaction_trigger=4,
        l1_bytes_trigger=1 << 20,
    )


def make_keys(count: int) -> list[bytes]:
    """Deterministic, ordered key set so both phases probe the same tree."""
    return [f"k-{index:08d}".encode() for index in range(count)]


def build_database(home: Path, keys: Sequence[bytes], value: bytes, files: int) -> Engine:
    """Publish ``files`` overlapping L0 tables over the same key space.

    Every round rewrites every key, so each file shadows the one before it.
    That is the shape compaction exists to collapse, and it is also the worst
    case for a reader: the newest version may live in any of several files.

    Args:
        home: Database directory.
        keys: Key space to write.
        value: Payload written for every key in every round.
        files: Number of L0 tables to leave behind.

    Returns:
        The open engine, positioned for the measured phases.
    """
    engine = Engine.open(home, stress_options())
    for _round in range(files):
        for key in keys:
            engine.put(key, value, WriteOptions(sync=False))
        engine.flush()
    return engine


def l0_file_count(engine: Engine) -> int:
    """Files at level 0, the ones a read must consult first."""
    return len(engine.level_files[0])


def reader_worker(
    engine: Engine,
    keys: Sequence[bytes],
    expected: bytes,
    *,
    gate: threading.Barrier,
    stop: threading.Event,
    result: WorkerResult,
) -> None:
    """Point-lookup loop: one ``get`` per sample, value verified every time.

    Args:
        engine: Shared handle; the engine is thread-safe by construction.
        keys: Probe set, walked in a fixed rotation.
        expected: Value every key must resolve to.
        gate: Start barrier shared by every worker in the phase.
        stop: Set by the coordinator when the window closes.
        result: Accumulator owned by this thread, merged after join.
    """
    gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)
    position = 0
    total = len(keys)
    while not stop.is_set():
        key = keys[position % total]
        position += 1
        started = time.perf_counter()
        try:
            observed = engine.get(key)
        except Exception as error:  # noqa: BLE001 - the point is to record it
            result.errors.append(f"get({key!r}): {error!r}")
            continue
        result.samples.append(time.perf_counter() - started)
        if observed != expected:
            result.errors.append(f"get({key!r}) returned {observed!r}")
            continue
        result.ops += 1


def scanner_worker(
    engine: Engine,
    keys: Sequence[bytes],
    expected: bytes,
    *,
    span: int,
    gate: threading.Barrier,
    stop: threading.Event,
    result: WorkerResult,
) -> None:
    """Range-scan loop: one bounded walk per sample, every pair verified.

    Args:
        engine: Shared handle.
        keys: Ordered key space the windows are cut from.
        expected: Value every visible pair must carry.
        span: Keys per window.
        gate: Start barrier shared by every worker in the phase.
        stop: Set by the coordinator when the window closes.
        result: Accumulator owned by this thread.
    """
    gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)
    total = len(keys)
    position = 0
    while not stop.is_set():
        start = keys[position % total]
        end = keys[min(position + span, total - 1)]
        position += span
        started = time.perf_counter()
        try:
            pairs = list(engine.scan(start, end, limit=span))
        except Exception as error:  # noqa: BLE001 - the point is to record it
            result.errors.append(f"scan({start!r}, {end!r}): {error!r}")
            continue
        result.samples.append(time.perf_counter() - started)
        if any(value != expected for _key, value in pairs):
            result.errors.append(f"scan({start!r}, {end!r}) returned a wrong value")
            continue
        result.ops += 1


def compactor_worker(
    engine: Engine,
    keys: Sequence[bytes],
    value: bytes,
    *,
    churn: int,
    gate: threading.Barrier,
    stop: threading.Event,
    result: WorkerResult,
) -> None:
    """Merge continuously, churning a key slice when the tree is balanced.

    Rewriting the same value means the data invariant the readers check never
    changes, so churn creates compaction work without creating a race the
    readers would legitimately lose.

    Args:
        engine: Shared handle.
        keys: Key space; the first ``churn`` entries are rewritten.
        value: Value rewritten, identical to what is already there.
        churn: Number of keys rewritten per idle pass.
        gate: Start barrier shared by every worker in the phase.
        stop: Set by the coordinator when the window closes.
        result: Accumulator whose ``ops`` counts completed jobs.
    """
    gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)
    slice_keys = list(keys[:churn])
    while not stop.is_set():
        try:
            merged = engine.compact()
        except Exception as error:  # noqa: BLE001 - the point is to record it
            result.errors.append(f"compact(): {error!r}")
            return
        if merged:
            result.ops += 1
            continue
        for key in slice_keys:
            try:
                engine.put(key, value, WriteOptions(sync=False))
            except Exception as error:  # noqa: BLE001 - the point is to record it
                result.errors.append(f"churn put({key!r}): {error!r}")
                return
        try:
            engine.flush()
        except Exception as error:  # noqa: BLE001 - the point is to record it
            result.errors.append(f"churn flush(): {error!r}")
            return


def solo_scan_reference(
    engine: Engine, keys: Sequence[bytes], *, span: int, rounds: int
) -> tuple[list[float], float]:
    """Measure scans with no competitors, as the reference the rest is read against.

    A contended scan number is meaningless on its own: every block is fetched
    with one positional read, and each read releases the GIL, so a scan running
    beside CPU-bound readers spends most of its life waiting to get the
    interpreter back. This row is what the engine actually costs.

    Args:
        engine: Populated handle.
        keys: Key space the windows are cut from.
        span: Keys per window.
        rounds: Windows to walk.

    Returns:
        ``(samples, seconds)`` — durations and total elapsed time.
    """
    samples: list[float] = []
    total = len(keys)
    with elapsed_clock() as seconds:
        for index in range(rounds):
            start = keys[(index * span) % total]
            end = keys[min((index * span) + span, total - 1)]
            began = time.perf_counter()
            list(engine.scan(start, end, limit=span))
            samples.append(time.perf_counter() - began)
    return samples, seconds()


def run_phase(
    engine: Engine,
    keys: Sequence[bytes],
    value: bytes,
    *,
    readers: int,
    scanners: int,
    span: int,
    churn: int,
    window: float,
    with_compaction: bool,
) -> dict[str, object]:
    """Run one measured phase and return its aggregate.

    Args:
        engine: Shared handle, already populated.
        keys: Key space.
        value: Expected payload.
        readers: Point-lookup threads.
        scanners: Range-scan threads.
        span: Keys per scan window.
        churn: Keys rewritten per idle compaction pass.
        window: Seconds the phase runs for.
        with_compaction: Whether a compactor thread participates.

    Returns:
        A dict of per-workload samples, error lists, compaction count and wall
        time. Nothing here raises: failures are reported as data so the caller
        can assert on them with a useful message.
    """
    party = readers + scanners + (1 if with_compaction else 0)
    gate = threading.Barrier(party)
    stop = threading.Event()

    point_results = [WorkerResult() for _ in range(readers)]
    scan_results = [WorkerResult() for _ in range(scanners)]
    compact_result = WorkerResult()

    threads = [
        threading.Thread(
            target=reader_worker,
            args=(engine, keys, value),
            kwargs={"gate": gate, "stop": stop, "result": point_results[index]},
            name=f"reader-{index}",
        )
        for index in range(readers)
    ]
    threads.extend(
        threading.Thread(
            target=scanner_worker,
            args=(engine, keys, value),
            kwargs={
                "span": span,
                "gate": gate,
                "stop": stop,
                "result": scan_results[index],
            },
            name=f"scanner-{index}",
        )
        for index in range(scanners)
    )
    if with_compaction:
        threads.append(
            threading.Thread(
                target=compactor_worker,
                args=(engine, keys, value),
                kwargs={"churn": churn, "gate": gate, "stop": stop, "result": compact_result},
                name="compactor",
            )
        )

    files_before = sum(len(level) for level in engine.level_files)
    for thread in threads:
        thread.start()
    with elapsed_clock() as seconds:
        time.sleep(window)
        stop.set()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT_SECONDS)
    elapsed = seconds()
    stalled = [thread.name for thread in threads if thread.is_alive()]

    def merge(results: Sequence[WorkerResult]) -> tuple[list[float], list[str], int]:
        samples: list[float] = []
        errors: list[str] = []
        ops = 0
        for item in results:
            samples.extend(item.samples)
            errors.extend(item.errors)
            ops += item.ops
        return samples, errors, ops

    point_samples, point_errors, point_ops = merge(point_results)
    scan_samples, scan_errors, scan_ops = merge(scan_results)
    return {
        "point_samples": point_samples,
        "scan_samples": scan_samples,
        "point_ops": point_ops,
        "scan_ops": scan_ops,
        "errors": [*point_errors, *scan_errors, *compact_result.errors],
        "compactions": compact_result.ops,
        "seconds": elapsed,
        "files_before": files_before,
        "files_after": sum(len(level) for level in engine.level_files),
        "stalled": stalled,
    }


def summarise(workload: str, samples: Sequence[float], seconds: float) -> dict[str, object]:
    """Turn durations into the row the report prints.

    Args:
        workload: Row label.
        samples: Durations in seconds.
        seconds: Wall time of the phase.

    Returns:
        Percentiles in milliseconds plus throughput.
    """
    count = len(samples)
    return {
        "workload": workload,
        "ops": count,
        "p50_ms": percentile(samples, 0.50) * 1_000,
        "p95_ms": percentile(samples, 0.95) * 1_000,
        "p99_ms": percentile(samples, 0.99) * 1_000,
        "ops_per_second": count / seconds if seconds else 0.0,
    }


def render(rows: Sequence[dict[str, object]], *, markdown: bool) -> str:
    """Render the latency table; Markdown is what a README embeds verbatim."""
    header = ("workload", "ops", "p50 ms", "p95 ms", "p99 ms", "ops/sec")
    lines = []
    if markdown:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
    for item in rows:
        cells = (
            str(item["workload"]),
            f"{int(item['ops']):,}",
            f"{float(item['p50_ms']):.3f}",
            f"{float(item['p95_ms']):.3f}",
            f"{float(item['p99_ms']):.3f}",
            f"{float(item['ops_per_second']):,.0f}",
        )
        lines.append("| " + " | ".join(cells) + " |" if markdown else "  ".join(cells))
    return "\n".join(lines)


def environment(switch_interval: float | None) -> list[tuple[str, str]]:
    """Host facts needed before any latency figure means anything."""
    return [
        ("Engine", f"sable {sable.__version__}"),
        ("Python", f"{platform.python_version()} ({sysconfig.get_platform()})"),
        ("OS", f"{platform.system()} {platform.release()}"),
        ("CPU count", str(os.cpu_count() or 1)),
        ("GIL", "present: threads overlap on I/O, not on bytecode"),
        (
            "GIL switch interval",
            f"{switch_interval * 1_000:.2f} ms"
            if switch_interval is not None
            else f"{sys.getswitchinterval() * 1_000:.2f} ms (interpreter default)",
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    """Build a contested tree, measure both phases and print the report.

    Args:
        argv: Process arguments. ``--readers``/``--scanners`` set the thread
            shape, ``--window`` the duration of each phase.

    Returns:
        Zero on success. Non-zero is reserved for a genuine assertion failure,
        which is the only outcome here that matters — this script grades itself.
    """
    parser = argparse.ArgumentParser(prog="bench_concurrent", description=__doc__)
    parser.add_argument("--keys", type=int, default=DEFAULT_KEYS)
    parser.add_argument("--value-bytes", type=int, default=DEFAULT_VALUE_BYTES)
    parser.add_argument("--readers", type=int, default=DEFAULT_READERS)
    parser.add_argument("--scanners", type=int, default=DEFAULT_SCANNERS)
    parser.add_argument("--span", type=int, default=DEFAULT_SCAN_SPAN)
    parser.add_argument("--churn", type=int, default=DEFAULT_CHURN_KEYS)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--l0-files", type=int, default=DEFAULT_L0_FILES)
    parser.add_argument(
        "--switch-interval",
        type=float,
        default=None,
        help="override sys.setswitchinterval; shortens the GIL slice",
    )
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--keep-home", type=Path, default=None)
    args = parser.parse_args(argv)

    previous_interval = sys.getswitchinterval()
    if args.switch_interval is not None:
        sys.setswitchinterval(args.switch_interval)

    scratch = Path(tempfile.mkdtemp(prefix="sable-concurrent-"))
    home = args.keep_home or scratch / "data"
    keys = make_keys(args.keys)
    value = b"x" * args.value_bytes

    print("# sable concurrent read stress")
    print()
    for label, fact in environment(args.switch_interval):
        print(f"- {label}: {fact}")
    print(
        f"- Shape: {args.readers} readers, {args.scanners} scanners, "
        f"{args.window:.1f}s per phase, {args.keys:,} keys"
    )
    print()

    engine = build_database(home, keys, value, args.l0_files)
    print(f"prepared: {l0_file_count(engine)} L0 files, engine sequence {engine.last_sequence}")
    solo_samples, solo_seconds = solo_scan_reference(engine, keys, span=args.span, rounds=50)

    try:
        quiet = run_phase(
            engine,
            keys,
            value,
            readers=args.readers,
            scanners=args.scanners,
            span=args.span,
            churn=args.churn,
            window=args.window,
            with_compaction=False,
        )
        contended = run_phase(
            engine,
            keys,
            value,
            readers=args.readers,
            scanners=args.scanners,
            span=args.span,
            churn=args.churn,
            window=args.window,
            with_compaction=True,
        )

        rows = [
            summarise("get (no compaction)", quiet["point_samples"], quiet["seconds"]),
            summarise("get (vs compaction)", contended["point_samples"], contended["seconds"]),
            summarise("scan (single thread)", solo_samples, solo_seconds),
            summarise("scan (no compaction)", quiet["scan_samples"], quiet["seconds"]),
            summarise("scan (vs compaction)", contended["scan_samples"], contended["seconds"]),
        ]
        print()
        print(render(rows, markdown=args.markdown))
        print()
        print(
            f"compaction jobs completed during the contended phase: "
            f"{int(contended['compactions'])}"
        )
        print(
            f"tree files: {int(contended['files_before'])} before -> "
            f"{int(contended['files_after'])} after"
        )
        print()

        errors = [*quiet["errors"], *contended["errors"]]
        stalled = [*quiet["stalled"], *contended["stalled"]]
        assert not stalled, f"worker threads did not finish: {stalled}"
        assert not errors, f"a concurrent read failed: {errors[:5]}"
        assert int(contended["compactions"]) > 0, (
            "the compactor found no work; the contended phase proved nothing"
        )

        # Reopening is the real integrity check: it forces recovery over whatever
        # the compactor left behind, with no open reader keeping files alive.
        engine.close()
        reopened = Engine.open(home, stress_options())
        try:
            for key in keys[:: max(1, len(keys) // 200)]:
                assert reopened.get(key) == value, f"post-compaction read lost {key!r}"
        finally:
            reopened.close()

        quiet_hits = float(rows[0]["ops_per_second"])
        contended_hits = float(rows[1]["ops_per_second"])
        if quiet_hits:
            retained = contended_hits / quiet_hits
            print(f"point-read throughput retained under compaction: {retained:.2f}x")
        print(
            "reading the scan rows: one block per positional read, and every read "
            "releases\nthe GIL, so a scan beside CPU-bound readers spends most of "
            "its life waiting to\nget the interpreter back. Compare against the "
            "single-thread row, or re-run with\n--switch-interval 0.0005 to "
            "separate engine cost from GIL handoff."
        )
        print("verdict: no read error, no lost key, recovery clean")
    finally:
        sys.setswitchinterval(previous_interval)
        if args.keep_home is None:
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
