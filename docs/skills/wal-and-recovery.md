# Write-Ahead Logging and Torn-Tail Recovery

How to build a write-ahead log whose recovery behaviour you can prove, not hope
for. Extracted from `src/sable/wal.py`, `tests/test_wal.py` and
`tests/test_crash.py`.

---

## 1. The one invariant the log exists to uphold

> A write that returned successfully must be readable after the process dies.

Everything else in this document is machinery in service of that sentence. The
machinery is `append-before-apply`:

1. allocate a monotonic sequence number,
2. serialise the mutation into a frame,
3. `write` + `flush` + `fsync` the frame,
4. *only then* make the mutation visible in the memtable.

Steps 3 and 4 are never reordered. `Engine._stage_mutation` in
`src/sable/engine.py` is the only place that admits a mutation, and it does so
holding `_lock` precisely so the order cannot be observed inverted.

The corollary is what makes recovery tractable: **the log may lose anything it
has not fsync-ed, because nothing un-fsync-ed was ever acknowledged.** A
recovery algorithm that "repairs" the tail is therefore not increasing
durability — it is inventing data nobody promised. That single shift in framing
turns a hard problem into a truncation.

---

## 2. Frame anatomy

```
+----------+----------+---------+-----------+-----------+------+-------+
| crc32    | sequence | op      | klen      | vlen      | key  | value |
| uint32   | uint64   | uint8   | uint32    | uint32    | klen | vlen  |
+----------+----------+---------+-----------+-----------+------+-------+
   4 B        8 B       1 B       4 B         4 B
```

21-byte header, `struct.Struct("<IQBII")`, little-endian, no padding.
`crc32` is computed over **everything after itself** — the remaining 17 header
bytes plus key and value.

Three properties follow from this shape, and they are the whole argument for a
binary frame:

| Property | Why it matters |
|---|---|
| **Self-describing** | From any byte offset you can compute the start of the next frame without consulting an external index. A scanner is a `while offset < total` loop. |
| **No delimiter ambiguity** | Length prefixes, not separators. A key may legally contain `0x0A`, `0x2C`, or any byte a text format would have to escape. |
| **Bounded read** | `klen`/`vlen` let the scanner decide "complete" or "partial" before it has consumed the payload. |

### Why not JSON lines

A newline-delimited JSON log is the obvious first draft. It fails on three
independent axes:

1. **Framing.** You need `\n` to be absent from your payloads. Keys and values
   in a key-value store are arbitrary bytes, so you must escape or
   base64-encode them — which means every write pays an encode and every read
   pays a decode, inside the hottest loop, to work around a bug in the framing
   choice.
2. **Partial-write detection.** `{"k": "a` is 10 bytes of syntactically
   incomplete JSON. To decide "torn" versus "corrupt" you must parse, and
   parsing failure is indistinguishable from a value that happens to be
   malformed. A length prefix answers the question in O(1) with an integer
   comparison.
3. **Cost.** Serialising a record through a general-purpose encoder allocates
   intermediate strings and dicts. `struct.pack` writes into one buffer. At the
   fsync-dominated latencies this engine runs at, CPU is not the bottleneck —
   but the *framing* properties above are unobtainable in the text format at any
   price, and the CPU saving is free once you have taken them.

The point is not "binary is faster". It is that **a self-describing fixed
header is what makes the recovery decision decidable**, and JSON does not give
you one.

### Why CRC32 sits at the front

Because it covers the bytes that follow, a reader can compute the digest in one
pass while streaming, without seeking back. Placing it first also means a
scanner that has validated frame *i* knows exactly where frame *i+1* begins
*and* can reject it before allocating the key/value slices.

---

## 3. The recovery decision table

`scan_wal_bytes(data)` in `src/sable/wal.py` is a pure function:
`bytes -> RecoveryResult`. No file handles, no clocks, no threads. That purity
is deliberate and is the reason the recovery matrix is exhaustively testable
(see §5).

