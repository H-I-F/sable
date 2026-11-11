[English](README.md) | [简体中文](README.zh-CN.md)

# sable — Engineering Knowledge Base

> This directory is the long-form companion to the [project README](../README.md).
> The README is the entry point for *users*; these pages are for people who want to
> understand **why** every byte is laid out the way it is, how the concurrency model
> stays correct under real threads, and which trade-offs were made on purpose.
>
> Every claim here is anchored to a real symbol in `src/sable/`. When a page says
> "see `engine.py`", the link resolves to the file, not a paraphrase.

## What lives here

| Section | For | Format |
| --- | --- | --- |
| [Skills](#skills-low-level-implementation-practice) | engineers implementing or porting the engine | narrative guides with code and tables |
| [FAQ](#faq-hard-storage-system-questions) | deep-dive questions, design reviews, "why not just…" pushback | bilingual Q&A with Mermaid diagrams |
| [Architecture Decision Records](#architecture-decision-records) | anyone changing the on-disk or in-memory contracts | ADR-001 … ADR-005 |
| [Benchmarks](#benchmarks) | performance work, regression hunting | runnable harnesses |
| [Reports](#reports-measured-numbers) | capacity planning, "will this hold at 10x" | measured runs on a named host |
| [Recipes](#recipes-building-on-the-engine) | solving a problem *with* the engine | runnable patterns, cost tables |
| [Case studies](#case-studies) | post-mortems, design review | narrative with the reproduction |
| [Release notes](#release-notes) | upgrading between versions | one announcement per tag, bilingual |
| [Release & maintenance policy](../RELEASING.md) | what earns a version and what merges on its own | one live rule, enforced by CI |
| [Source map](#source-map) | finding the code behind a concept | module table |

## Case studies

- [When `get` and `scan` disagreed about a snapshot](case-studies/snapshot-read-consistency.md) /
  [简体中文](case-studies/snapshot-read-consistency.zh-CN.md) — the v0.1.1 snapshot-read fix,
  reproduced in pure `sable` and written up from the upstream side. The reporting project's account
  is [here](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.md).

## Release notes

- [v0.1.0](release-notes-v0.1.0.md) / [简体中文](release-notes-v0.1.0.zh-CN.md) — first coherent
  release: the complete LSM tree, the crash matrix, the inspectors and the knowledge base.
- [v0.1.1](release-notes-v0.1.1.md) / [简体中文](release-notes-v0.1.1.zh-CN.md) — the snapshot-read
  correctness fix and `Engine.open_snapshots`.
- [v0.1.2](release-notes-v0.1.2.md) / [简体中文](release-notes-v0.1.2.zh-CN.md) — the bilingual
  engineering-notes set; no engine changes.
- [v0.1.3](release-notes-v0.1.3.md) / [简体中文](release-notes-v0.1.3.zh-CN.md) — the pinned
  linter update and synced contributor guides; no engine changes.
- [v0.1.4](release-notes-v0.1.4.md) / [简体中文](release-notes-v0.1.4.zh-CN.md) — the auto-merge
  workflow, monthly grouped Dependabot, and the rule for future releases.

## Skills: low-level implementation practice

Three deep guides, each grounded in the actual source rather than generic advice.

- [Write-Ahead Logging and Torn-Tail Recovery](skills/wal-and-recovery.md) / [简体中文](skills/wal-and-recovery.zh-CN.md) — the 21-byte frame
  anatomy, why CRC32 sits at the *front*, the five-case recovery decision table, the
  distinction between a discardable torn tail and a fatal mid-stream checksum, and a
  WAL state machine designed so byte-image, `monkeypatch`, and SIGKILL tests can all
  drive it.
- [Atomic Publish](skills/atomic-flush.md) / [简体中文](skills/atomic-flush.zh-CN.md) — `temp → fsync → rename → fsync(parent)`,
  why skipping the directory fsync leaves data durable but unnamed, `os.replace` vs
  `os.rename`, and the documented Windows/NTFS degradation.
- [Concurrency Control](skills/concurrency-control.md) / [简体中文](skills/concurrency-control.zh-CN.md) — reference-counted file leases
  versus a reader-writer lock, snapshot sequence bands, the three-lock set and its
  ordering rules, and deterministic race tests built on `threading.Barrier`.

## FAQ: hard storage-system questions

Bilingual, code-anchored, diagram-backed. Eight questions spanning WAL/recovery,
atomic publish, concurrency/compaction, tombstones/memtable snapshots, and Bloom
filters — four Mermaid diagrams in each file.

- English: [Storage Engine Deep-Dive Q&A](faq/deep-dive-qa.md)
- 简体中文: [存储引擎硬核追问与深度解答](faq/deep-dive-qa.zh-CN.md)

## Architecture Decision Records

Five decisions that shaped the engine, each in **Status / Context / Decision /
Consequences** form. Read these before changing any on-disk or in-memory contract.

- [ADR-001: WAL frame integrity uses CRC32](decisions/adr-001-wal-frame-crc32.md) / [简体中文](decisions/adr-001-wal-frame-crc32.zh-CN.md) — why
  CRC32, not SHA-256 or CRC64, for per-frame verification.
- [ADR-002: Compaction is explicit, not scheduled](decisions/adr-002-manual-compaction-policy.md) / [简体中文](decisions/adr-002-manual-compaction-policy.zh-CN.md) —
  why a background compactor thread was rejected.
- [ADR-003: Reference-counted file leases for snapshot safety](decisions/adr-003-refcount-file-snapshots.md) / [简体中文](decisions/adr-003-refcount-file-snapshots.zh-CN.md) —
  why file leases beat an RWLock, epochs, and finalisers.
- [ADR-004: Synchronous WAL flush by default (append-before-apply)](decisions/adr-004-sync-flush-guarantees.md) / [简体中文](decisions/adr-004-sync-flush-guarantees.zh-CN.md) —
  why every acknowledged write is fsync-ed immediately.
- [ADR-005: Sparse per-block index at the tail of each SSTable](decisions/adr-005-sparse-index-layout.md) / [简体中文](decisions/adr-005-sparse-index-layout.zh-CN.md) —
  why the index is sparse, per-block, and at the tail rather than the head.

## Benchmarks

All four are reproducible and self-asserting: a clean run exits `0` and prints a
verdict; a violated invariant raises instead of printing a misleading number.

- [Concurrent read/write stress under compaction](../benchmarks/bench_concurrent.py) —
  `python -m benchmarks.bench_concurrent`. Multi-threaded `get`/`scan` contending
  with a background compactor; reports P95/P99/throughput and asserts no read throws.
- [WAL throughput ceilings](../benchmarks/bench_wal_throughput.py) —
  `python -m benchmarks.bench_wal_throughput`. Durable vs. grouped vs. unsafe puts,
  measured against real disk `fsync`.
- [Read amplification matrix](../benchmarks/bench_read_amplification.py) —
  `python -m benchmarks.bench_read_amplification`. Physical block reads per logical
  point read across two file layouts × filter on/off × hit/miss, taken from each
  reader's own counter rather than estimated.
- [Core engine benchmark](../benchmarks/bench_engine.py) —
  `python -m benchmarks.bench_engine --markdown`. The headline numbers used in the
  README.

## Reports: measured numbers

Benchmarks measure; a report commits to a reading of them, together with the host it
was taken on and the parts it does not cover.

- [Pre-release benchmark report](reports/benchmark.md) / [简体中文](reports/benchmark.zh-CN.md) — write path (198–219
  durable records/s against 102,398–117,160 with `fsync` disabled), read path
  (p50 38.7–41.4 µs on a hit, 8.2–9.0 µs on a miss), write amplification 3.54x
  decomposed into WAL, flush and merge, space amplification 1.17x per generation
  and 2.35x after one uncompacted overwrite, and why six reading threads deliver
  0.98x what one does. Every figure is from a committed script; where a measurement
  drifts between runs the report publishes the spread.

## Recipes: building on the engine

The engine is the substrate, not the product. These pages take a problem the engine
does *not* solve and build the solution from the two primitives it does expose —
ordered range scans and tombstones.

- [Application-level TTL eviction](recipes/ttl-eviction.md) / [简体中文](recipes/ttl-eviction.zh-CN.md) — `sable` has no expiry
  thread and no compaction filter, so expiry is built from a big-endian, fixed-width
  index key that turns "everything older than now" into a half-open range query.
  Three runnable snippets (`put_with_ttl`, `get_if_live`, `evict_expired`), the
  six-stage tombstone lifecycle with the read/write/space cost each stage adds, and
  the generation-guard rule that keeps a rewritten record from inheriting a stale
  deadline.

## Source map

| Module | Responsibility | Key symbols |
| --- | --- | --- |
| [`sable/types.py`](../src/sable/types.py) | value types, budgets, `SSTableMeta` | `InternalEntry`, `KeyRange`, `SSTableMeta` |
| [`sable/errors.py`](../src/sable/errors.py) | corruption vs. torn-write vs. caller error | `CorruptionError`, `ChecksumMismatchError` |
| [`sable/fsutil.py`](../src/sable/fsutil.py) | atomic publish primitive | `write_atomically` |
| [`sable/wal.py`](../src/sable/wal.py) | frame encode/decode, torn-tail scan | `WalReader`, `WalWriter` |
| [`sable/memtable.py`](../src/sable/memtable.py) | skip-list, `(key, -sequence)` order | `MemTable` |
| [`sable/sstable.py`](../src/sable/sstable.py) | blocks, index, Bloom, footer, positional reads | `SSTableReader`, `SSTableWriter` |
| [`sable/bloom.py`](../src/sable/bloom.py) | double-hashing filter | `BloomFilter` |
| [`sable/compaction.py`](../src/sable/compaction.py) | leveled planner, tombstone reclamation | `Planner` |
| [`sable/engine.py`](../src/sable/engine.py) | version set, manifest, reads, leases | `Engine`, `Snapshot` |
| [`sable/cli.py`](../src/sable/cli.py) | `put` / `get` / `delete` / `scan` / `stats` | `main` |

## Where to start

A suggested reading order for someone dropped into the codebase cold:

1. [`README.md`](../README.md) — the one-page mental model and diagrams.
2. [Skills → WAL](skills/wal-and-recovery.md) then [Skills → Atomic Publish](skills/atomic-flush.md) —
   the two primitives every other guarantee rests on.
3. [Skills → Concurrency Control](skills/concurrency-control.md) — the part that is
   wrong in most LSM implementations.
4. [ADR-001](decisions/adr-001-wal-frame-crc32.md) through
   [ADR-005](decisions/adr-005-sparse-index-layout.md) — the reasoning behind the
   contracts you are about to touch.
5. [FAQ (en)](faq/deep-dive-qa.md) — the "why not just…" pushback, pre-answered.

---

[← Back to project README](../README.md)
