# Security Policy

## Supported versions

sable is pre-1.0. Only the latest `0.1.x` line receives security fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

Downstream projects such as [`sable-vector`](https://github.com/H-I-F/sable-vector)
pin `sable` to a released tag; a fix ships to them as a pin bump.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via the "Report a vulnerability" option on the
[Security tab](https://github.com/H-I-F/sable/security/advisories/new).

Acknowledgement is normally within 72 hours. Include a minimal reproduction
where possible: the affected version, the sequence of operations (especially
crash or concurrent access steps), and the observed vs. expected behaviour.

## Scope

sable is an **embedded** storage engine: it runs inside the caller's process and
trusts its host. The following are out of scope unless explicitly stated:

- Authentication / authorization of clients (it has none — it is a library).
- Network transport security (the host is responsible for any RPC it builds on
  top).
- Supply-chain integrity of the build host.

In scope:

- **Durability and recovery**: write-ahead log replay, manifest integrity, and
  behaviour after an unclean shutdown or `kill -9`.
- **Snapshot isolation**: a snapshot must observe a consistent point in time for
  its whole lifetime, including across memtable and SSTable boundaries.
- **Integrity of persisted data**: corruption detection, checksum handling, and
  recovery semantics for partially written records.
- **Resource safety**: unbounded growth or exhaustion triggered by crafted input
  within the engine's own control loops.

## Verification expectations

Reports affecting durability or isolation should include a reproducer that fails
on a clean checkout — a script plus its observed output is ideal. Historical
example: a snapshot could observe a write it should not have seen, because the
memtable lookup did not honour the snapshot's pinned sequence. That was fixed in
`get_visible()` and released as `v0.1.1`; see the cross-layer write-up in
[sable-vector](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.md).
