[English](RELEASE_NOTES_v0.1.1.md) | [简体中文](RELEASE_NOTES_v0.1.1.zh-CN.md)

# sable v0.1.1

A correctness fix for snapshot reads that a pinned view performs after a key has been overwritten while the older and newer versions still coexist in the active memtable.

## Fixed
- **Snapshot point lookups now resolve to the newest *visible* version.** A key holding a newer, post-snapshot version beside an older one the view is still entitled to previously returned absence from `engine.snapshot().get(key)`: the memtable lookup only considered the newest version and gave up when it was invisible, even though an older visible version existed in the same table. Range scans already filtered by visibility and therefore disagreed with point lookups on what a pinned sequence number resolved to. `memtable.get_visible` now walks the version run and returns the first version at or below the view's sequence, matching the SSTable reader's long-correct behaviour.
- **`Engine.open_snapshots`** exposes the count of unreleased views, so a caller can assert that a snapshot acquire/release pair netted to zero without reaching into engine internals.

## Upgrade
`pip install --upgrade sable>=0.1.1`

---

The story behind this fix is written up from the upstream side in
[When `get` and `scan` disagreed about a snapshot](case-studies/snapshot-read-consistency.md),
and from the reporting side in
[sable-vector](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.md).

- Repository: <https://github.com/H-I-F/sable>
- Release: <https://github.com/H-I-F/sable/releases/tag/v0.1.1>
- Full history: [CHANGELOG.md](../CHANGELOG.md)
