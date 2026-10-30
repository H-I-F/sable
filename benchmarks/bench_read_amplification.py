"""Read amplification, measured: physical block reads per logical point read.

    python -m benchmarks.bench_read_amplification [--keys N] [--probes N]
                                                  [--batches N] [--markdown]

Read amplification is the ratio that explains why an LSM tree can feel slow while
the disk sits idle: one logical ``get`` may consult several files, and every file
that cannot be skipped costs a data block fetch. This script isolates the two
variables that decide that cost.

* **Layout.** ``overlapping`` leaves every flush in level 0, so all files span the
  whole key range and a probe has to consider all of them. ``levelled`` runs
  compaction first, so each file owns a disjoint slice and a probe considers one.
* **Bloom filter.** With a filter, a file that cannot hold the key answers from a
  bit vector and never touches a data block. With ``bloom_false_positive_rate=0.0``
  no filter is written at all, and the same probe pays for the block.

Both shapes hold identical keys and values; only their *arrangement* differs,
which is the variable under test. Every probe is therefore charged against the
same logical work, and the difference in physical work is the amplification.

What is counted, and what is not:

* Counted: every data block fetch, taken from each ``SSTableReader``'s own
  ``data_block_reads`` counter, and every probe the filter rejected. Physical
  bytes are ``block reads x block size``: the exact size of each fetch is not
  tracked per read, and charging the configured block size never understates it.
* Not counted: the index region, read once per file per engine instance and then
  cached, and WAL reads. Both amortise away over a read-mostly workload, which is
  the regime this number describes. A warm-up pass runs before every measurement
  so neither lands inside the window.

The counters are per reader, and the engine does not aggregate them, so the script
sums the readers it opened itself. That is also why each cell builds a fresh store
and opens a fresh engine: a counter that survives a compaction would be comparing
different file populations.
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
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sable  # noqa: E402
from sable import Engine, EngineOptions  # noqa: E402
from sable.types import WriteOptions  # noqa: E402

DEFAULT_KEYS = 4_000
DEFAULT_VALUE_BYTES = 96
DEFAULT_PROBES = 2_000
DEFAULT_BATCHES = 6
BLOCK_BYTES = 4096
BLOOM_RATE = 0.01
RANDOM_SEED = 20261030
LAYOUTS = ("overlapping", "levelled")


def engine_options(*, bloom_rate: float, compacting: bool) -> EngineOptions:
    """Geometry shared by both shapes; only the filter and trigger differ.

    ``overlapping`` raises the L0 trigger out of reach so no opportunistic
    compaction can quietly merge the layout away mid-write. ``levelled`` sets it
    to two, which is the switch that makes every flush get merged as soon as a
    pair exists. Neither value touches the read path, so the two cells remain a
    fair comparison of arrangement alone.

    The memtable budget is far above the dataset size, so a flush only happens
    where the script asks for one and produces exactly one file each time.
    """
    trigger = 1 << 16 if not compacting else 2
    return EngineOptions(
        memtable_bytes_threshold=8 << 20,
        block_size_bytes=BLOCK_BYTES,
        level0_file_num_compaction_trigger=trigger,
        level0_slowdown_trigger=1 << 16,
        l1_bytes_trigger=64 << 20,
        bloom_false_positive_rate=bloom_rate,
    )


@dataclass(slots=True)
class Counters:
    """Per-reader totals at one instant, summed across every open table."""

    block_reads: int
    bloom_rejections: int
    readers: int


@dataclass(slots=True)
class Sample:
    """One cell of the matrix: a layout, a filter setting and a probe kind."""

    layout: str
    bloom: bool
    probe: str
    probes: int
    resolved: int
    files_in_span: int
    block_reads: int
    bloom_rejections: int

    @property
    def reads_per_probe(self) -> float:
        """Data blocks fetched per logical read: the amplification itself."""
        return self.block_reads / self.probes if self.probes else 0.0

    @property
    def files_per_probe(self) -> float:
        """Tables whose key span admitted the probe, before any filter ran."""
        return self.files_in_span / self.probes if self.probes else 0.0

    @property
    def rejection_rate(self) -> float:
        """Share of the filter's answers that avoided a block fetch."""
        return self.bloom_rejections / self.files_in_span if self.files_in_span else 0.0


