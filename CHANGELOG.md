# Changelog

All notable changes to `sable` are recorded here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); while the engine is
pre-1.0 every release may change layout or API, and this file says so explicitly
rather than letting developers discover it at recovery time.

Format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

[0.1.0]: https://github.com/MKA930/sable/releases/tag/v0.1.0
