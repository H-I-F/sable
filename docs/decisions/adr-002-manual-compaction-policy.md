# ADR-002: Compaction is explicit, not scheduled

**Status:** Accepted — implemented in PR 10, refined in PR 11.

**Date:** 2026-10-25

**Deciders:** engine maintainer

---

## Context

An LSM tree needs compaction to bound two things: the number of overlapping L0
files a read must consult (read amplification), and the space held by versions
nobody can read any more (space amplification). Both grow without it.

The usual answer is a background compactor thread. That was considered and
rejected for this engine.

Constraints specific to this project:

1. **The engine's claim is verifiable correctness**, not throughput. Every
   mechanism exists so a test can make an absolute assertion about it.
2. **CPython.** A compactor thread shares the GIL with the writers it would be
   "running in parallel" with.
3. **The test suite injects `SIGKILL` and drives barrier-synchronised races.**
   Both need to know when maintenance has happened.

---

## Decision

`Engine.compact()` is an explicit, synchronous call that plans, merges and
installs exactly one job and returns whether it did work. A caller wanting a
balanced tree loops:

```python
while engine.compact():
    pass
```

Two paths invoke it implicitly, both inside the calling thread:

- `_maybe_compact()` runs after a budget-driven flush, using
  `acquire(blocking=False)` — a writer that loses the race returns instead of
  queueing.
- `flush()` does **not** compact. An explicit flush is an operator action with an
  observable result; folding a merge into it would make level state depend on
  timing rather than on what was asked for.

There is no compactor thread, no work queue, and no timer.

---

## Alternatives considered

### Background compactor thread

Rejected. Three reasons, in order of weight:

- **Failure modes stop being observable.** A thread that raises has nowhere to
  report. It either dies silently — the tree degrades with no signal — or retries
  forever on an input that will never parse. Called inline, a compaction failure
  raises in the thread that caused it, at the point that caused it. For an engine
  whose entire claim is verifiable correctness, "an error happened off-thread and
  the tree kept running" is not an acceptable outcome.
- **Tests stop being deterministic.** The crash matrix needs to know whether
  compaction completed before the kill; the race suite needs compaction and a
  scan to genuinely overlap. With an opaque scheduler every such test needs a
  sleep or a hook. With an explicit call, `compact()` returning is the
  synchronisation point.
- **The GIL caps the benefit.** The merge is CPU-bound Python over
  `heapq.merge`. A background thread buys overlap on I/O and competes for the
  GIL on everything else, at unpredictable moments, plus all the lifecycle
  complexity of a thread that must be started, drained and stopped.

### Inline compaction on every write

Rejected. It would make write latency a function of tree shape, with no bound.
The current design compacts on a *threshold crossing*, not on every write, which
bounds the amortised cost without hiding it.

### No compaction at all

Rejected. Read amplification would grow without limit, and tombstones would
never be physically reclaimed, so a deleted key would keep costing space forever.
That contradicts the design goal of bounded, observable behaviour.

---

## Rationale

Back-pressure is better expressed as a threshold than as a scheduler.
`EngineOptions` carries `level0_file_num_compaction_trigger`,
`level0_slowdown_trigger`, `l1_bytes_trigger`, `max_levels` and
`level_size_multiplier`. The policy is data, and the *actuation* is a call the
operator owns. A caller who wants automation writes the three-line loop; a caller
who wants determinism calls it once. Neither has to fight the other.

Losing the race is free, which is why contention is expressed as
`acquire(blocking=False)` rather than a blocking acquire. Whoever holds the
compactor slot is already rewriting exactly the files this caller would have
chosen, so blocking would buy nothing and would turn a throughput spike into a
convoy of writers waiting on work already being done.

---

## Consequences

**Positive**

- Compaction failures surface at the call site, in the calling thread.
- Every maintenance step is a synchronisation point for tests; the race suite
  drives compaction and scans against a `threading.Barrier` and asserts on
  results rather than on timing.
- No thread lifecycle: no start, drain, stop, or join-on-shutdown logic, and no
  possibility of maintenance running after `close()`.
- Level state after a deterministic sequence of calls is deterministic, which is
  what lets the compaction tests assert exact file counts.

**Negative**

- A write-heavy workload that never calls `compact()` accumulates overlapping L0
  files, and read amplification grows. The mitigation is opportunistic
  (`_maybe_compact` after a budget-driven flush), not guaranteed.
- Compaction latency lands in whichever thread crossed the threshold. That
  thread is not necessarily the one that caused the pressure, so tail latency is
  less smooth than with a dedicated background thread.
- No compaction happens while the process is idle, because there is nothing to
  trigger it. Space held by reclaimable versions is recovered on the next write,
  not on a timer.

**Accepted trade-off**

The engine trades smooth tail latency for observable failure and testable
behaviour. For a reference implementation whose deliverable is provable
correctness, that is the right direction to err in. A production embedding would
write the three-line loop in a maintenance thread of its own and decide for
itself how to report a failure it can no longer raise.
