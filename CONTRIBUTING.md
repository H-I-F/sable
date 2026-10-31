# Contributing to sable

`sable` is a reference implementation of an LSM-tree storage engine: pure Python,
standard library only, no runtime dependencies. That constraint is the point — the
code is meant to be read as much as run — so the bar for a patch is "would this
teach someone how the thing actually works", not "does this add a feature".

This document covers what you need to get a change merged. It is deliberately
specific: every command here is one CI runs, and every rule here is one a reviewer
will hold you to.

## Contents

1. [Set up](#1-set-up)
2. [The four gates](#2-the-four-gates)
3. [Code conventions](#3-code-conventions)
4. [Tests](#4-tests)
5. [Commit messages](#5-commit-messages)
6. [Pull requests](#6-pull-requests)

## 1. Set up

Python 3.10 or newer. Nothing else is required at runtime.

```bash
git clone https://github.com/H-I-F/sable.git
cd sable
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

`.[dev]` installs the package in editable mode plus the pinned toolchain: `ruff`,
`mypy`, `pytest` and `pytest-cov`. Use it rather than installing those separately —
CI resolves the same extras, so a version that works on your machine works in CI.

Confirm the environment before you write code:

```bash
ruff --version                     # 0.17.3
mypy --version                     # 2.4.1
pytest -q                          # the suite is green before you touch it
```

If the suite is not green on a clean checkout, that is a bug worth reporting before
it becomes a bug you are blamed for.

`.[test]` exists for consumers who only want to run the suite: it installs `pytest`
and `pytest-cov` without the linters. CI's test job uses it, which is why the tests
themselves must never import `ruff` or `mypy`.

## 2. The four gates

CI runs four jobs on every push to `main` and every pull request. They are the same
commands you can run locally, in the same order, and all four must pass.

| Gate | Command | What it protects |
|---|---|---|
| Lint | `ruff check src tests examples benchmarks` | Formatting, import order, unused names, and the security rules that catch `assert` in production code |
| Types | `mypy src` | The public surface: every function has annotations and they hold |
| Tests | `pytest -q --cov=sable --cov-fail-under=85 tests/` | Behaviour, plus the coverage floor |
| Examples | `python examples/basic.py && python examples/crash_demo.py` | That the documentation still runs |

Two details about these commands are deliberate:

- **Lint covers `examples` and `benchmarks`, not just `src`.** Those trees are the
  documentation, and documentation that does not lint rots faster than the code it
  describes. `ruff check .` is equivalent and is fine to run locally.
- **`mypy` covers `src` only.** Tests are checked for correctness by running them,
  and benchmarks are checked by running them; demanding types from both would cost
  more than it finds. Adding annotations anyway is welcome.

The coverage floor is **85%**, enforced by `--cov-fail-under`. The suite currently
sits at **94.1%** of statements, so a patch has room to move the number without
being blocked — but not much. A change that adds a branch nothing exercises will be
asked to either exercise it or exclude it with a comment explaining why it cannot
be reached.

Run all four the way CI does:

```bash
ruff check src tests examples benchmarks
mypy src
pytest -q --cov=sable --cov-fail-under=85 tests/
python examples/basic.py && python examples/crash_demo.py
```

## 3. Code conventions

Everything below is enforced by a tool except where marked otherwise.

- **Line length 100, target Python 3.10.** Both come from `pyproject.toml`; do not
  reformat around them by hand, run `ruff check --fix`.
- **Every function is annotated.** `disallow_untyped_defs` is on. `Any` in a public
  signature needs a reason in the docstring.
- **No third-party imports.** The engine imports the standard library and nothing
  else. A patch that needs a dependency needs to explain why the dependency is
  worth more than the readability it costs.
- **Docstrings say why, not what.** `"""Freeze the active memtable and publish it
  as a new L0 table."""` earns its line; a docstring that restates the signature
  does not. The non-obvious rationale belongs in the docstring, and the long-form
  version belongs in `docs/decisions/` as an ADR.
- **No `TODO`, `FIXME` or placeholder code.** If something is not finished, it does
  not go in. If it is finished but crude, say so in the PR description where a
  reviewer can weigh it.
- **Errors come from the hierarchy in `sable.errors`.** `SableError` for anything a
  caller might reasonably handle; `CorruptionError` and its subclasses for damage
  on disk. Never raise bare `Exception`, and never swallow one silently.
- **Durability is not negotiable.** Anything on the write path must keep the
  invariant documented in ADR-004: an acknowledged write survives a crash. A patch
  that trades that away for throughput belongs behind an explicit option, and the
  option needs an ADR.

## 4. Tests

Tests live in `tests/`, mirror the module they cover, and are the part of a patch a
reviewer reads first.

**What makes a test worth adding:**

- It fails on the code before your change and passes after. If it passes both ways,
  it is documentation, and it belongs in `examples/`.
- It asserts the *invariant*, not the current output. `assert engine.get(k) == v` is
  a fact; `assert engine.level_files[0] == []` after a failed flush is a property.
- It survives re-running. Anything seeded uses a fixed seed; anything measuring time
  asserts a bound, not a value.

**The crash matrix is the reason this project exists.** If you touch the WAL, the
flush path or compaction, add a case that fails at the point you changed. `tests/`
already contains the injection helpers to cut a write short mid-frame, corrupt a
block CRC, exhaust disk space and leak file descriptors — use them rather than
building new ones (see `tests/test_chaos_enospc.py` for the pattern: patch the real
syscall the way the filesystem fails, not the way the code calls it).

**Coverage is a floor, not a goal.** 100% of the lines in a function that is never
called correctly under concurrency is worth less than 90% with a chaos test that
interleaves a compaction.

## 5. Commit messages

[Conventional Commits](https://www.conventionalcommits.org/), with the type carrying
the intent and the scope naming the module:

```
feat(sstable): add Bloom filter to the table footer
fix(engine): keep the WAL position stable across a failed flush
perf(scan): reuse the merged iterator between adjacent blocks
test(chaos): inject ENOSPC at fsync rather than at write
docs(recipes): document TTL eviction on top of tombstones
bench(read-amp): measure physical block reads per logical read
build(meta): add a Makefile for the local gates
ci(workflow): run the examples as part of the test job
chore(progress): tick PR 22
```

| Type | Use for |
|---|---|
| `feat` | A capability the public API did not have |
| `fix` | Behaviour that was wrong, including a crash or a corruption |
| `perf` | A change whose only justification is a measured number — quote it |
| `test` | Tests only, no production change |
| `docs` | Prose, ADRs, recipes, README |
| `bench` | Measurement harnesses and their reports |
| `build` | Tooling that is not CI: Makefile, packaging |
| `ci` | Workflows |
| `chore` | Bookkeeping with no effect on the artefact |

Rules the reviewer will check:

- **Subject in the imperative, lowercase, no trailing period, ≤ 72 characters.**
  Git's own output starts with the subject, so `add` reads as "this commit adds".
- **The body says why, not what.** The diff already says what. If the body would
  fit in the subject, the change was too small to need a body.
- **One concern per commit.** A commit that both fixes a bug and reformats a file
  cannot be reverted cleanly, which is the only test of a commit that matters.

## 6. Pull requests

### Before you open one

1. All four gates pass locally, in the order listed in §2.
2. Your branch is rebased on current `main`, not merged with it.
3. Every commit is self-contained: the tree builds and tests green at that commit,
   not only at the tip.

### Branch names

`<type>/<short-topic>` — `feat/bloom-filter`, `fix/wal-torn-tail`,
`docs/recipes-ttl`, `bench/read-amplification`. Lowercase, hyphenated, no PR numbers
in the name: the number changes when the PR is renumbered, the name does not.

### What a reviewer looks for

| Question | Why it decides the review |
|---|---|
| Does the change keep the durability invariant? | Everything else is negotiable; this is not |
| Is there a test that fails without the patch? | A bug fix without a failing test is a guess |
| Does it add a dependency? | The standard library constraint is a feature |
| Is the docstring explaining *why*? | The code is documentation |
| Does the commit stand alone? | History is read far more often than it is written |

### How merges happen

Maintainers merge with **rebase**, so `main` stays linear and every commit that lands
is a commit that was reviewed. Two consequences:

- **Force-push your branch freely while it is open.** Since the history is rewritten
  on merge, there is no cost to cleaning it up beforehand, and there is a real cost
  to landing four commits that say "wip", "fix typo", "actually fix typo".
- **Do not merge `main` into your branch.** Rebase onto it. A merge commit inside a
  PR survives into `main` as noise when the PR is rebased.

CI must be green before a merge, without exception. A red build that "is unrelated
to this change" is still a red build; if `main` is broken, fix that first in its own
PR, because a reviewer cannot tell your breakage from the pre-existing one.

### Reporting a bug instead

Open an issue with the version, the smallest script that reproduces it, and what you
expected. For anything that loses or corrupts data, say so in the first line — those
issues skip the queue. A reproduction that includes the WAL or SSTable left behind by
the failure is worth ten that describe it, and the inspectors exist for exactly this:

```bash
python -m sable.inspector path/to/wal-000001.log      # frame-by-frame WAL audit
python -m sable.sstable_inspector path/to/000001.sst  # header, index, Bloom geometry
python -m sable.topology path/to/db                   # which file holds what
```

Each one reads a file the engine wrote and prints what it found; none of them need
the engine to be open, which is what makes them usable against the wreckage of a
failed run.