def make_keys(count: int) -> list[bytes]:
    """Fixed-width keys, so lexicographic order matches numeric order."""
    return [f"k-{index:08d}".encode() for index in range(count)]


def absent_keys(keys: list[bytes]) -> list[bytes]:
    """Probes that sort next to a real key but were never written."""
    return [key + b"~absent" for key in keys]


def counters(engine: Engine) -> Counters:
    """Sum the counters of the readers this engine has opened so far.

    The engine keeps one reader per table but exposes no aggregate, and the
    counters are what makes read amplification observable at all, so they are
    read where they live rather than re-derived from file sizes.
    """
    readers = list(engine._readers.values())
    return Counters(
        block_reads=sum(reader.data_block_reads for reader in readers),
        bloom_rejections=sum(reader.bloom_rejections for reader in readers),
        readers=len(readers),
    )


def delta(after: Counters, before: Counters) -> Counters:
    """Counter movement across one probe phase."""
    return Counters(
        block_reads=after.block_reads - before.block_reads,
        bloom_rejections=after.bloom_rejections - before.bloom_rejections,
        readers=after.readers,
    )


def drain(engine: Engine) -> int:
    """Run compaction to exhaustion and report how many jobs it took."""
    jobs = 0
    while engine.compact():
        jobs += 1
    return jobs


def build(
    home: Path,
    keys: list[bytes],
    value: bytes,
    *,
    bloom_rate: float,
    batches: int,
    compacting: bool,
) -> None:
    """Write the dataset in ``batches`` striped passes, flushing after each.

    Striping matters: pass ``b`` writes every ``batches``-th key, so each resulting
    file spans the entire key range. That is what makes the un-compacted layout
    maximally *overlapping* - the worst case a read path can face - instead of a
    set of tidy disjoint runs that would flatter the numbers.
    """
    engine = Engine.open(home, engine_options(bloom_rate=bloom_rate, compacting=compacting))
    try:
        for batch in range(batches):
            for index in range(batch, len(keys), batches):
                engine.put(keys[index], value, WriteOptions(sync=False))
            engine.flush()
        if compacting:
            drain(engine)
    finally:
        engine.close()


def probe(
    engine: Engine,
    keys: list[bytes],
    *,
    layout: str,
    bloom: bool,
    kind: str,
) -> Sample:
    """Run one probe phase and attribute the physical work it caused."""
    metas = [meta for level in engine.level_files for meta in level]
    in_span = sum(1 for key in keys for meta in metas if meta.covers(key))

    # Warm-up: creates the readers and loads their index regions, so the measured
    # window contains data block fetches only.
    for key in keys[:16]:
        engine.get(key)

    before = counters(engine)
    resolved = sum(1 for key in keys if engine.get(key) is not None)
    movement = delta(counters(engine), before)
    return Sample(
        layout=layout,
        bloom=bloom,
        probe=kind,
        probes=len(keys),
        resolved=resolved,
        files_in_span=in_span,
        block_reads=movement.block_reads,
        bloom_rejections=movement.bloom_rejections,
    )


def measure(
    home: Path,
    hits: list[bytes],
    misses: list[bytes],
    *,
    layout: str,
    bloom_rate: float,
    compacting: bool,
) -> tuple[list[Sample], str]:
    """Measure both probe kinds against one store and describe its shape."""
    engine = Engine.open(home, engine_options(bloom_rate=bloom_rate, compacting=compacting))
    try:
        levels = engine.level_files
        shape = " ".join(f"L{index}x{len(files)}" for index, files in enumerate(levels))
        bloom = bloom_rate > 0.0
        samples = [
            probe(engine, hits, layout=layout, bloom=bloom, kind="hit"),
            probe(engine, misses, layout=layout, bloom=bloom, kind="miss"),
        ]
    finally:
        engine.close()
    return samples, shape


def render(header: tuple[str, ...], rows: list[tuple[str, ...]], *, markdown: bool) -> str:
    """Render rows as a Markdown table, or as aligned plain columns."""
    lines = []
    if markdown:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        return "\n".join(lines)
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
    lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


