# ADR-003: Reference-counted file leases for snapshot safety

**Status:** Accepted — implemented in PR 11.

**Date:** 2026-10-26

**Deciders:** engine maintainer

---

## Context

Compaction reads a set of SSTables, merges them, publishes the outputs, and
deletes the inputs. A concurrent read may be holding an open reader on one of
those inputs.

Two distinct hazards, which need different mechanisms:

1. **Lifecycle.** The file is unlinked while a reader still needs it. On POSIX a
   reader with an open fd keeps reading the unlinked inode, so the bug hides
   until a *different* reader opens the path and gets `FileNotFoundError` —
   nondeterministic, and probability scales with read concurrency. On Windows the
   `unlink` itself fails with `PermissionError`.
2. **Visibility.** Even with the file intact, the merge can drop a version that a
   live snapshot is still entitled to read.

This ADR covers hazard 1. Hazard 2 is handled by sequence bands in
`compaction.visible_versions` and is deliberately a separate mechanism.

Requirement: a reader must be able to complete against a tree that is being
rewritten underneath it, and compaction must not wait for readers to finish.

---

## Decision

`SSTableMeta` carries a `refs` counter. Every read acquires a **lease** on the
whole file set before it starts, through a single context manager:

```python
with self._lock:
    tables = [self._active, *self._immutable]
    metas = [meta for level in self._levels for meta in level]
    for meta in metas:
        meta.pin()
```

The lease is released in a `finally`, including when a generator is abandoned
mid-iteration and closed by the GC.

Compaction removes inputs from the version set immediately but defers the
physical unlink:

```python
if meta.refs > 0:
    self._pinned_obsolete.append(meta)
    continue
(home / meta.file_name).unlink(missing_ok=True)
```

`_pinned_obsolete` is the graveyard. The last reader to unpin triggers
`_discard_retired_files`, which unlinks everything whose `refs` reached zero and
returns the list so the containing directory is fsync-ed once for the batch.

The non-obvious requirement: **capturing the view and incrementing `refs` must be
atomic with respect to each other.** Split them and the race returns:

```
reader:  resolve metas           compaction: drop meta from _levels
reader:  (not yet pinned)        compaction: observe refs == 0 -> unlink
reader:  pin()  -> too late
```

---

## Alternatives considered

### Reader-writer lock around the file set

Rejected. An RWLock also prevents the unlink, but it conflates the logical and
physical effects of compaction. Holding the write side means compaction waits for
every in-flight reader, so a long scan directly delays maintenance. Reference
counting lets the *logical* effect land immediately — the file leaves the version
set at once — and defers only the *physical* effect.

It is also a worse fit for the read pattern: a scan needs a coherent set of files
for its whole duration, which under an RWLock means holding the read side for
that duration. Under counting it means a lease, which is one integer per file and
no waiting at all.

### Epoch-based reclamation (hazard pointers)

Rejected as disproportionate. Epoch reclamation shines when the reclaimer must
never block and the reader set is large and churning. Here the reader set is
bounded by the thread count, and the reclamation work is one `unlink`. The extra
bookkeeping would add global state that has to be scanned periodically — a
scheduler, which ADR-002 exists to avoid.

### Garbage collection / finalisers

Rejected. Deleting a file when its reader object is collected makes deletion
timing a function of the garbage collector, which differs between CPython, PyPy,
and future CPython versions. That is untestable: a test cannot assert *when* a
file disappears. An explicit lease makes the lifetime observable and
interpreter-independent.

### Copy the file set per read

Rejected. It would make read cost proportional to tree size rather than to the
data actually read, which defeats the point of a layered tree.

### Never delete during compaction; sweep at `close()`

Rejected. Space would be bounded by the lifetime of the process rather than by
the data, and a long-running store would grow without limit.

---

## Rationale

Reference counting converts *exclusive access* into *deferred reclamation*,
which is the weaker and more appropriate guarantee. Compaction never waits for a
reader; a reader never waits for compaction; the two interact only through an
integer that both already needed to look at.

The design also keeps the critical section tiny. `_lock` is held for one list
comprehension and a loop of integer increments — not for the scan. The
correctness requirement is not "hold the lock longer" but "make these two steps
atomic with respect to each other", which is a much weaker and much cheaper
condition.

---

## Consequences

**Positive**

- Compaction's logical result is immediate; only byte reclamation waits.
- Readers never block, and never block writers.
- A long scan cannot delay maintenance, and maintenance cannot delay a scan.
- Deletions are batched, so the directory is fsync-ed once per batch instead of
  once per file.
- The lifetime of a file is observable: `meta.refs` is inspectable, and
  `tests/test_concurrent.py::test_retired_file_outlives_the_walk_that_leased_it`
  asserts the observable consequence rather than an internal counter.

**Negative**

- Two mechanisms instead of one. File pinning does not address version
  visibility; `visible_versions(snapshots=...)` does. A future change that fixes
  one and assumes the other is a latent bug.
- A reader that never releases its lease — a generator stored somewhere and
  abandoned — pins bytes indefinitely. Leases are held by `contextlib`
  precisely so generator close releases them, but a caller that keeps an iterator
  alive keeps the files alive.
- Reclamation is not prompt: a deleted file's bytes persist until the last lease
  drops, so disk usage can briefly exceed the logical size of the tree.
- `unpin()` raises on an unbalanced release rather than silently tolerating it.
  That is deliberate — a double-release means the lease bookkeeping is wrong —
  but it makes a bug in a caller loud.

**Operational note**

Because reclamation is deferred, `stats`-style reporting on the tree can show
files that are no longer in the version set. That is expected: the version set is
what reads consult, and the graveyard is what the filesystem still holds.
