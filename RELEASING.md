# Releasing

This is the working rule for what gets a release, and it is a live document. If
it disagrees with `.github/scripts/change_policy.py`, the script wins: CI and
the auto-merge workflow run that, not this page.

## The one rule

A version number moves only when the engine changes. Dependency bumps, CI and
workflow edits, bot configuration and documentation merge on their own and get
no tag of their own.

The classification is not a judgement call made at review time. It is computed
from the changed files by `.github/scripts/change_policy.py`, and every consumer
imports the same verdict:

| Tier | What triggers it | Human review | Auto-merge | Release |
| --- | --- | --- | --- | --- |
| `core` | anything under `src/`, or a runtime dependency in `pyproject.toml` | required | no | yes |
| `review` | a base image or action reference changes (`FROM python:3.11` -> `3.15`, `@v4` -> `@v7`) | required | no | no, unless it also touches `src/` |
| `routine` | docs, CI, workflow, bot config, dev-tool pins, version metadata | not required | yes | no |

An empty or malformed file list is an error, never a routine change.
`change_policy.py --self-test` and `release_tool.py --self-test` both prove their
detectors still fire. `policy-check.yml` runs them plus `release_tool.py check`
on every push and pull request, and that job is a required status check.

## Cutting a release

Run from a clean checkout of `main`:

```sh
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
BASE=$(gh api "repos/$REPO/releases/latest" --jq .tag_name)

# 1. Is a release actually due? Reads the compare payload from stdin.
gh api "repos/$REPO/compare/$BASE...main" | python3 .github/scripts/release_tool.py eligible

# 2. Mechanical edits only: version sources, bilingual changelogs, links.
python3 .github/scripts/release_tool.py prepare 0.1.5 2026-12-01

# 3. Write the two announcements and update the four indexes; check refuses
#    a tree where any of them disagree.
python3 .github/scripts/release_tool.py check
```

Then open a PR with an English message, let CI finish, squash-merge, and create
the tag and Release. The prose stays human on purpose: announcements and index
wording are written for readers, not filled in by a tool.

`prepare` deliberately does only what can be proven with a hit assertion. It
will not invent announcement text, and `check` will refuse a tree whose version
sources, bilingual changelogs, compare/tag links, announcements and index
entries do not all point at the same version.

## Routine changes

Everything that is not `core` lands without a maintainer click when it comes
from Dependabot: the auto-merge workflow asks `change_policy.py` for a verdict
and arms GitHub auto-merge only for `routine`. Branch protection still has the
final say. A `review` tier stays open for a person to read.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the reviewer's side and
[CHANGELOG.md](CHANGELOG.md) for the history of what actually shipped.