def io_rows(samples: list[Sample]) -> list[tuple[str, ...]]:
    """Table A: where the reads went, in block fetches."""
    return [
        (
            sample.layout,
            "on" if sample.bloom else "off",
            sample.probe,
            f"{sample.probes:,}",
            f"{sample.files_per_probe:.2f}",
            f"{sample.block_reads:,}",
            f"{sample.reads_per_probe:.2f}",
            f"{sample.bloom_rejections:,}",
            f"{sample.rejection_rate:.0%}",
        )
        for sample in samples
    ]


def byte_rows(samples: list[Sample], value_bytes: int, block_bytes: int) -> list[tuple[str, ...]]:
    """Table B: physical bytes against the payload the caller received."""
    rows: list[tuple[str, ...]] = []
    for sample in samples:
        physical = sample.block_reads * block_bytes
        logical = sample.resolved * value_bytes
        ratio = f"{physical / logical:.2f}x" if logical else "n/a"
        rows.append(
            (
                sample.layout,
                "on" if sample.bloom else "off",
                sample.probe,
                f"{physical / 1024:.1f} KiB",
                f"{logical / 1024:.1f} KiB",
                ratio,
            )
        )
    return rows


def environment(keys: int, value_bytes: int, probes: int, batches: int) -> list[tuple[str, str]]:
    """Host and workload facts a reader needs before any ratio means anything."""
    return [
        ("Engine", f"sable {sable.__version__}"),
        ("Python", f"{platform.python_version()} ({sysconfig.get_platform()})"),
        ("OS", f"{platform.system()} {platform.release()}"),
        ("CPU count", str(os.cpu_count() or 1)),
        ("Dataset", f"{keys:,} keys x {value_bytes} B values, striped over {batches} flushes"),
        ("Probes", f"{probes:,} present keys + the same number of absent ones"),
        ("Block size", f"{BLOCK_BYTES} B"),
    ]


def find(samples: list[Sample], layout: str, bloom: bool, kind: str) -> Sample:
    """Look one cell up, failing loudly rather than printing a wrong comparison."""
    for sample in samples:
        if (sample.layout, sample.bloom, sample.probe) == (layout, bloom, kind):
            return sample
    raise LookupError(f"no sample for {layout}/bloom={bloom}/{kind}")


