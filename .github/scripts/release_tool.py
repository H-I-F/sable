#!/usr/bin/env python3
"""Release plumbing: one place for the mechanical steps and their guard rails.

Before this existed, every release was driven by throw-away scripts outside the
repository. This tool keeps the repeatable parts here and turns the release
convention into something CI can actually check.

Commands:

``check [root]``        Static consistency of the tree. Wired into the ``docs``
                        required check, so a half-finished release cannot merge.
``eligible``            Read a GitHub ``compare`` payload on stdin and ask
                        change_policy whether the changes since the last tag
                        earn a release. Non-core changes exit non-zero.
``prepare V D``         Apply the purely mechanical edits for version V dated D:
                        version sources, CHANGELOG archiving, compare/tag links.
                        Prose -- announcements and index wording -- stays human.
``--self-test``         Prove every detector fires on a known-broken tree.

Standard library only, no network, no writes outside ``prepare``. An empty or
malformed input is always an error, never a pass.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

VERSION_PY = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
VERSION_INIT = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _require(cond: bool, msg: str) -> None:
    """Fail loudly; never rely on ``assert`` (disabled under ``python -O``)."""
    if not cond:
        raise RuntimeError(msg)


def _text(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _version(root: Path) -> tuple[str | None, str | None]:
    py = VERSION_PY.search(_text(root, "pyproject.toml"))
    init = None
    for path in sorted(root.glob("src/*/__init__.py")):
        m = VERSION_INIT.search(path.read_text(encoding="utf-8"))
        if m:
            init = m.group(1)
            break
    return (py.group(1) if py else None, init)


def collect(root: Path) -> list[tuple[str, str]]:
    """Return (detector, detail) for every inconsistency found."""
    problems: list[tuple[str, str]] = []
    py_v, init_v = _version(root)

    if py_v is None or init_v is None or py_v != init_v:
        problems.append(("version-source", f"pyproject={py_v} __init__={init_v}"))

    ver = py_v or "0.0.0"
    en, zh = _text(root, "CHANGELOG.md"), _text(root, "CHANGELOG.zh-CN.md")

    if f"## [{ver}]" not in en or f"## [{ver}]" not in zh:
        problems.append(("changelog-archive", f"no archived [{ver}] section in both changelogs"))
    if "## [Unreleased]" not in en or "## [未发布]" not in zh:
        problems.append(("unreleased-section", "empty Unreleased section missing in a changelog"))

    for rel, label in (("CHANGELOG.md", "Unreleased"), ("CHANGELOG.zh-CN.md", "未发布")):
        body = _text(root, rel)
        if f"compare/v{ver}...HEAD" not in body:
            problems.append(("compare-link", f"{rel}: [{label}] does not compare against v{ver}"))
        if f"[{ver}]: " not in body or f"/releases/tag/v{ver}" not in body:
            problems.append(("tag-link", f"{rel}: missing [{ver}] release link"))

    for rel in (f"docs/release-notes-v{ver}.md", f"docs/release-notes-v{ver}.zh-CN.md"):
        if not (root / rel).is_file():
            problems.append(("notes-exist", f"missing {rel}"))

    for rel in ("README.md", "README.zh-CN.md"):
        if f"release-notes-v{ver}" not in _text(root, rel):
            problems.append(("index-readme", f"{rel}: no v{ver} announcement link"))

    for rel in ("docs/README.md", "docs/README.zh-CN.md"):
        if f"release-notes-v{ver}" not in _text(root, rel):
            problems.append(("index-docs", f"{rel}: no v{ver} announcement link"))

    return problems


def _seed_clean_tree(root: Path, ver: str) -> None:
    """Minimal tree that satisfies every detector. Used by self_test."""
    (root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(f'version = "{ver}"\n', encoding="utf-8")
    (root / "src" / "pkg" / "__init__.py").write_text(
        f'__version__ = "{ver}"\n', encoding="utf-8"
    )
    base = "https://github.com/H-I-F/example"
    for rel, label in (("CHANGELOG.md", "Unreleased"), ("CHANGELOG.zh-CN.md", "未发布")):
        (root / rel).write_text(
            f"# Changelog\n\n## [{label}]\n\n## [{ver}] - 2026-11-11\n\nA release.\n\n"
            f"[{label}]: {base}/compare/v{ver}...HEAD\n"
            f"[{ver}]: {base}/releases/tag/v{ver}\n",
            encoding="utf-8",
        )
    for rel in (f"docs/release-notes-v{ver}.md", f"docs/release-notes-v{ver}.zh-CN.md"):
        (root / rel).write_text(f"# v{ver}\n", encoding="utf-8")
    for rel in ("README.md", "README.zh-CN.md", "docs/README.md", "docs/README.zh-CN.md"):
        (root / rel).write_text(f"- [v{ver}](docs/release-notes-v{ver}.md)\n", encoding="utf-8")


def self_test() -> None:
    """Prove all eight detectors fire on a deliberately broken tree."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "pyproject.toml").write_text('version = "9.9.9"\n', encoding="utf-8")
        (root / "src" / "pkg" / "__init__.py").write_text(
            '__version__ = "8.8.8"\n', encoding="utf-8"
        )
        for name in ("CHANGELOG.md", "CHANGELOG.zh-CN.md", "README.md", "README.zh-CN.md"):
            (root / name).write_text("# placeholder\n", encoding="utf-8")
        (root / "docs").mkdir()
        for name in ("README.md", "README.zh-CN.md"):
            (root / "docs" / name).write_text("# placeholder\n", encoding="utf-8")

        fired = {kind for kind, _ in collect(root)}
        expected = {
            "version-source", "changelog-archive", "unreleased-section",
            "compare-link", "tag-link", "notes-exist", "index-readme", "index-docs",
        }
        missing = expected - fired
        _require(not missing, f"SELF-TEST FAILED, detectors did not fire: {sorted(missing)}")
        print(f"[release-tool] self-test: {len(expected)}/{len(expected)} detectors armed")

    # A clean tree must report nothing.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_clean_tree(root, "0.1.4")
        _require(collect(root) == [], f"clean tree failed: {collect(root)}")
        print("[release-tool] self-test: clean tree passes")

    # prepare must actually move the version and archive the changelog.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_clean_tree(root, "0.1.4")
        prepare(root, "0.1.5", "2026-12-01")
        _require(_version(root) == ("0.1.5", "0.1.5"), f"version not moved: {_version(root)}")
        _require("## [0.1.5]" in _text(root, "CHANGELOG.md"), "en changelog not archived")
        _require(
            "## [0.1.5]" in _text(root, "CHANGELOG.zh-CN.md"), "zh changelog not archived"
        )
        _require(
            "compare/v0.1.5...HEAD" in _text(root, "CHANGELOG.md"), "compare link not moved"
        )
        print("[release-tool] self-test: prepare moves version and archives changelog")

    print("[release-tool] self-test: OK")


