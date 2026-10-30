# ADR-005: Sparse per-block index at the tail of each SSTable

**Status:** Accepted — implemented in PR 04, refined in PR 08 and PR 09.

**Date:** 2026-10-24

**Deciders:** engine maintainer

---

## Context

A point read on an SSTable must find one record among `num_entries` without
reading the whole file. Three things have to be balanced:

- **Memory.** The index is resident for the lifetime of the open file. An engine
  with thousands of open tables cannot hold one index row per record.
- **I/O.** Each extra block read on a point lookup is read amplification, and it
  is the cost that actually shows up in the benchmark numbers.
- **Simplicity.** This is a reference implementation whose layouts are meant to be
  read, not just used.

Default block size is 4096 bytes (`EngineOptions.block_size_bytes`), so a
100 MB table holds roughly 25,000 blocks and potentially millions of records.

---

## Decision

One index row **per data block**, serialised after the data region:

```
block_offset | block_size | first_key_len | block_crc32 | first_key bytes
   uint64        uint32        uint32          uint32
```

`_INDEX_ROW_HEADER = struct.Struct("<QIII")`, defined in `src/sable/sstable.py`.

Lookup is two steps:

1. **Binary search the index** on `first_key` — pure memory, no I/O — to find the
   only block that can contain the key.
2. **Read that one block** and scan its records linearly.

The index is loaded lazily (`_ensure_index`) the first time a read needs it, so
opening a table does not parse a file that may never be queried.

Each row carries the block's CRC32, so the checksum is verified against the
block actually being read rather than by whole-file checksumming at open time.
The footer re-checks the header CRC, which is what makes "a file is either
complete or absent" true rather than aspirational.

---

## Alternatives considered

### Full index — one row per record

Rejected on memory. It gives O(1) position and zero intra-block scanning, at the
cost of an index that scales with record count rather than with file size. For a
4 KiB block holding dozens of records, that is one to two orders of magnitude
more resident memory per open table — and the whole point of a layered tree is
that many tables are open at once.

The I/O difference is also smaller than it looks: a full index turns step 2's
linear scan into a direct seek, but step 2 reads one 4 KiB block either way. The
saving is CPU, not I/O, and CPU is not the bottleneck at these latencies.

### No index — scan the file

Rejected. Point reads would become O(file size), which makes read amplification
proportional to table size. That is the failure mode the layered design exists to
bound.

### B-tree over the data region

Rejected as disproportionate for an immutable file. A B-tree earns its complexity
when the structure must support incremental updates; an SSTable is written once
and never modified. A sorted array of block first-keys gives the same
`O(log blocks)` lookup with no pointer chasing, no node splitting, and a layout a
reader can verify by reading it top to bottom.

### Index at the head of the file

Rejected. The index size is not known until the data region is written, so a
head-of-file index requires either a two-pass write or a fixed-size reservation
that can overflow. Placing it after the data region lets the writer stream: build
blocks, record rows, serialise the index, then the filter block, then the footer
with all offsets.

---

## Rationale

Sparse indexing matches the unit of I/O to the unit of indexing. A block is what
the filesystem hands back, so one row per block means the index never points
inside a unit the reader does not already have. Everything finer is CPU
optimisation; everything coarser costs whole extra reads.

The CRC per row is the part worth calling out. Checksumming the whole file at
open time would cost a full read of every table on every `Engine.open` — and
`open` is on the recovery path, where latency matters most. Per-block CRC32 means
corruption is detected when and where it is encountered, against the exact bytes
being handed to the caller.

---

## Consequences

**Positive**

- Index memory is one row per 4 KiB, independent of record count. Ten thousand
  open tables cost tens of megabytes, not gigabytes.
- Point lookup reads exactly one data block after a memory-only binary search.
- Range scans benefit more than point reads: blocks are ordered by first key, so
  once a block starts past the window, every later block does too and no further
  I/O is justified (`iter_slice` returns on the first block that cannot hold the
  range).
- Corruption is detected per block, so a bad block raises rather than being
  silently skipped.
- The layout is verifiable by reading it sequentially — no pointers into the
  middle of structures.

**Negative**

- Intra-block lookup is a linear scan over the block's records. Worst case is
  one block's worth of comparison work per point read, which is CPU and therefore
  cheap, but it is not O(1).
- Index accuracy depends on `first_key` alone. A key can only be in block *i* if
  `first_key[i] <= key < first_key[i+1]`, which is exact — but a block whose
  first key is greater than the target does not prove the key is absent from the
  *file*; the Bloom filter block (`src/sable/bloom.py`) answers that case
  without I/O.
- Keys are stored verbatim in each record. Prefix compression with restart points
  is not implemented: `EngineOptions.block_restart_interval` is carried in the
  options surface but no writer path consumes it today, so long common prefixes
  cost bytes that a restart-point scheme would reclaim.
- Changing `block_size_bytes` changes the index density, so a table written with
  one block size is still readable with another — the index is self-describing —
  but performance characteristics differ.

**Interaction with other decisions**

- ADR-001: the same CRC32 choice, applied per block rather than per frame.
- [`../skills/atomic-flush.md`](../skills/atomic-flush.md): the index is part of
  the single image published atomically, which is why a reader never observes an
  index that disagrees with its data region.
- The Bloom filter block sits immediately after the index and is consulted
  *before* it: a negative filter answer skips the binary search and the block
  read entirely, which is the source of the measured 5.74x miss-versus-hit gap.
