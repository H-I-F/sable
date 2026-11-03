[English](snapshot-read-consistency.md) | [简体中文](snapshot-read-consistency.zh-CN.md)

# When `get` and `scan` disagreed about a snapshot

A bug that arrived from downstream, and turned out to be two implementations of the
same predicate inside this engine.

## Summary

| | |
| --- | --- |
| **Symptom** | A pinned `Engine.snapshot()` could fail to find a committed key whose newest version post-dated the snapshot |
| **Reported by** | Concurrent-read testing in [`sable-vector`](https://github.com/H-I-F/sable-vector), which pinned this repo at the time |
| **Root cause** | `MemTable.get()` looked only at the head of a key's version run and gave up if it was invisible; range scans walked the run and filtered by visibility |
| **Fix** | `MemTable.get_visible()` — return the first version at or below the view's sequence |
| **Shipped as** | [`v0.1.1`](https://github.com/H-I-F/sable/releases/tag/v0.1.1) |

## How it got here

Two tests in sable-vector's suite failed on every run. One of them was cosmetic —
it referenced an observability attribute the engine did not expose. The other was
not: a concurrent reader saw a committed record's payload while its vector was
missing. Since a vector record is staged across several keys, that symptom could
mean either a broken staged write downstream or a broken visibility rule here.

The useful move was to reproduce the behaviour with no downstream code at all:

```python
# put K=A  -> sequence 1
# put K=B  -> sequence 4 (same key, newer version)
# snapshot pinned at sequence 2
view.get(b"K")   # expected b"A" (newest version <= 2)
                 # actual: None
```

Three lines of pure `sable`, no vector code in sight. That settled the ownership
question: this was an engine defect, and no amount of care in the caller could
work around it.

## What was actually wrong

The memtable is a deterministic skip list ordered by `(user_key, -sequence)`, so
the newest version of a key sits at the head of its version run. That makes
"newest wins" structural. But the engine has two read paths, and they did not
implement the same visibility rule:

- **Range scans** (`scan` / `_versioned_scan`) walk every version, filter by
  `sequence <= visible_sequence`, and take the first survivor. Correct.
- **Point lookups** (`MemTable.get`) went straight to the head, returned it if
  visible, and returned `None` otherwise — stopping instead of continuing down
  the run.

`SSTableReader.get_entry` already did the right thing, which is why the bug only
appeared while the key was still in the memtable: after a flush or compaction the
same read resolved correctly. It needed versions to *coexist* — exactly what a
concurrent writer produces. With a view pinned at sequence 2 and a sequence-4
version sitting in front of an entitled sequence-1 version, `get` found the
invisible head, stopped, and reported absence. A scan over the same key returned
`A`.

Two read paths giving two different answers to "what does this pinned sequence
resolve to" is not a subtle performance issue. It is the definition of broken,
and it invalidates the snapshot guarantee in `SECURITY.md`.

## The fix

Teach the memtable the rule the SSTable reader already had:

```python
def get_visible(self, user_key, visible_sequence):
    with self._mutation:
        node = self._descend_to(user_key)
        while node is not None:
            candidate = node.entry
            if candidate.user_key != user_key:
                break
            if visible_sequence is None or candidate.sequence <= visible_sequence:
                return candidate
            node = node.next[0]
    return None
```

`Engine._lookup_entry` now calls `table.get_visible(user_key, visible_sequence)`
for the memtable. `visible_sequence is None` still means "latest wins", so
ordinary reads are unchanged. Two regression tests were written at engine level,
not downstream level, so the invariant stays guarded even if the reporting project
disappears.

The other failing test wanted to assert that a snapshot acquire/release pair nets
to zero. `Engine.open_snapshots` now exposes the count of unreleased views; a
snapshot pins files and retains versions, so being able to check that balance
without reaching into internals is what stops a slow leak from becoming a mystery.

**Verification:** the pure-`sable` reproduction passes, both originally-failing
downstream tests turn green, this repo's suite stays at 183 tests with all five CI
checks green, and sable-vector stays at 374 passing / 1 xpassed — that `xpassed`
being the concurrency regression that used to be expected to fail.

## Why it could not be fixed downstream

`sable` has no atomic multi-key batch: every `put` gets its own sequence number.
A caller staging a record's parts has to rely on the storage engine's visibility
rule to make them look atomic to a reader. There is no sequence arithmetic the
caller could do to side-step a memtable that stops at the first invisible version.
The only honest options were to fix the engine or to document that concurrent
snapshot reads may lose committed keys.

## Shipping across two repositories

1. Land the fix here (PR #37) with two engine-level regression tests.
2. Bump to **v0.1.1** and tag it — a behaviour change needs a version, and the
   CHANGELOG entry names the downstream project that surfaced it.
3. The downstream repo moves its pin from `@main` to the released tag
   (`sable @ git+https://github.com/H-I-F/sable.git@v0.1.1`), so a release can be
   reproduced rather than pointing at whichever commit happened to exist.
4. Add an `## Ecosystem` section to both READMEs here, pointing back at the
   downstream write-up so the relationship is discoverable from either side.

## Lessons

- **A predicate implemented twice will eventually be implemented twice differently.**
  The invariant both paths owed was "the highest sequence at or below the view's
  sequence". One of them implemented it. That is two answers to one question.
- **Structure is not semantics.** Ordering by `(user_key, -sequence)` puts the
  newest version first; it does not make that version visible. The shortcut held
  until someone asked for a version that was neither newest nor absent.
- **Reproduce in the dependency.** Three lines of pure `sable` decided which
  repository to fix. Without them the natural assumption would have been a bug in
  the caller's staged writes.
- **A 100%-failing test is a gift.** Deterministic reproduction, already written
  for you; fixing it removes a permanent tax.

## Links

- [`v0.1.1` release](https://github.com/H-I-F/sable/releases/tag/v0.1.1)
- [Downstream write-up](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.md)
- [`sable-vector`](https://github.com/H-I-F/sable-vector) — the project that reported it