| Observation at offset `o` | Classification | Action |
|---|---|---|
| `remaining < FRAME_HEADER_SIZE` | Torn tail | Stop. Discard `remaining` bytes. |
| `klen > MAX_KEY_BYTES` or `vlen > MAX_VALUE_BYTES` | Structural corruption | Raise `CorruptWALError` |
| Unknown `op` byte | Structural corruption | Raise `CorruptWALError` |
| `remaining < header + klen + vlen` | Torn tail | Stop. Discard `remaining` bytes. |
| CRC mismatch, **not** the last frame | Real corruption | Raise `ChecksumMismatchError` |
| CRC mismatch, **is** the last frame | Torn tail (default) | Stop. Discard. Raise instead under `strict_tail_crc=True`. |

### Why non-terminal CRC failure must be fatal

If a frame fails its checksum and *more bytes follow it*, then some later write
was fsync-ed after it. The log is append-only and the filesystem does not
reorder a completed `fsync`, so a valid frame after a broken one cannot be
explained by a crash — it means the bytes were mutated after they landed. Bit
rot, a bad disk block, someone editing the file, a copy that truncated in the
middle and was then appended to.

That is a genuinely different situation, and the two must not share a recovery
path:

- Torn tail → the data was never promised. Discarding is **correct**.
- Mid-stream corruption → the data *was* promised and has been lost. Discarding
  silently would under-report data loss. Guessing would be worse.

So the engine refuses to guess and raises. Refusing loudly is the only honest
response to "I cannot reconstruct what you gave me".

### Why the terminal frame gets the benefit of the doubt

A partially written frame whose length fields landed but whose payload did not
is byte-for-byte indistinguishable from a complete frame with a flipped bit.
There is no test that separates them. So the default answers the question that
matters operationally: *is this file safe to replay?* Yes — everything before
the tail verified. `strict_tail_crc=True` exists for auditing, where you would
rather know that the tail is suspicious than quietly drop it.

---

## 4. Idempotent repair

```python
result = WriteAheadLog.recover(path)          # pure scan, no mutation
if result.torn_tail:
    removed = WriteAheadLog.truncate_torn_tail(path, result.valid_bytes)
```

Repair is idempotent because `valid_bytes` is a *pure function of the byte
image*: scanning the same bytes twice yields the same offset, and truncating to
an offset you already satisfy is a no-op (the guard
`if valid_bytes >= size: return 0`).

This matters more than it looks. Recovery runs at `Engine.open`, and `open` can
be called on a database that was already recovered, or on one that crashed
*during* a previous recovery. If recovery were stateful — "repair advances the
cursor" — a crash inside recovery could double-apply or skip. Making it a pure
function with an idempotent commit means recovery is safe to run any number of
times, which is the only way to be sure it is safe to run once.

`truncate_torn_tail` fsyncs after truncating. Truncation is a metadata change
too; an un-fsync-ed truncate can be undone by a power loss, resurrecting the
garbage on the next open.

---

## 5. Designing the state machine so it can be tested

The entire recovery policy lives in one pure function. Everything else is I/O
around it. That split buys three different test shapes, and you want all three.

### 5.1 Byte-image tests (fast, exhaustive, deterministic)

Construct the image directly. No filesystem, no subprocess, sub-millisecond:

```python
def build_image(count: int) -> bytes:
    return b"".join(
        encode_frame(InternalEntry.put(b"k-%05d" % i, i, b"v-%05d" % i))
        for i in range(count)
    )

def test_recovery_returns_three_records_when_fourth_is_truncated() -> None:
    image = build_image(4)
    # Cut the fourth frame in half: a power loss mid-frame.
    cut = len(image) - 8
    result = scan_wal_bytes(image[:cut])
    assert result.frame_count == 3
    assert result.torn_tail is True
    assert result.discarded_bytes == len(image[:cut]) - result.valid_bytes
```

Because the input is bytes, you can cut at *every* offset cheaply. Sweeping
`cut` over `range(len(image))` and asserting the invariants hold everywhere is
a stronger statement than any number of crash runs, and it takes milliseconds.

### 5.2 Fault injection with `monkeypatch` (real I/O, forced branch)

