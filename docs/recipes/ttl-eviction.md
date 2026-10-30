# Application-level TTL with key-encoded expiry

`sable` has no native time-to-live. There is no background expiry thread, no
per-key deadline the engine enforces, and no compaction filter that silently drops
old data. What it does have is the two primitives every TTL scheme is built from:

- **Ordered range scans.** `Engine.scan(start, end)` walks keys in ascending byte
  order, so a key layout can be searched by time if the time is encoded in it.
- **Tombstones.** `Engine.delete(key)` appends a deletion marker; a key is gone
  once a compaction merges that tombstone past every elder version.

This recipe layers TTL on top of those two primitives without touching the engine.
It is the pattern to reach for when you need expiry today and cannot wait for a
kernel feature — and the analysis at the end explains what it actually costs,
because "just write a tombstone" hides a real bill.

## The key schema

Two keys are written per logical record, kept in one keyspace separated by a tag
byte so a single scan can address either index:

```
data key    : <namespace> 0x00 'd' <ident:16>
expiry key  : <namespace> 0x00 'x' <expires_at:uint64 BE> <ident:16>
value (data): <expires_at:uint64 BE> <payload>
```

The expiry timestamp is **big-endian and fixed-width**. That is not cosmetic: a
range scan compares raw bytes, so `struct.pack(">Q", t)` is the only encoding for
which byte order equals numeric order across the full 64-bit range. Little-endian
would sort `256` before `1`, and a scan for "everything expired" would silently
miss most of the keyspace.

Encoding the timestamp in the expiry key is what makes eviction a *range* query
instead of a full scan: everything that expired before `now` is exactly the
half-open interval `[ns 0x00 'x' 0 … ns 0x00 'x' (now + 1))`.

## Writing with a TTL

The data key stays stable and addressable by `ident`, so a point read still works
without knowing the deadline. The expiry key carries the deadline for the sweep.

```python
import struct
import time

from sable import Engine, WriteOptions

SEP = b"\x00"
TAG_DATA = b"d"
TAG_EXPIRY = b"x"
ID_LEN = 16


def data_key(namespace: bytes, ident: bytes) -> bytes:
    if len(ident) != ID_LEN:
        raise ValueError(f"ident must be exactly {ID_LEN} bytes")
    return namespace + SEP + TAG_DATA + ident


def expiry_key(namespace: bytes, expires_at: int, ident: bytes) -> bytes:
    return namespace + SEP + TAG_EXPIRY + struct.pack(">Q", expires_at) + ident


def put_with_ttl(
    engine: Engine,
    namespace: bytes,
    ident: bytes,
    payload: bytes,
    ttl_seconds: int,
    *,
    now: int | None = None,
    sync: bool = True,
) -> int:
    """Store ``payload`` under ``ident`` and schedule it to expire after ``ttl_seconds``.

    Returns the absolute expiry as a Unix timestamp.
    """
    now = int(time.time()) if now is None else now
    expires_at = now + ttl_seconds
    value = struct.pack(">Q", expires_at) + payload
    options = WriteOptions(sync=sync)

    engine.put(data_key(namespace, ident), value, options)
    # The expiry index stores (expires_at, ident) so a stale row can be told
    # apart from a live one when the same ident is rewritten with a new TTL.
    engine.put(
        expiry_key(namespace, expires_at, ident),
        struct.pack(">Q", expires_at) + ident,
        options,
    )
    return expires_at
```

Two writes per record is the price of a time-ordered secondary index; the
alternative — scanning the whole namespace on every sweep — trades that write
amplification for an unbounded read cost that grows with table size.

## Reading defensively

Eviction is asynchronous, so a key can be past its deadline but not yet tombstoned.
The value carries the deadline precisely so a reader can reject it locally instead
of serving stale data:

```python
def get_if_live(
    engine: Engine, namespace: bytes, ident: bytes, *, now: int | None = None
) -> bytes | None:
    """Return the payload only if it has not expired; ``None`` past the deadline."""
    raw = engine.get(data_key(namespace, ident))
    if raw is None:
        return None
    expires_at = struct.unpack(">Q", raw[:8])[0]
    now = int(time.time()) if now is None else now
    if expires_at <= now:
        return None
    return raw[8:]
```

This keeps correctness independent of how promptly the sweeper runs: the sweeper
reclaims space, but the reader is the authority on whether a value is still valid.

## Evicting expired keys

The sweep is a range scan followed by batched tombstone writes. It runs in **two
phases** on purpose: collect victims while iterating, then delete after the
iterator is exhausted. Mutating the memtable mid-scan would feed the very iterator
that is producing the work.

