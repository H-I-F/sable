[English](RELEASE_NOTES_v0.1.2.md) | [简体中文](RELEASE_NOTES_v0.1.2.zh-CN.md)

# sable v0.1.2

A documentation release. 3 November 2026.

Nothing in the engine changes here. The WAL is still the self-describing 21-byte
frame, SSTables still publish through `temp -> fsync -> rename -> fsync(parent)`,
the read path still stops *at* a tombstone, and no public API moved. A store
written by 0.1.1 opens unchanged under 0.1.2, with nothing to migrate. The version
number moves because the documentation work done since 0.1.1 deserves a tag of its
own, not because there is a new behaviour to install.

## What is in it

The engineering notes under `docs/` are now a complete bilingual set:

- **An index for both languages.** `docs/README.md` and `docs/README.zh-CN.md`
  map every page, with a source map linking concepts back to modules and a reading
  order that starts at the root README and ends at the benchmark report.
- **Chinese editions of the long-form pages.** The five ADRs, the TTL eviction
  recipe, the 2024 benchmark report and the three `docs/skills/` guides each have
  an `.md` and a `.zh-CN.md` now, as do `CHANGELOG`, `CONTRIBUTING` and `SECURITY`.
- **Release notes and a case study.** `docs/RELEASE_NOTES_v0.1.0.md`,
  `docs/RELEASE_NOTES_v0.1.1.md` and
  `docs/case-studies/snapshot-read-consistency.md` in both languages. The case
  study tells the v0.1.1 snapshot-read fix from this side and links to the
  downstream account in sable-vector.
- **A clean split of the evolution ledger.** `PROGRESS.md` had English headings
  wrapped around Chinese entries; it is now English-only, with `PROGRESS.zh-CN.md`
  carrying the Chinese. Both keep the same 32 PR entries and 11 link definitions.
- **README wiring.** A documentation table, a governance list pairing English and
  简体中文 for each entry, and a link to the downstream case study.

`docs/RELEASE_NOTES_v0.1.1.md` also had its title printed twice; the duplicate is
gone.

## What is not in it

No benchmark was re-run and no figure was recomputed. Every measurement quoted in
`docs/` is the one first published in its report. The `[Unreleased]` section of the
CHANGELOG still carries the deterministic PR review report and the move to the
H.I.F organization — tooling and governance items on their own timeline, left out
of a tag whose subject is documentation.

## Upgrading

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.2"
python -c "import sable; print(sable.__version__)"   # 0.1.2
```

- Repository: <https://github.com/H-I-F/sable>
- Release: <https://github.com/H-I-F/sable/releases/tag/v0.1.2>
- Full history: [CHANGELOG.md](../CHANGELOG.md) · [简体中文](../CHANGELOG.zh-CN.md)
- Engineering notes: [index](README.md) · [简体中文](README.zh-CN.md)
