[English](release-notes-v0.1.4.md) | [简体中文](release-notes-v0.1.4.zh-CN.md)

# sable v0.1.4

A maintenance release. 11 November 2026.

Nothing in the engine changes. The WAL is still the self-describing 21-byte
frame, SSTables still publish through `temp -> fsync -> rename -> fsync(parent)`,
the read path still stops *at* a tombstone, and no public API moved. A database
written by 0.1.3 opens unchanged, with nothing to migrate. `sable` still imports
nothing outside the standard library.

## What is in it

This version records a change to how the repository looks after itself.

- **Dependency updates merge on their own.** A `workflow_run` workflow arms
  `auto-merge` on a Dependabot pull request once CI is green. Branch protection
  still decides when the merge lands: required checks, a linear history and
  resolved conversations all have to pass first, so a failing build never
  reaches `main`. It runs on the default branch, needs no extra token or
  third-party action, and only touches pull requests opened by Dependabot.
- **Dependabot scans monthly, in groups.** The pip, GitHub Actions and Docker
  ecosystems moved off the weekly schedule, and each now groups into a single
  pull request. With updates merging themselves, a weekly cadence only made more
  pull requests to close.
- **Automated security fixes are on.** Slowing routine updates down does not
  slow down a reported vulnerability, which still gets its own fix pull request.
- **The contributor guides stopped naming tool versions.** The environment check
  in `CONTRIBUTING.md` and `CONTRIBUTING.zh-CN.md` used to print a fixed `ruff`
  and `mypy` number; it now says the installed version should match `.[dev]`.
  A linter bump merged by the bot can no longer leave the guide behind.

## What gets a release from now on

Changes that do not touch the engine — dependency bumps, CI and workflow edits,
bot configuration, documentation — now land through the auto-merge path without
a manual review, and no longer get a release of their own. A tag is reserved for
a change to the core logic, or another change large enough that someone
installing the package should read about it.

This is the version that puts that rule on the record. It is the last release
made for a maintenance-only change; after it the version number moves only when
the engine does.

## What is not in it

No engine change, no new runtime dependency, no format change and no benchmark
re-run. Every measurement quoted in `docs/` is still the one first published in
its report. Installing `sable` from this tag gives the same code as 0.1.3 with a
different version string.

## Upgrading

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.4"
python -c "import sable; print(sable.__version__)"   # 0.1.4
```

- Repository: <https://github.com/H-I-F/sable>
- Release: <https://github.com/H-I-F/sable/releases/tag/v0.1.4>
- Full history: [CHANGELOG.md](../CHANGELOG.md) · [简体中文](../CHANGELOG.zh-CN.md)
- Engineering notes: [index](README.md) · [简体中文](README.zh-CN.md)
