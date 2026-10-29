# sable

A crash-safe, pure-Python LSM-tree key-value storage engine reference implementation
with WAL, binary SSTables, Bloom filters, and snapshot-safe compaction.

```console
$ sable put demo-key demo-value
ok
$ sable get demo-key
demo-value
```

Design and operating notes are documented in `README.md` (English) and
`README.zh-CN.md` (简体中文); both are delivered together with the benchmarking
suite in the final iteration of the merge plan tracked in `PROGRESS.md`.
