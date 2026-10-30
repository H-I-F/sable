# ADR-004: Synchronous WAL flush by default (append-before-apply)

**Status:** Accepted — implemented in PR 06, unchanged since.

**Date:** 2026-10-24

**Deciders:** engine maintainer

---

## Context

`put()` returning must mean something precise. The options are a spectrum:

- the value is in a Python buffer,
- the value is in the kernel page cache,
- the value is on stable storage.

Only the third survives `SIGKILL` *and* power loss. The engine's central claim —
"a write that returned successfully must be readable after the process dies" —
requires it, and the crash-recovery matrix in `tests/test_crash.py` asserts
exactly that against ten different kill points.

`fsync` on this machine is around 4.5 ms. Paying it per write is the single
largest cost the engine has: 223 ops/sec durable against 108,687 ops/sec with
syncing off, a factor of 487 (archived in the README benchmark table).

---

## Decision

Write path is **append-before-apply**, synchronous by default:

1. allocate a monotonic sequence,
2. serialise the frame,
3. `write` + `flush` + `fsync` the WAL,
4. *only then* insert into the memtable and return.

`EngineOptions.default_write_sync` is `True`. `WriteOptions(sync=False)` is a
per-call override for callers who knowingly accept a weaker guarantee — bulk
loads, benchmarks, and the compaction path, where the data being written is
already durable in the inputs.

Steps 3 and 4 are never reordered, and `Engine._stage_mutation` holds `_lock`
across both precisely so no observer can see the inverse order.

---

## Alternatives considered

### Async fsync, fsync on a timer

Rejected. It changes the meaning of a successful `put()` from "durable" to
"durable within N milliseconds", and there is no N small enough to preserve the
crash tests and large enough to be worth the complexity. It also introduces a
window whose size depends on wall-clock scheduling, which is exactly the kind of
timing-dependent behaviour the test suite exists to eliminate.

### Group commit (batch fsync across concurrent writers)

Not adopted. It is the standard production answer and it would recover most of
the throughput: one `fsync` covers several writers' frames. It costs a queue, a
batcher thread or a lease, and — decisively here — an acknowledgement that is no
longer a property of the calling thread. A writer in a group commit does not know
its frame is durable until the group flushes, and if the group leader dies the
caller cannot distinguish "committed" from "lost" without asking. That is a
failure mode this engine refuses to make unobservable.

`WriteOptions(sync=False)` is the documented escape hatch instead: it is explicit,
per-call, and the caller has decided.

### Write to the WAL but skip fsync; rely on the memtable

Rejected. `SIGKILL` does not flush page cache, so every acknowledged write since
the last flush would vanish. That is precisely the data loss the engine exists
to prevent.

### Memory-mapped WAL

Rejected. `mmap` makes the flush boundary implicit: `msync` is required to make
writes durable, and the mapping hides when bytes actually leave the process. A
durability guarantee that depends on `msync` timing is harder to reason about
than an explicit `write` + `fsync` pair, for no gain at these latencies.

---

## Rationale

The cost of durable writes is the cost of stable storage, and the engine should
not pretend otherwise. 4.5 ms per `fsync` is a property of the disk, not of the
code. The correct response is to make the cost visible and opt-outable, not to
absorb it into a weaker default.

The asymmetry is the design point: **syncing is the default because the safe
setting should be the one you get without asking.** A caller who wants throughput
passes `sync=False` and has made a decision; a caller who does not think about it
gets durability.

The benchmark table in both READMEs reports both numbers side by side and names
the ratio "the price of surviving a crash", so the cost is documented rather than
hidden.

---

## Consequences

**Positive**

- `put()` returning has one meaning, and it is the strong one.
- The crash matrix can assert exact equality — every id the child acknowledged is
  readable after reopen — instead of a probabilistic bound.
- Sequence allocation, WAL contents and memtable contents can never disagree in
  the durable direction: everything in the memtable is in the log.
- The guarantee is per-call and locally verifiable; no background state.

**Negative**

- Throughput is bounded by `fsync` latency: roughly 223 ops/sec on the reference
  machine, versus five orders of magnitude less work done per operation. This is
  the engine's dominant cost and it is visible in every write-heavy benchmark.
- `put()` latency is bimodal under contention, because a budget-crossing write
  also pays for a flush.
- The per-call `sync=False` override can be misused. It is documented in the
  README durability section as changing what an acknowledgement means.

**Interaction with other decisions**

- ADR-001: the frame carries CRC32 precisely because a synchronous fsync is the
  durability boundary the scanner needs to reason about.
- ADR-002: because compaction is explicit, its output writes use `sync=False`
  where the inputs are already durable — the override exists for exactly this.
- [`../skills/atomic-flush.md`](../skills/atomic-flush.md): the same
  temp → fsync → replace → fsync(parent) discipline applies to SSTables and the
  manifest, so the durability story is one story.
