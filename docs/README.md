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
| [FAQ](#faq-hard-storage-system-questions) | interviews, design reviews, "why not just…" pushback | bilingual Q&A with Mermaid diagrams |
| [Architecture Decision Records](#architecture-decision-records) | anyone changing the on-disk or in-memory contracts | ADR-001 … ADR-005 |
| [Benchmarks](#benchmarks) | performance work, regression hunting | runnable harnesses |
| [Source map](#source-map) | finding the code behind a concept | module table |

## Skills: low-level implementation practice

Three deep guides, each grounded in the actual source rather than generic advice.

- [Write-Ahead Logging and Torn-Tail Recovery](skills/wal-and-recovery.md) — the 21-byte frame
  anatomy, why CRC32 sits at the *front*, the five-case recovery decision table, the
  distinction between a discardable torn tail and a fatal mid-stream checksum, and a
  WAL state machine designed so byte-image, `monkeypatch`, and SIGKILL tests can all
  drive it.
- [Atomic Publish](skills/atomic-flush.md) — `temp → fsync → rename → fsync(parent)`,
  why skipping the directory fsync leaves data durable but unnamed, `os.replace` vs
  `os.rename`, and the documented Windows/NTFS degradation.
- [Concurrency Control](skills/concurrency-control.md) — reference-counted file leases
  versus a reader-writer lock, snapshot sequence bands, the three-lock set and its
  ordering rules, and deterministic race tests built on `threading.Barrier`.

## FAQ: hard storage-system questions

Bilingual, code-anchored, diagram-backed. Eight questions spanning WAL/recovery,
atomic publish, concurrency/compaction, tombstones/memtable snapshots, and Bloom
filters — four Mermaid diagrams in each file.

- English: [Storage Engine Deep-Dive Q&A](faq/interview-qa-en.md)
- 简体中文: [存储引擎硬核追问与深度解答](faq/interview-qa-zh.md)

## Architecture Decision Records

Five decisions that shaped the engine, each in **Status / Context / Decision /
Consequences** form. Read these before changing any on-disk or in-memory contract.

- [ADR-001: WAL frame integrity uses CRC32](decisions/adr-001-wal-frame-crc32.md) — why
  CRC32, not SHA-256 or CRC64, for per-frame verification.
- [ADR-002: Compaction is explicit, not scheduled](decisions/adr-002-manual-compaction-policy.md) —
  why a background compactor thread was rejected.
- [ADR-003: Reference-counted file leases for snapshot safety](decisions/adr-003-refcount-file-snapshots.md) —
  why file leases beat an RWLock, epochs, and finalisers.
- [ADR-004: Synchronous WAL flush by default (append-before-apply)](decisions/adr-004-sync-flush-guarantees.md) —
  why every acknowledged write is fsync-ed immediately.
- [ADR-005: Sparse per-block index at the tail of each SSTable](decisions/adr-005-sparse-index-layout.md) —
  why the index is sparse, per-block, and at the tail rather than the head.

## Benchmarks

All three are reproducible and self-asserting: a clean run exits `0` and prints a
verdict; a violated invariant raises instead of printing a misleading number.

- [Concurrent read/write stress under compaction](../benchmarks/bench_concurrent.py) —
  `python -m benchmarks.bench_concurrent`. Multi-threaded `get`/`scan` contending
  with a background compactor; reports P95/P99/throughput and asserts no read throws.
- [WAL throughput ceilings](../benchmarks/bench_wal_throughput.py) —
  `python -m benchmarks.bench_wal_throughput`. Durable vs. grouped vs. unsafe puts,
  measured against real disk `fsync`.
- [Core engine benchmark](../benchmarks/bench_engine.py) —
  `python -m benchmarks.bench_engine --markdown`. The headline numbers used in the
  README.

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
5. [FAQ (en)](faq/interview-qa-en.md) — the "why not just…" pushback, pre-answered.

---

[← Back to project README](../README.md)