```python
def evict_expired(
    engine: Engine,
    namespace: bytes,
    *,
    now: int | None = None,
    compact: bool = True,
) -> int:
    """Tombstone every record expired at or before ``now``; return how many rows died."""
    now = int(time.time()) if now is None else now
    start = namespace + SEP + TAG_EXPIRY + struct.pack(">Q", 0)
    end = namespace + SEP + TAG_EXPIRY + struct.pack(">Q", now + 1)

    doomed_data: list[bytes] = []
    doomed_index: list[bytes] = []
    for index_key, blob in engine.scan(start, end):
        expires_at = struct.unpack(">Q", blob[:8])[0]
        ident = blob[8:]
        doomed_index.append(index_key)
        if len(ident) != ID_LEN:
            continue  # malformed row: drop the index entry, never touch data
        data = engine.get(data_key(namespace, ident))
        # Only delete the record if its *current* deadline is the one we are
        # evicting. A rewrite with a later TTL must survive an older stale row.
        if data is not None and struct.unpack(">Q", data[:8])[0] == expires_at:
            doomed_data.append(data_key(namespace, ident))

    # Batched, unsynced deletes, then one flush: the SSTable publish fsyncs the
    # whole batch, so durability is paid once instead of once per tombstone.
    options = WriteOptions(sync=False)
    for key in doomed_data:
        engine.delete(key, options)
    for key in doomed_index:
        engine.delete(key, options)

    if doomed_index:
        engine.flush()
        if compact:
            while engine.compact():
                pass
    return len(doomed_data)
```

Run it on a timer (a cron entry, a supervisor loop, a request-triggered job — the
engine does not care). Because compaction in `sable` is explicit, the `compaction`
step in the sweep is not optional: deleting alone only *marks* data dead.

## What a tombstone actually costs

A tombstone is cheap to write and expensive to retire. Between those two moments it
occupies space, participates in reads, and can outlive several compactions. The
lifecycle of one expired record in this scheme:

| Stage | What exists on disk | What it costs |
|---|---|---|
| TTL write | data record + expiry record | 2× write, 2× WAL frame, 2× fsync (unless batched) |
| Expiry sweep | 2 tombstones appended to the WAL | 2 more frames; the originals still on disk |
| Memtable flush | 2 tombstones land in an L0 file | read path now consults them |
| Compaction L0 → L1 | tombstones merged with elder versions | write amplification: rewritten bytes include dead data |
| Compaction to bottom | tombstone meets the last elder version | **now** the bytes are physically dropped |
| Space reclaimed | record gone | the storage is finally returned |

Three consequences follow, and they are the ones that bite in production:

1. **Deletion is a write, not a subtraction.** Evicting `n` records appends `2n`
   tombstones. A sweep that clears a million keys is itself a million-record write
   burst. Batch it (`sync=False`), flush once, and schedule it off the hot path.
2. **A tombstone is only "gone" at the bottom level.** Until a compaction merges it
   past every elder version it is retained, so space is reclaimed *lazily*. A
   workload that never reaches the bottom level never reclaims the space it
   "deleted" — it just accumulates tombstones. Drive `engine.compact()` to
   exhaustion after each sweep for that reason.
3. **Re-writes need a generation guard.** Writing the same `ident` with a new TTL
   leaves the old expiry row behind. The sweep compares the current value's
   deadline against the row's before deleting, so an old row can only remove the
   data it was created for — never a newer version.

## Operational notes

- **Use a monotonic clock for deadlines, wall-clock for storage.** Deadlines stored
  as `time.time()` are correct across restarts but jump with NTP. For sub-minute
  TTLs, store a monotonic-derived deadline in the value and compare with
  `time.monotonic()` within a process, falling back to wall-clock across restarts.
- **Bound the sweep.** A single sweep over a huge expired range holds references on
  many SSTables at once. Page it with `limit=` on `scan`, or iterate in fixed time
  windows (`now - 1h … now`), so each pass releases its leases before the next.
- **Do not sweep on the write path.** The sweep writes tombstones and triggers
  compaction; running it inline with application writes stacks two write bursts
  and doubles tail latency. Run it from a dedicated maintenance loop.
- **Measure the amplification.** The [read-amplification benchmark](../../benchmarks/bench_read_amplification.py)
  and the [pre-release report](../reports/benchmark-2024.md) quantify what the
  extra keyspace and tombstones do to physical IO, so the cost is a number rather
  than an intuition.