# ---------------------------------------------------------------------------
# prepare: mechanical edits with hit assertions
# ---------------------------------------------------------------------------


def _sub_once(root: Path, rel: str, pattern: re.Pattern[str], repl: str, label: str) -> None:
    path = root / rel
    body = path.read_text(encoding="utf-8")
    new, n = pattern.subn(repl, body, count=1)
    _require(n == 1, f"prepare: {label} did not match exactly once in {rel} (hits={n})")
    path.write_text(new, encoding="utf-8")


def prepare(root: Path, version: str, date: str) -> None:
    """Apply the mechanical part of a release. Prose stays with the human."""
    old, _ = _version(root)
    _require(bool(old), "prepare: cannot read the current version")
    _require(bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), f"prepare: bad version {version!r}")
    _require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)), f"prepare: bad date {date!r}")

    _sub_once(root, "pyproject.toml", VERSION_PY, f'version = "{version}"', "version field")
    init = next(root.glob("src/*/__init__.py"), None)
    _require(init is not None, "prepare: no src/*/__init__.py found")
    _sub_once(
        root, str(init.relative_to(root)), VERSION_INIT,
        f'__version__ = "{version}"', "__version__ field",
    )

    # Archive the Unreleased section by inserting a new version heading above
    # its contents, then leaving a fresh empty Unreleased heading at the top.
    for rel, label in (("CHANGELOG.md", "Unreleased"), ("CHANGELOG.zh-CN.md", "未发布")):
        body = _text(root, rel)
        heading = f"## [{label}]"
        _require(body.count(heading) == 1, f"prepare: {rel}: expected one {heading}")
        new = body.replace(heading, f"{heading}\n\n## [{version}] - {date}\n", 1)
        (root / rel).write_text(new, encoding="utf-8")

    # Point the Unreleased compare link at the new tag and add the tag link.
    for rel, label in (("CHANGELOG.md", "Unreleased"), ("CHANGELOG.zh-CN.md", "未发布")):
        body = _text(root, rel)
        prefix = f"[{label}]: "
        line = next(
            (ln for ln in body.splitlines() if ln.startswith(prefix) and "/compare/" in ln),
            "",
        )
        _require(bool(line), f"prepare: {rel}: missing [{label}] compare link")
        base = line[len(prefix):].split("/compare/")[0]
        new_line = f"{prefix}{base}/compare/v{version}...HEAD"
        _require(body.count(line) == 1, f"prepare: {rel}: compare link not unique")
        body = body.replace(line, new_line, 1)
        tag_line = f"[{version}]: {base}/releases/tag/v{version}"
        body = body.replace(new_line, f"{new_line}\n{tag_line}", 1)
        (root / rel).write_text(body, encoding="utf-8")

    print(f"[release-tool] prepare {old} -> {version} ({date}): mechanical edits applied")
    print("[release-tool] still human: write both announcements, update the four indexes,")
    print("[release-tool]               then run 'release_tool.py check' before opening the PR.")


