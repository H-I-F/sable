[English](release-notes-v0.1.3.md) | [简体中文](release-notes-v0.1.3.zh-CN.md)

# sable v0.1.3

A maintenance release. 10 November 2026.

Nothing in the engine changes. The WAL is still the self-describing 21-byte
frame, SSTables still publish through `temp -> fsync -> rename -> fsync(parent)`,
the read path still stops *at* a tombstone, and no public API moved. A store
written by 0.1.2 opens unchanged, with nothing to migrate. There are no runtime
dependencies to update: `sable` still imports nothing outside the standard
library.

## What is in it

- **`ruff` 0.17.3 → 0.17.4 in the dev toolchain.** This is a patch release of the
  linter, covering bug fixes and formatter performance. The lint job installs
  `.[dev]`, so the green CI on this change ran with 0.17.4 rather than the old
  pin.
- **Both contributor guides now print the version that gets installed.**
  `CONTRIBUTING.md` and `CONTRIBUTING.zh-CN.md` had `0.17.3` written into the
  environment check; they now say `0.17.4`. The version in the guide and the
  version in `pyproject.toml` agree again.
- **The CHANGELOG backlog cleared.** The deterministic PR review report and the
  move to the H.I.F organization had been sitting under `[Unreleased]` since
  v0.1.2; they now sit in the version history where they belong.

## What is not in it

No engine change, no new runtime dependency, no format change and no benchmark
re-run. Every measurement quoted in `docs/` is still the one first published in
its report. The linter bump affects only contributors and CI; installing `sable`
from this tag gives the same code as 0.1.2 with a different version string.

## Upgrading

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.3"
python -c "import sable; print(sable.__version__)"   # 0.1.3
```

- Repository: <https://github.com/H-I-F/sable>
- Release: <https://github.com/H-I-F/sable/releases/tag/v0.1.3>
- Full history: [CHANGELOG.md](../CHANGELOG.md) · [简体中文](../CHANGELOG.zh-CN.md)
- Engineering notes: [index](README.md) · [简体中文](README.zh-CN.md)
