# Atomic Publish: temp → fsync → rename → fsync(parent)

The four-step sequence every durable file replacement in `sable` goes through,
why each step exists, what breaks when you skip it, and how the same code
behaves on platforms that cannot honour it.

Reference implementation: `src/sable/fsutil.py`.

---

## 1. The sequence

```python
tmp_path = target.with_name(target.name + ".tmp-sable")
fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "wb") as handle:
    handle.write(payload)
    handle.flush()          # Python buffer  -> OS page cache
    os.fsync(handle.fileno())   # page cache  -> stable storage
os.replace(tmp_path, target)    # atomic swap
fsync_directory(target.parent)  # the directory ENTRY -> stable storage
```

Four distinct things are being persisted, and each step owns exactly one:

| Step | Persists | Skipping it costs you |
|---|---|---|
| temp file, sibling of target | — | Cross-device `rename` fails with `EXDEV` if the temp lives elsewhere (e.g. `/tmp`). A sibling in the same directory is the only way to guarantee the rename is a directory-entry swap and not a copy. |
| `flush` | process buffer → kernel | Nothing on this machine; everything if the process dies, because the bytes never left user space. |
| `fsync(file)` | file **contents** | A crash leaves a zero-length or partially-filled file at the final path. Contents are gone; the name survives. |
| `os.replace` | — | Writing in place means a reader can observe a half-written file. `replace` is atomic, so a reader sees old or new, never a blend. |
| `fsync(parent)` | the **directory entry** | The classic silent failure: data is perfectly durable, but the rename is not, so after reboot the file is missing or still has its old contents. |

### `os.replace` rather than `os.rename`

`os.rename` on Windows fails when the destination exists. `os.replace`
overwrites atomically on both POSIX and Windows. Since every publish here is a
replacement (manifest, SSTable, CURRENT), `replace` is the correct primitive on
both platforms and removes a per-OS branch.

### Why the whole file is assembled before publishing

`SSTableWriter.finish()` builds the complete image — header, data blocks,
sparse index, Bloom block, footer — in memory and then hands it to
`atomic_write_bytes` once. There is no incremental append to the final path.
The consequence is a strong one, and worth stating explicitly:

> An SSTable is either complete or absent. There is no third state, so no
> reader ever needs logic for "file exists but is truncated".

That is why `tests/test_crash.py::test_crash_never_publishes_a_partial_sstable`
can make an absolute assertion instead of a probabilistic one.

---

## 2. The failure you get from skipping the directory fsync

This is the step almost every hand-rolled implementation omits, and the one
whose absence is hardest to notice, because it fails only under power loss
(process-kill tests will never show it).

A rename is two logical writes: the file's blocks, and the directory's entry
mapping `000004.sst` → inode. `fsync(file)` covers the first. Without
`fsync(dir)`, the second may still be in the page cache when the machine loses
power. On reboot:

- the directory entry may still point at the **old** inode (your new SSTable
  vanished, and the engine's manifest references a file whose contents are
  stale), or
- the entry may be missing entirely, even though the blocks are sitting
  somewhere on disk with a perfectly valid footer.

Both are "the data was durable but the name was not". In `sable` the manifest is
itself published through the same path, so a lost directory entry means either
a manifest that describes files that do not exist or files that exist but are
unreferenced — and the recovery code has no way to tell which of the two
happened.

The same argument applies to `unlink`. Deleting a retired SSTable is also a
directory mutation, which is why `Engine._discard_retired_files` batches
deletions and then calls `_seal_home_directory()` once for the batch rather than
per file.

---

## 3. Platform behaviour and graceful degradation

Directory `fsync` is not universally available. `src/sable/fsutil.py` handles
this in two places, and the important design decision is *how* it degrades.

```python
flags = getattr(os, "O_DIRECTORY", os.O_RDONLY)
try:
    fd = os.open(os.fspath(path), flags)
except OSError:
    return
try:
    os.fsync(fd)
except OSError:
    return
finally:
    os.close(fd)
```

### What actually happens on Windows / NTFS

