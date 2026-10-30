# ADR-001: WAL frame integrity uses CRC32

**Status:** Accepted — implemented in PR 02, unchanged since.

**Date:** 2026-10-24

**Deciders:** engine maintainer

---

## Context

Every WAL frame needs an integrity check. The scanner uses it to answer one
question that cannot be answered any other way: *did this frame reach stable
storage intact?* — and, downstream, to separate "never promised" (torn tail)
from "promised and lost" (corruption). See
[`../faq/interview-qa-en.md`](../faq/interview-qa-en.md) Q1 and Q2.

Constraints that bound the choice:

1. **Stdlib only.** `sable` is a pure-Python reference implementation with no
   runtime dependencies. A checksum that needs a third-party package is out.
2. **Per-write hot path.** The digest runs once per `put`/`delete`, on every
   write, before an `fsync` that measures in milliseconds. CPU is not the
   bottleneck, but a Python-level loop would become one at scale.
3. **Auditable.** The frame layout is documented byte by byte, and the digest
   algorithm must be one a reader can verify by inspection — not a construction
   whose correctness requires trusting an implementation.
4. **Threat model is media error, not adversaries.** The engine must survive a
   torn write, a bad sector, and a truncated-then-appended copy. It does not
   claim to detect deliberate tampering; no checksum does without a key.

---

## Decision

Frame header is `struct.Struct("<IQBII")` — `crc32 | sequence | op | klen | vlen`.
The CRC32 covers everything *after* itself: the remaining 17 header bytes plus
key and value. Computed with `zlib.crc32`, masked to 32 bits, stored
little-endian at offset 0.

`scan_wal_bytes` in `src/sable/wal.py` recomputes it per frame and branches on
the result.

---

## Alternatives considered

### No checksum

Rejected. Without one, a torn tail and a corrupted frame are the same event, so
recovery cannot distinguish "discard, this was never acknowledged" from "stop,
data was lost". The engine would have to pick one behaviour for both, and either
choice is wrong half the time.

### CRC64

Rejected on two independent grounds. There is no CRC64 in the standard library —
`zlib` provides `crc32` and `adler32` only — so it would be a table-driven
Python loop, and a Python-level loop over every value byte would dominate a
write path whose actual cost is a single `fsync`. And the marginal detection
improvement (2⁻⁶⁴ versus 2⁻³² against *random* multi-bit errors) does not
address the error class that actually occurs, which is burst, not random.

### SHA-256 (`hashlib`)

Rejected as over-specified for the threat model. `hashlib` is C-backed, so it is
fast enough — roughly four times the cost of `crc32` per byte, which is still
small next to an `fsync`. But it buys collision resistance, and collision
resistance defends against an adversary who can choose inputs to produce a
chosen digest. The failure being defended against here is a disk that returned
different bytes than were written. SHA-256 is not wrong, it is the wrong tool:
it costs more, it needs more code to verify, and it implies a security property
this engine deliberately does not claim.

### Per-block checksums covering several frames

Rejected. It reintroduces the framing problem this layout exists to avoid: the
checksum unit and the frame unit would differ, so "which frames are trustworthy"
would need a second mapping to answer.

---

## Rationale for CRC32 specifically

CRC32 is the cheapest algorithm that fully covers the error class that matters.
It detects:

- every burst error of 32 bits or fewer — which is the shape of a bad sector, a
  dropped DMA transfer, or a page that partially landed;
- every error involving an odd number of flipped bits;
- all but roughly 2⁻³² of arbitrary multi-bit errors.

The 2⁻³² residual deserves an honest note. At 100k frames per second, random
corruption at that rate would produce an undetected error every dozen hours or
so. But real storage failures are not random single-bit flips at that rate; they
are bursts, and bursts up to 32 bits are caught deterministically. The residual
is the right place to accept risk, and the mitigations below bound what it can
cost.

---

## Consequences

**Positive**

- One C call per frame, no Python-level loop, no dependency.
- The digest is computed in a single forward pass and can reject a frame before
  its key and value slices are allocated.
- The algorithm is verifiable by inspection and by any third-party tool; the
  format does not require trusting this implementation to audit a file.
- Placing it first means the scanner can compute the next frame's offset and
  validate it without seeking back.

**Negative**

- 2⁻³² undetected rate against adversarial or highly-random corruption.
- Not tamper-resistant. Anyone who can write the file can recompute the CRC.
- A flipped bit inside the `crc32` field itself is indistinguishable from a
  flipped bit in the payload — both present as a mismatch, which is why the
  terminal-frame case is treated as a torn tail rather than assumed malicious.

**Mitigations already in place**

- SSTable data blocks and the index carry their own CRC32, and the file footer
  re-checks the header — so corruption that survives the WAL has a second chance
  to be caught after the data is published.
- Recovery raises `ChecksumMismatchError` rather than silently dropping a frame
  that has surviving frames after it; data loss is reported, not absorbed.
- `strict_tail_crc=True` exists for auditing a file whose producer is known to
  publish only complete frames.

**Explicitly out of scope**

Authenticated or keyed checksums (HMAC). Defending against an adversary with
write access to the data directory is a different problem, and pretending a
keyless CRC addresses it would be the worse error.