# ---------------------------------------------------------------------------
# eligible: does this diff earn a release?
# ---------------------------------------------------------------------------


def eligible(payload: dict) -> tuple[bool, str]:
    from change_policy import TIER_CORE, classify

    files = payload.get("files")
    if not isinstance(files, list):
        raise ValueError("compare payload has no files array")
    verdict = classify(files)
    if verdict.tier == TIER_CORE:
        return True, f"core change present ({'; '.join(verdict.reasons)}) -> a release is due"
    return False, (
        f"tier={verdict.tier}: no engine change since the last tag; "
        "per the release convention this does not earn a version"
    )


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        self_test()
        return 0

    cmd = argv[0] if argv else ""
    rest = argv[1:]

    if cmd == "check":
        root = Path(rest[0]) if rest else Path.cwd()
        # A guard that cannot detect a known fault must never report a clean tree.
        self_test()
        problems = collect(root)
        if problems:
            for kind, detail in problems:
                print(f"[FAIL] {kind}: {detail}", file=sys.stderr)
            print(f"[release-tool] {len(problems)} problem(s)", file=sys.stderr)
            return 1
        print("[release-tool] OK: version sources, changelogs, links and indexes agree")
        return 0

    if cmd == "prepare":
        if len(rest) != 2:
            print("usage: release_tool.py prepare VERSION YYYY-MM-DD", file=sys.stderr)
            return 2
        prepare(Path.cwd(), rest[0], rest[1])
        return 0

    if cmd == "eligible":
        raw = sys.stdin.read()
        if not raw.strip():
            print("[release-tool] FATAL: no compare payload on stdin", file=sys.stderr)
            return 2
        try:
            ok, why = eligible(json.loads(raw))
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            print(f"[release-tool] FATAL: {exc}", file=sys.stderr)
            return 2
        print(why)
        return 0 if ok else 1

    print(
        "usage: release_tool.py {check [root] | eligible | prepare V D | --self-test}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