def main(argv: list[str] | None = None) -> int:
    """Run the matrix and print both tables.

    Args:
        argv: Process arguments. ``--keys`` sizes the dataset, ``--probes`` the
            number of reads per cell, ``--batches`` the number of overlapping
            flushes, ``--markdown`` emits paste-ready tables.

    Returns:
        Zero: this script reports measurements, it does not grade them.
    """
    parser = argparse.ArgumentParser(prog="bench_read_amplification", description=__doc__)
    parser.add_argument("--keys", type=int, default=DEFAULT_KEYS)
    parser.add_argument("--value-bytes", type=int, default=DEFAULT_VALUE_BYTES)
    parser.add_argument("--probes", type=int, default=DEFAULT_PROBES)
    parser.add_argument("--batches", type=int, default=DEFAULT_BATCHES)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--keep-home", type=Path, default=None)
    args = parser.parse_args(argv)

    scratch = Path(tempfile.mkdtemp(prefix="sable-readamp-"))
    keys = make_keys(args.keys)
    value = b"v" * args.value_bytes
    # One shuffled sample, reused by every cell so the cells differ in layout and
    # filter only - not in which keys happened to be asked for.
    picks = list(keys)
    random.Random(RANDOM_SEED).shuffle(picks)  # noqa: S311 - ordering, not security
    hits = picks[: args.probes]
    misses = absent_keys(hits)

    print("# sable read amplification")
    print()
    for label, fact in environment(args.keys, args.value_bytes, len(hits), args.batches):
        print(f"- {label}: {fact}")
    print()

    samples: list[Sample] = []
    shapes: list[tuple[str, bool, str]] = []
    for layout in LAYOUTS:
        for bloom in (True, False):
            rate = BLOOM_RATE if bloom else 0.0
            name = f"{layout}-{'bloom' if bloom else 'nobloom'}"
            home = (args.keep_home / name) if args.keep_home else scratch / name
            build(
                home,
                keys,
                value,
                bloom_rate=rate,
                batches=args.batches,
                compacting=layout == "levelled",
            )
            cell, shape = measure(
                home,
                hits,
                misses,
                layout=layout,
                bloom_rate=rate,
                compacting=layout == "levelled",
            )
            samples.extend(cell)
            shapes.append((layout, bloom, shape))

    print("## Store shape after the write phase")
    print()
    for layout, bloom, shape in shapes:
        print(f"- {layout:<12} bloom={'on ' if bloom else 'off'} -> {shape}")
    print()

    print("## Block reads per logical read")
    print()
    print(
        render(
            (
                "layout",
                "bloom",
                "probe",
                "probes",
                "files/probe",
                "block reads",
                "reads/probe",
                "rejected",
                "reject rate",
            ),
            io_rows(samples),
            markdown=args.markdown,
        )
    )
    print()

    print("## Physical bytes against payload returned")
    print()
    print(
        render(
            ("layout", "bloom", "probe", "physical", "logical", "amplification"),
            byte_rows(samples, args.value_bytes, BLOCK_BYTES),
            markdown=args.markdown,
        )
    )
    print()

    miss_overlap_off = find(samples, "overlapping", False, "miss")
    miss_overlap_on = find(samples, "overlapping", True, "miss")
    miss_levelled_off = find(samples, "levelled", False, "miss")
    miss_levelled_on = find(samples, "levelled", True, "miss")
    hit_overlap_off = find(samples, "overlapping", False, "hit")
    hit_overlap_on = find(samples, "overlapping", True, "hit")
    hit_levelled_on = find(samples, "levelled", True, "hit")

    for sample in samples:
        expected = sample.probes if sample.probe == "hit" else 0
        assert sample.resolved == expected, f"{sample.layout}/{sample.probe} resolved wrongly"

    # A filter can only ever remove work, never add it.
    assert miss_overlap_on.block_reads <= miss_overlap_off.block_reads, "filter added misses"
    assert hit_overlap_on.block_reads <= hit_overlap_off.block_reads, "filter added hits"
    # The same probe against a merged store must not cost more than against a
    # stack of overlapping files.
    assert miss_levelled_off.block_reads <= miss_overlap_off.block_reads, "levelling cost more"
    assert hit_levelled_on.block_reads <= hit_overlap_on.block_reads, "levelling cost more"

    print("## What the numbers say")
    print()
    print(
        f"- **An absent key.** With no filter every table whose key span admits the probe "
        f"pays a block fetch: {miss_overlap_off.reads_per_probe:.2f} reads per probe across "
        f"{miss_overlap_off.files_per_probe:.2f} files. With a filter the same probe costs "
        f"{miss_overlap_on.reads_per_probe:.2f}, because {miss_overlap_on.rejection_rate:.0%} "
        "of the candidates are answered from a bit vector."
    )
    print(
        f"- **A present key.** The search runs newest-first and stops at the table that "
        f"holds the key, so a key written by the first pass waits behind every later one: "
        f"{hit_overlap_off.reads_per_probe:.2f} fetches on average. With a filter those elder "
        f"tables are rejected without IO, leaving {hit_overlap_on.reads_per_probe:.2f} fetch - "
        "the table that actually answers, plus the filter's own false positives."
    )
    print(
        f"- **Merging the same data.** Compaction collapsed the span to "
        f"{hit_levelled_on.files_per_probe:.2f} file per probe, at which point the two probe "
        f"kinds cost {hit_levelled_on.reads_per_probe:.2f} and "
        f"{miss_levelled_on.reads_per_probe:.2f} reads. That convergence, not the throughput "
        "of any single operation, is what compaction buys."
    )
    print(
        f"- **The floor is the block, not the filter.** A 96 B value served from a "
        f"{BLOCK_BYTES} B block is {BLOCK_BYTES / args.value_bytes:.1f}x amplified before any "
        f"file is duplicated. The overlapping unfiltered hit sits at "
        f"{hit_overlap_off.reads_per_probe * BLOCK_BYTES / args.value_bytes:.1f}x of the payload "
        "it returns, so layout adds a factor of "
        f"{hit_overlap_off.reads_per_probe:.1f} on top of the granularity penalty - and that "
        "factor is what both the filter and the merge are competing to remove."
    )
    print()

    if args.keep_home is None:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
