# Implementation Progress

Single-step file-driven state machine. Every box is ticked only after the
PR-scoped acceptance command passes locally **and** the PR is merged.

- [x] PR 01: package skeleton, domain types and the error contract
- [x] PR 02: WAL frame format, CRC32 checks and torn-tail truncation recovery
- [x] PR 03: MemTable ordering and tombstone shadowing semantics
- [x] PR 04: SSTable binary layout and atomic on-disk publish
- [x] PR 05: read path: MemTable → immutable MemTable → L0...Ln
- [x] PR 06: write path: append-before-apply and durable flush
- [x] PR 07: production CLI entry point (`sable put/get/stats`)
- [x] PR 08: SSTable Bloom filter blocks and fast pruning
- [x] PR 09: sparse-index-driven range scan and multi-level K-way merge
- [x] PR 10: compaction and physical reclamation of tombstones
- [x] PR 11: refcount-protected snapshot concurrency (`threading.Barrier`)
- [x] PR 12: SIGKILL crash-recovery matrix tests
- [x] PR 13: changelog and the crash demo (`examples/crash_demo.py`)
- [x] PR 14: production CI pipeline on GitHub Actions
- [x] PR 15: multi-stage Dockerfile and the bilingual architecture README
- [x] PR 16: benchmark suite and archived final performance numbers

## Phase 2 — Engineering Knowledge Base (PR 17 ~ PR 21)

- [x] PR 17: docs(skills): low-level engineering guides distilled from the storage engine
- [x] PR 18: docs(faq): bilingual deep-dive Q&A on storage systems
- [x] PR 19: docs(adr): architecture decision records (ADR-001 ~ ADR-005)
- [x] PR 20: bench(stress): multi-threaded concurrent reads/writes and high-contention stress tests
- [x] PR 21: docs(portal): bilingual README rework and an interactive architecture index

## Phase 3 — Pre-Release Hardening & Governance (PR 22 ~ PR 31)

> **Numbering note**: the list below uses the **planned** PR numbers (from PR 22 on);
> the actual GitHub PR numbers are shifted by one (planned PR 22 → actual #23),
> because Phase 2's PR #22 already took that number. The two map one-to-one and
> were merged in exactly the order shown here.

- [x] PR 22: feat(inspect): visual inspector for WAL binary frames → [#23]
- [x] PR 23: feat(inspect): SSTable block and Bloom filter parser → [#24]
- [x] PR 24: feat(inspect): engine file-version topology and level view → [#25]
- [x] PR 25: test(chaos): disk-full (ENOSPC) fault injection → [#26]
- [x] PR 26: test(chaos): file-descriptor leak detection and concurrency breaker tests → [#27]
- [x] PR 27: docs(recipes): user-space TTL eviction built on tombstones → [#28]
- [x] PR 28: bench(read-amp): read-amplification measurement harness → [#29]
- [x] PR 29: docs(reports): pre-release benchmark report built on real measurements → [#30]
- [x] PR 30: docs(governance): CONTRIBUTING.md → [#31]
- [x] PR 31: build(meta): a Makefile that unifies local dev and test commands → [#32]
- [x] PR 32: fix(inspect): inspector CLI now exits with code 2 on missing or corrupt files, instead of printing an empty table or a traceback → [#33]

> PR 32 was not among the original ten. It fixes a defect found while running the
> documented commands from PR 30 (the contributor guide) one by one:
> `python -m sable.sstable_inspector <missing-path>` raised a `FileNotFoundError`
> traceback, while `python -m sable.inspector <missing-path>` printed `0 frame(s)`
> and exited 0 — so a pipeline could not tell an empty file from a missing one.
> Both paths are fixed and covered by tests.

## Phase 4 — Release Prep

Winding down. Only the first item lands in the repository; the other three are
repository-side operations and never become commits.

| Item | Status |
| --- | --- |
| `docs(index)`: wire recipes and reports into four entry points in both languages; `docs(changelog)`: fold the hardening work into `[0.1.0]` and add `### Fixed` | this PR |
| Tag `v0.1.0` and create the GitHub Release — removes the CHANGELOG's dead links to a tag that did not exist yet | after merge |
| Delete 19 remote / 19 local feature branches (`git cherry` reports 0 unmerged patches) and turn on `delete_branch_on_merge` | after merge |
| Repository governance: add topics, enable branch protection on `main` (PR + green CI required) | after merge |

[#23]: https://github.com/H-I-F/sable/pull/23
[#24]: https://github.com/H-I-F/sable/pull/24
[#25]: https://github.com/H-I-F/sable/pull/25
[#26]: https://github.com/H-I-F/sable/pull/26
[#27]: https://github.com/H-I-F/sable/pull/27
[#28]: https://github.com/H-I-F/sable/pull/28
[#29]: https://github.com/H-I-F/sable/pull/29
[#30]: https://github.com/H-I-F/sable/pull/30
[#31]: https://github.com/H-I-F/sable/pull/31
[#32]: https://github.com/H-I-F/sable/pull/32
[#33]: https://github.com/H-I-F/sable/pull/33
