#!/usr/bin/env python3
"""The single definition of what a change means for merging and releasing.

Three consumers import :func:`classify` -- the auto-merge workflow, the PR
review report and the release tool. A rule changed here changes it everywhere;
nothing else in the repository is allowed to guess a verdict on its own.

The three tiers:

``core``     engine/library code or a runtime dependency changed. This is the
             only tier that earns a release, and it never auto-merges.
``review``   a base image or action reference changed, or another wide version
             jump. Never auto-merges, does not by itself earn a release.
``routine``  docs, CI, bot config, dev-tool pins, version metadata. May
             auto-merge and never gets a tag of its own.

Standard library only: no network, no writes, no dependency on GitHub. Input is
the file list a pull request or compare returns (``filename`` plus optional
``patch``). ``--self-test`` builds known changes and asserts every tier fires,
so a classifier that has stopped working can never be mistaken for "all
routine". An empty file list is an error, not a routine change.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass

TIER_CORE = "core"
TIER_REVIEW = "review"
TIER_ROUTINE = "routine"

# Engine and library code. Nothing else in the tree earns a release on its own.
CORE_PREFIX = "src/"

# A pyproject.toml line that changes a *runtime* dependency. Dev/lint pins and
# the version field are deliberately absent: those are routine metadata.
RUNTIME_DEP = re.compile(
    r"^[+-]\s*(dependencies\s*=|requires-python\s*=|numpy|typer|mcp|sable\s*@)",
    re.IGNORECASE | re.MULTILINE,
)

# Environment pins. The whole tag is compared, not just the leading number:
# python:3.11-slim -> 3.15-slim keeps major "3" but changes the runtime, and
# actions/checkout@v4 -> @v7 is a different ref. Either leaves a person in charge.
FROM_TAG = re.compile(r"^([+-])\s*FROM\s+\S+:(\S+)", re.MULTILINE)
USES_TAG = re.compile(r"^([+-])\s*-?\s*uses:\s+\S+@(\S+)", re.MULTILINE)
DIFF_HEADER = ("---", "+++")


@dataclass(frozen=True)
class Verdict:
    tier: str
    release: bool
    auto_merge: bool
    reasons: tuple[str, ...]


def _require(cond: bool, msg: str) -> None:
    """Fail loudly; never rely on ``assert`` (disabled under ``python -O``)."""
    if not cond:
        raise RuntimeError(msg)


def _core_hits(files: list[dict]) -> list[str]:
    hits = [f["filename"] for f in files if f.get("filename", "").startswith(CORE_PREFIX)]
    for f in files:
        if f.get("filename") == "pyproject.toml" and RUNTIME_DEP.search(f.get("patch") or ""):
            hits.append("pyproject.toml: runtime dependency")
    return hits


def _env_pin_changes(files: list[dict]) -> list[str]:
    changes = []
    for f in files:
        before: set[str] = set()
        after: set[str] = set()
        for line in (f.get("patch") or "").splitlines():
            if line.startswith(DIFF_HEADER):
                continue
            for pattern in (FROM_TAG, USES_TAG):
                m = pattern.match(line)
                if m:
                    bucket = before if m.group(1) == "-" else after
                    bucket.add(m.group(2))
        # Only a real substitution counts. Adding a new ``uses:`` line (or
        # deleting one) leaves one side empty -- that is a workflow edit, not
        # a version jump, and must stay routine.
        if before and after and before != after:
            changes.append(
                f'{f.get("filename")}: pin {sorted(before)} -> {sorted(after)}'
            )
    return changes


def classify(files: list[dict]) -> Verdict:
    """Return the policy for one change. ``files`` must not be empty."""
    if not files:
        # A broken API call or a missing --files argument must fail loudly.
        # Silently returning "routine" here would auto-merge an unreviewed PR.
        raise ValueError("empty file list: classifier input is broken, not a routine change")

    core = _core_hits(files)
    if core:
        return Verdict(
            tier=TIER_CORE,
            release=True,
            auto_merge=False,
            reasons=tuple(f"core: {p}" for p in sorted(core)),
        )

    jumps = _env_pin_changes(files)
    if jumps:
        return Verdict(
            tier=TIER_REVIEW,
            release=False,
            auto_merge=False,
            reasons=tuple(f"environment pin changed: {p}" for p in sorted(jumps)),
        )

    return Verdict(
        tier=TIER_ROUTINE,
        release=False,
        auto_merge=True,
        reasons=("no engine code, runtime dependency or environment pin touched",),
    )


def load_files(raw: str) -> list[dict]:
    """Parse one or more concatenated JSON arrays (``gh api --paginate`` output).

    A paginated GitHub API call prints ``[...]\\n[...]`` rather than a single
    array. Parsing only the first page would silently under-report the change,
    which is exactly the kind of silent failure this classifier must not have.
    """
    decoder = json.JSONDecoder()
    out: list[dict] = []
    pos = 0
    while True:
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos >= len(raw):
            break
        value, pos = decoder.raw_decode(raw, pos)
        if not isinstance(value, list):
            raise ValueError("expected a JSON array of file objects")
        out.extend(value)
    return out


def self_test() -> None:
    """Prove every tier fires on known changes before trusting the classifier."""
    cases: list[tuple[list[dict], str]] = [
        ([{"filename": "src/sable/engine.py"}], TIER_CORE),
        (
            [{"filename": "pyproject.toml", "patch": '-dependencies = []\n+dependencies = ["x"]'}],
            TIER_CORE,
        ),
        (
            [{"filename": "Dockerfile", "patch": "-FROM python:3.11-slim\n+FROM python:3.15-slim"}],
            TIER_REVIEW,
        ),
        (
            [{
                "filename": ".github/workflows/ci.yml",
                "patch": "-      - uses: actions/checkout@v4\n+      - uses: actions/checkout@v7",
            }],
            TIER_REVIEW,
        ),
        ([{"filename": "docs/README.md"}], TIER_ROUTINE),
        (
            [{"filename": "pyproject.toml", "patch": '-version = "0.1.4"\n+version = "0.1.5"'}],
            TIER_ROUTINE,
        ),
        (
            [{"filename": "pyproject.toml", "patch": "-ruff==0.17.4\n+ruff==0.17.5"}],
            TIER_ROUTINE,
        ),
        # Adding a checkout step is a workflow edit, not an action version bump.
        (
            [{
                "filename": ".github/workflows/dependabot-auto-merge.yml",
                "patch": (
                    "+      - uses: actions/checkout@v7\n"
                    "+        with:\n+          persist-credentials: false"
                ),
            }],
            TIER_ROUTINE,
        ),
        # A whole set of CI/script/docs edits: routine, no release.
        (
            [
                {"filename": ".github/scripts/change_policy.py"},
                {"filename": ".github/workflows/docs-check.yml"},
                {"filename": "CONTRIBUTING.md"},
                {"filename": "RELEASING.md"},
            ],
            TIER_ROUTINE,
        ),
    ]
    fired: set[str] = set()
    for files, expected in cases:
        verdict = classify(files)
        _require(verdict.tier == expected, f"expected {expected}, got {verdict.tier}: {files}")
        fired.add(verdict.tier)

    missing = {TIER_CORE, TIER_REVIEW, TIER_ROUTINE} - fired
    _require(not missing, f"detectors did not fire: {sorted(missing)}")

    core = classify([{"filename": "src/x.py"}])
    _require(core.release and not core.auto_merge, f"core verdict wrong: {core}")
    review = classify([{"filename": "Dockerfile", "patch": "-FROM p:3.1\n+FROM p:4.1"}])
    _require(not review.release and not review.auto_merge, f"review verdict wrong: {review}")
    routine = classify([{"filename": "README.md"}])
    _require(not routine.release and routine.auto_merge, f"routine verdict wrong: {routine}")

    empty_ok = False
    try:
        classify([])
    except ValueError:
        empty_ok = True
    _require(empty_ok, "an empty file list must not be reported as routine")

    # Paginated API output is concatenated arrays; both pages must be seen.
    paged = '[{"filename": "README.md"}]\n[{"filename": "src/x.py"}]'
    _require(
        load_files(paged) == [{"filename": "README.md"}, {"filename": "src/x.py"}],
        "concatenated pages were not parsed",
    )
    _require(load_files("   \n  ") == [], "whitespace must parse to an empty list")
    _require(
        classify(load_files(paged)).tier == TIER_CORE, "second page must not be lost"
    )
    bad_ok = False
    try:
        load_files('{"not": "an array"}')
    except ValueError:
        bad_ok = True
    _require(bad_ok, "a non-array payload must be rejected")

    print(
        f"[change-policy] self-test: {len(cases)} cases, 3/3 tiers armed, "
        "empty input rejected, concatenated pages parsed"
    )


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        self_test()
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        print("[change-policy] FATAL: no file list on stdin (pipe a JSON array)",
              file=sys.stderr)
        return 2
    try:
        verdict = classify(load_files(raw))
    except (ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"[change-policy] FATAL: {exc}", file=sys.stderr)
        return 2

    if "--json" in argv:
        print(json.dumps(asdict(verdict), ensure_ascii=False))
    else:
        print(f"tier: {verdict.tier}")
        print(f"release: {verdict.release}")
        print(f"auto-merge: {verdict.auto_merge}")
        for reason in verdict.reasons:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