`os.open()` on a directory is not a supported operation there. Underneath,
CPython calls `CreateFile`, and opening a directory handle requires
`FILE_FLAG_BACKUP_SEMANTICS`, which the `os.open` path does not pass. The call
fails with `ERROR_ACCESS_DENIED`, which CPython surfaces as an `OSError` —
`PermissionError`, errno 13 (`EACCES`), on stock builds. Through a
POSIX-emulation layer (MSYS2, Cygwin, some WSL filesystem mounts) the same
attempt is reported as errno 22 (`EINVAL`, "Invalid argument"). You will also
see `EINVAL` from network and virtual filesystems that reject directory handles
outright, and from container runtimes whose volume driver does not implement
`fsync` on a directory fd.

**Practical rule: catch `OSError`, never one specific errno.** Any code that
special-cases `errno.EINVAL` will work on one platform and silently disable
durability on another, or vice versa. `sable` uses `getattr(os, "O_DIRECTORY",
os.O_RDONLY)` for the same reason — `O_DIRECTORY` is not defined on Windows, so
the flag is probed rather than assumed.

### The degradation is documented, not hidden

Silently returning turns a hard guarantee into a hope. The correct engineering
response is not to pretend: it is to state the envelope where the reader will
find it. `README.md` and `README.zh-CN.md` both record that directory-entry
durability degrades to whatever the platform guarantees where a directory handle
cannot be obtained, and that the full four-step invariant holds on POSIX
filesystems.

What that means concretely on Windows:

- **File contents remain durable.** `fsync` on a regular file handle works.
- **The atomic swap still happens.** `os.replace` is atomic on NTFS.
- **The directory entry itself is not explicitly flushed.** NTFS journals
  metadata, and in practice the rename survives power loss, but that is the
  filesystem's guarantee rather than this code's. Recovery is still correct
  because the manifest is validated (magic + CRC) and any file it names but
  cannot open is treated as a hard error rather than silently skipped.

### Consequences for tests

A crash test that asserts "no partial file survived" is valid on every platform,
because it depends on `replace` rather than on directory fsync. A test that
asserted "the rename is on disk" would be asserting something the platform may
not offer. Only assert what the invariant actually provides on the platform
under test — this is why `tests/test_crash.py` inspects footers and sequence
continuity rather than directory state.

---

## 4. Failure cleanup

```python
try:
    ...
    os.replace(tmp_path, target)
except OSError:
    tmp_path.unlink(missing_ok=True)
    raise
```

If anything before the rename fails, the sibling temp file is removed and the
`OSError` propagates. There is no path where a failed publish leaves a
half-written artifact at the final name, and none where it leaves a
`.tmp-sable` file behind. `tests/test_sstable.py` asserts exactly this by
injecting an I/O failure and checking the directory afterwards.

Note the ordering: the `except` clause unlinks before re-raising, and the
directory fsync happens *after* the `try` block. A failed publish has nothing to
seal, so it must not call `fsync_directory`.

---

## 5. Checklist

- [ ] Temp file is a **sibling** of the target, in the same directory.
- [ ] `flush` then `fsync` on the file descriptor, before the rename.
- [ ] `os.replace`, not `os.rename`, not in-place write.
- [ ] `fsync` the parent directory after every create/rename/unlink that must
      survive.
- [ ] Batch deletions, then seal the directory once.
- [ ] Catch `OSError` around directory fsync, never a specific errno; probe for
      `O_DIRECTORY`.
- [ ] Document the degraded guarantee instead of implying the full one.
- [ ] Remove the temp file on failure, then re-raise.
- [ ] Never let a reader see a third state: complete or absent, nothing else.

---

## 6. Where to read next

| Concern | Source |
|---|---|
| The four-step primitive | `src/sable/fsutil.py` |
| Whole-image publish of tables | `SSTableWriter.finish` in `src/sable/sstable.py` |
| Manifest + CURRENT handling | `Engine._publish_manifest` in `src/sable/engine.py` |
| Injected I/O failure tests | `tests/test_sstable.py` |
| Crash assertions across ten kill points | `tests/test_crash.py` |
| Log-level durability | `docs/skills/wal-and-recovery.md` |