Byte images verify the scanner. They do not verify that `append` really writes
what the scanner expects. Patch the writer to stop halfway:

```python
def test_power_cut_mid_frame_leaves_a_torn_tail(tmp_path, monkeypatch) -> None:
    real = WriteAheadLog.append

    def half_write(self, entry, sync=None):
        frame = encode_frame(entry)
        self._handle.write(frame[: len(frame) // 2])
        self._handle.flush()
        os.fsync(self._handle.fileno())      # the half frame IS durable
        return len(frame) // 2

    monkeypatch.setattr(WriteAheadLog, "append", half_write)
    log = WriteAheadLog.open(tmp_path / "wal.log")
    log.append(InternalEntry.put(b"a", 0, b"1"))
    log.append(InternalEntry.put(b"b", 1, b"2"))
    log.close()
    monkeypatch.undo()

    result = WriteAheadLog.recover(tmp_path / "wal.log")
    assert [e.user_key for e in result.entries] == [b"a"]
    assert result.torn_tail
```

Two rules for injection tests:

- **Inject at the seam you actually distrust.** Patching `os.fsync` to a no-op
  tests "does the code call fsync", which is a coverage question. Patching the
  write to be partial tests "does recovery classify correctly", which is a
  correctness question.
- **Assert on the classification, not just the count.** `frame_count == 1`
  passes for the wrong reason if corruption is silently swallowed. Assert
  `torn_tail`, `discarded_bytes`, and `valid_bytes` separately.

### 5.3 Process-level `SIGKILL` (the only test that cannot lie)

Unit tests share the interpreter's assumptions with the code under test. A
`SIGKILL` cannot:

```python
def kill_after_acks(home, acks, options):
    process = subprocess.Popen([sys.executable, "-c", CHILD_SOURCE, str(home), ...])
    acknowledged = []
    for line in process.stdout:            # child prints only after put() returned
        acknowledged.append(line.strip().encode())
        if len(acknowledged) == acks:
            process.send_signal(signal.SIGKILL)
            break
    process.wait()
    return acknowledged
```

The child prints an id *after* `put()` returns. The parent kills it the instant
it sees the *n*-th line. `SIGKILL` is uncatchable, so there is no shutdown hook,
no `finally`, no flush-on-exit — the closest thing to pulling the plug that a
process can experience.

The parent's assertion is then exact rather than statistical: **every id the
child acknowledged must be readable after reopen.** Parametrise over
`acks in range(1, 11)` and you have ten different crash points covering
"crashed before any flush", "crashed mid-flush", "crashed after manifest
publish", and everything between.

`tests/test_crash.py` does exactly this, and additionally asserts the absence of
negative evidence: no `.tmp-sable` sibling survives, no SSTable fails its
footer CRC, sequence allocation resumes above the highest replayed frame.

---

## 6. Checklist for a new WAL

- [ ] Fixed-size, self-describing header; length prefixes, no delimiters.
- [ ] Checksum covers the frame *after* the checksum field, so streaming works.
- [ ] Frames appended only, never rewritten in place.
- [ ] `fsync` before the caller is told the write succeeded; `fsync` the
      directory after creating the segment.
- [ ] Recovery is a pure function of the byte image.
- [ ] Repair is idempotent and fsyncs the truncation.
- [ ] Torn tail (never promised) and mid-stream corruption (promised and lost)
      are different errors with different outcomes.
- [ ] Three test layers: byte images, injected partial writes, `SIGKILL`.
- [ ] Log rotation happens only after the memtable it covered is durably
      published, never before.

---

## 7. Where to read next

| Concern | Source |
|---|---|
| Frame encode / scan / repair | `src/sable/wal.py` |
| Scanner unit tests, byte-image cuts | `tests/test_wal.py` |
| `SIGKILL` matrix | `tests/test_crash.py` |
| Append-before-apply call site | `Engine._stage_mutation` in `src/sable/engine.py` |
| Atomic publish of SSTables and manifests | `docs/skills/atomic-flush.md` |
