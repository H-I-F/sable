# Changelog

All notable changes to `sable` are recorded here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); while the engine is
pre-1.0 every release may change layout or API, and this file says so explicitly
rather than letting developers discover it at recovery time.

Format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] - 2026-10-30

A correctness fix for snapshot reads that a pinned view performs after a key has
been overwritten while the older and newer versions still coexist in the active
memtable.

### Fixed

- **Snapshot point lookups now resolve to the newest *visible* version.** A key
  holding a newer, post-snapshot version beside an older one the view is still
  entitled to previously returned absence from `engine.snapshot().get(key)`,
  because the memtable lookup only considered the newest version and gave up when
  it was invisible — even though an older visible version existed in the same
  table. Range scans already filtered by visibility and therefore disagreed with
  point lookups on what a pinned sequence number resolved to. `memtable.get_visible`
  now walks the version run and returns the first version at or below the view's
  sequence, matching the SSTable reader's long-correct behaviour.
- **`Engine.open_snapshots`** exposes the count of unreleased views, so a caller
  can assert that a snapshot acquire/release pair netted to zero without reaching
  into engine internals.
- Fixed a concurrency issue discovered during the development of downstream
  project sable-vector.

## [0.1.0] - 2026-10-30

First coherent release: a complete single-node LSM-tree with every durability
decision stated as a filesystem invariant.

### Added

- **Package skeleton and domain contract** — `sable.types` fixes the vocabulary
  (`InternalEntry`, `KeyRange`, `SSTableMeta`, options blocks) and
  `sable.errors` fixes the failure taxonomy, so corruption, torn writes and
  caller mistakes never collapse into one another. Ships `py.typed`.
- **Write-ahead log** — self-describing 21-byte frames (`crc32 | sequence | op |
  klen | vlen`), CRC32 over every byte after the checksum itself, and torn-tail
  recovery that distinguishes a partial trailing frame (discardable) from a bad
  CRC in a surviving frame (fatal). `truncate_torn_tail` repairs the segment in
  place so recovery is idempotent.
- **MemTable** — a deterministic skip list ordered by `(user_key, -sequence)`,
  which makes "newest version first" structural rather than a convention. Inserts
  reject sequence regressions instead of silently corrupting read-your-writes.
  Traversal and linking are serialised so concurrent readers never see a
  half-linked tower.
- **SSTable** — restart-aligned data blocks with per-block CRC32, a sparse index
  row per block, a Bloom filter block, and a fixed footer re-checking the header.
  Published through `temp -> fsync -> rename -> fsync(parent-dir)`, so a table is
  either complete or absent.
- **Read path** — active memtable, then immutables, then L0 newest-first, then
  L1..Ln. The walk stops *at* a tombstone: that is how it masks elder versions
  rather than merely skipping one.
- **Write path** — append-before-apply. A sequence is allocated, the frame is
  fsync-ed, and only then does the entry become visible, which is what makes an
  acknowledgement mean "durable".
- **CLI** — `sable put | get | delete | scan | stats`, including level
  distribution, per-level bytes and an estimated read amplification figure.
- **Range scan** — sparse-index seek plus a multi-level k-way merge; tombstones
  are consumed during the merge, so a deleted key is absent rather than yielded
  with a sentinel.
- **Compaction** — a leveled planner (L0 file count, then per-level byte budget)
  that physically reclaims superseded versions, and drops tombstones at the
  deepest level where nothing remains for them to hide.
- **Snapshots** — `Engine.snapshot()` returns a sequence-pinned view acquired in
  O(1) with nothing copied. Files are protected by reference counts, versions by
  a retention rule keyed on each view's visibility band. Compaction refuses to
  discard what a live view can still read.
- **Crash-recovery test matrix** — ten `SIGKILL` injection points auditing a
  reopened store against exactly the writes it acknowledged.
- **Benchmark harness** — `python -m benchmarks.bench_engine` measures durable
  writes, memtable publish, point hits, point misses, full scans, compaction and
  reopen cost, and emits the Markdown table the READMEs embed.
- **Container image** — multi-stage build producing a wheel-only runtime that runs
  unprivileged with data on a volume.
- **Store inspectors** — three read-only CLIs that answer questions about a store
  *without opening it*, which is what makes them usable against the wreckage of a
  failed run. `python -m sable.inspector` audits a WAL frame by frame and separates
  a discardable torn tail from a fatal mid-stream checksum; `python -m
  sable.sstable_inspector` decodes a table's header, sparse index and Bloom
  geometry, and back-solves the filter's fill ratio into an entry count and false
  positive rate; `python -m sable.topology` prints which file at which level holds
  which key range, including active and pinned immutable versions.
- **Fault-injection suite** — `tests/test_chaos_enospc.py` drives a WAL that refuses
  every write and a flush whose `fsync` fails, asserting that a rejected write
  leaves no key visible, no partial `.sst`, no `.tmp-sable` and no phantom key after
  reopen. `tests/test_chaos_fd_leak.py` asserts the descriptor count returns to
  baseline after close, stays bounded through mixed write/flush/compact traffic, and
  holds under three reading threads racing a compactor.
- **Read-amplification harness** — `python -m benchmarks.bench_read_amplification`
  reports physical block reads per logical point read across two file layouts
  (six overlapping L0 files versus one merged run) and filter on/off, taken from each
  reader's own `data_block_reads` counter rather than estimated.
- **TTL eviction recipe** — `docs/recipes/ttl-eviction.md` builds expiry from the two
  primitives the engine exposes (ordered range scans and tombstones) because there
  is no expiry thread and no compaction filter, and prices the six-stage tombstone
  lifecycle it creates.
- **Pre-release benchmark report** — `docs/reports/benchmark-2024.md` publishes the
  measured write path, read path, and all three amplification factors on one named
  host, with the spread between runs and an explicit list of what it does not cover.
- **Contributor guide and Makefile** — `CONTRIBUTING.md` states the four gates and
  the conventions the tools enforce; `make test | lint | typecheck | chaos | clean`
  wraps the exact commands CI runs, so "it passed locally" means the same thing.

### Changed

- Flush publishes its SSTable outside the global engine lock; the table stays
  visible in the immutable set until its replacement is linked, so no reader can
  observe a gap between "memtable gone" and "table present".
- Compaction runs its merge with no engine lock at all — inputs are immutable and
  outputs are unnamed until install — so maintenance overlaps real work instead
  of stalling it.

### Security

- No token, secret or credential is ever written into a source file, commit or
  test fixture; the crash harness runs the interpreter against a script embedded
  in the test module and nothing from the outside.

### Fixed

- The inspector CLIs failed a missing path in opposite ways: `sstable_inspector`
  raised `FileNotFoundError` with a traceback, while `inspector` printed
  `0 frame(s)` and exited `0`, so a pipeline could not tell "empty segment" from
  "segment is gone". Both now exit `2` with a one-line reason on stderr, and the
  SSTable CLI routes a foreign or corrupt file through the same path. The library
  functions are unchanged — `inspect_wal` still returns `[]` for a missing file,
  which is what segment iteration needs.

[0.1.0]: https://github.com/MKA930/sable/releases/tag/v0.1.0
