#!/usr/bin/env python3
"""
H.I.F deterministic PR review report.

Runs under pull_request_target. It deliberately NEVER checks out, builds or
executes the PR's code: the workflow checks out the default branch only, and
this script reads the PR exclusively through the REST API. Rules live on the
default branch, never in the PR, so a contributor cannot rewrite them.

Phase 1 only suggests. It writes ONE sticky comment and labels from a fixed
allow-list. It never merges, closes, approves, pushes or requests changes.
No model inference, no external services, no cost.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

MARKER = "<!-- hif-pr-report v1 -->"
BOT_LOGIN = "github-actions[bot]"
OWNED_PREFIXES = ("area/", "size/", "risk/")
API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Repository-specific rigor rules. These encode real project knowledge (past
# bugs and invariants), which is what makes the report useful rather than
# generic. Deliberately declarative: no LLM can quietly drift them.
# ---------------------------------------------------------------------------
RULES: dict[str, list[tuple[str, list[str], str]]] = {
    "sable": [
        (
            "snapshot / recovery path",
            ["src/sable/memtable.py", "src/sable/engine.py", "src/sable/sstable.py", "src/sable/wal.py"],
            "Touches snapshot isolation or WAL recovery. Confirm the pinned-sequence "
            "semantics (`MemTable.get_visible`: newest entry with "
            "`sequence <= visible_sequence`) and that the crash matrix still passes.",
        ),
        (
            "python version",
            ["pyproject.toml"],
            "Dependency or `requires-python` change: keep the CI matrix and the "
            "Dockerfile base image in sync with the declared support range.",
        ),
        (
            "container",
            ["Dockerfile", ".dockerignore"],
            "Image change: `docker build & smoke` is a required check; verify the "
            "base tag exists and the CLI starts inside the image.",
        ),
        (
            "ci definition",
            [".github/workflows/ci.yml", ".github/dependabot.yml"],
            "CI/workflow change: remember to update branch protection's required "
            "status checks, otherwise the new job is advisory only.",
        ),
    ],
    "sable-vector": [
        (
            "mcp adapter",
            ["src/sable_vector/adapter/mcp_server.py", "pyproject.toml"],
            "mcp is pinned `<2` because 2.x removed `@server.list_tools` / "
            "`@server.call_tool`. Widening the range breaks test collection "
            "outright; a version bump must be paired with an adapter port.",
        ),
        (
            "retrieval core",
            ["src/sable_vector/index/", "src/sable_vector/engine.py", "src/sable_vector/prefilter.py"],
            "Touches index or filtering logic: recall@k against resident flat is "
            "the acceptance gate; add a regression case if behaviour changed.",
        ),
        (
            "upstream pin",
            ["pyproject.toml"],
            "`sable @ git+...@v0.1.1` is a PEP 508 direct reference. Dependabot "
            "does not update direct references — this stays a deliberate manual bump.",
        ),
        (
            "container",
            ["Dockerfile", ".dockerignore"],
            "Image change: `docker build & self-test` builds the image and runs the "
            "full suite inside it. That job is a required check.",
        ),
        (
            "benchmarks",
            ["benchmarks/"],
            "Benchmark change: keep resident-flat as ground truth and report "
            "recall@k alongside latency, otherwise the numbers are not comparable.",
        ),
    ],
}

LABEL_COLORS = {
    "area/core": "1d76db", "area/tests": "0e8a16", "area/docs": "0075ca",
    "area/ci": "fbca04", "area/deps": "5319e7", "area/docker": "006b75",
    "area/mcp": "b60205", "area/bench": "c5def5", "area/other": "cccccc",
    "size/XS": "3cbf00", "size/S": "5d9801", "size/M": "dfb317", "size/L": "d93f0b",
    "risk/high": "e11d21",
}


def die(msg: str) -> None:
    print(f"[!] FATAL: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# GitHub gives a read-only token to pull_request_target workflows triggered by
# Dependabot. That is a permission limit, NOT a scanner failure: reads still
# work, only the advisory write is refused. Report it loudly, but do not turn
# the PR red -- the bot is advisory and must never block a merge.
DENIED = (401, 403)


def write_denied(status: int, what: str) -> None:
    print(f"[!] WRITE DENIED (HTTP {status}) while {what}. "
          "Token is read-only for this event (typically a Dependabot PR). "
          "Reads succeeded, so this is not a scanner failure. "
          "Advisory report skipped without failing the check.", flush=True)
    raise SystemExit(0)


def request(method: str, path: str, body: dict | None = None,
            ok_errors: tuple[int, ...] = ()) -> tuple[object, int]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hif-pr-report",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else {}), resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        if exc.code in ok_errors:
            return {}, exc.code
        die(f"{method} {path} -> HTTP {exc.code}: {detail}")
    except Exception as exc:  # network / TLS
        die(f"{method} {path} -> {exc}")
    return {}, 0


def paged(path: str) -> list:
    out: list = []
    page = 1
    while page <= 10:
        sep = "&" if "?" in path else "?"
        chunk, _ = request("GET", f"{path}{sep}per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out


def ensure_label(name: str) -> None:
    _, status = request("POST", f"/repos/{REPO}/labels",
                        {"name": name, "color": LABEL_COLORS.get(name, "cccccc"),
                         "description": "Managed by the H.I.F PR review report"},
                        ok_errors=(422,) + DENIED)  # 422 = already exists
    if status in DENIED:
        write_denied(status, f"ensuring label {name}")
    if status not in (201, 422):
        die(f"could not ensure label {name} (HTTP {status})")


def classify(path: str) -> str:
    if path.startswith("src/"):
        return "area/core"
    if path.startswith("tests/"):
        return "area/tests"
    if path.startswith("benchmarks/") or path.startswith("examples/"):
        return "area/bench"
    if path in ("Dockerfile", ".dockerignore"):
        return "area/docker"
    if path.startswith(".github/"):
        return "area/ci"
    if path in ("pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock"):
        return "area/deps"
    if path.lower().endswith((".md", ".rst", ".txt")) or path == "LICENSE":
        return "area/docs"
    return "area/other"


def size_label(total: int) -> str:
    if total < 50:
        return "size/XS"
    if total < 200:
        return "size/S"
    if total < 600:
        return "size/M"
    return "size/L"


def icon(conclusion: str | None, status: str) -> str:
    if conclusion == "success":
        return "pass"
    if conclusion in ("failure", "timed_out", "startup_failure"):
        return "FAIL"
    if conclusion in ("skipped", "neutral", "cancelled", "action_required", "stale"):
        return "skip"
    return "run" if status != "completed" else "?"


def build_report(pr: dict, files: list, checks: list, rules_hit: list,
                 head: str) -> str:
    n = pr["number"]
    title = pr["title"]
    author = pr["user"]["login"]
    adds = pr.get("additions") or 0
    dels = pr.get("deletions") or 0
    total = adds + dels
    areas = Counter(classify(f["filename"]) for f in files)
    is_bot = author in ("dependabot[bot]", "app/dependabot", "github-actions[bot]")

    lines: list[str] = []
    lines.append(MARKER)
    lines.append("## H.I.F Protocol - automated PR review")
    lines.append("")
    lines.append("> Advisory only. Deterministic checks, no model inference. "
             "This bot never merges, closes, approves or pushes.")
    lines.append("")
    lines.append("| | |")
    lines.append("|:---|:---|")
    lines.append(f"| **PR** | #{n} - {title} |")
    lines.append(f"| **Author** | `{author}`" + (" (automated)" if is_bot else "") + " |")
    lines.append(f"| **Change** | {len(files)} file(s), +{adds} / -{dels} |")
    lines.append(f"| **Head** | `{head[:7]}` |")
    lines.append("")

    lines.append("### Change map")
    lines.append("")
    lines.append("| Area | Files |")
    lines.append("|:---|---:|")
    for area, count in sorted(areas.items()):
        lines.append(f"| `{area}` | {count} |")
    lines.append("")

    lines.append("### Gate status")
    lines.append("")
    if not checks:
        lines.append("_No check runs reported for this head yet (CI may not have "
                 "started). This is **not** a pass._")
    else:
        lines.append("| Check | Result |")
        lines.append("|:---|:---|")
        for c in sorted(checks, key=lambda x: x["name"]):
            if c["name"] == "report":  # this workflow's own job is not a gate
                continue
            lines.append(f"| `{c['name']}` | {icon(c.get('conclusion'), c['status'])} |")
    lines.append("")

    lines.append("### Rigor notes")
    lines.append("")
    notes: list[str] = []
    for label, _paths, text in rules_hit:
        notes.append(f"- **{label}** - {text}")
    src_changed = any(classify(f["filename"]) == "area/core" for f in files)
    tests_changed = any(classify(f["filename"]) == "area/tests" for f in files)
    if src_changed and not tests_changed and not is_bot:
        notes.append("- **tests** - Source changed with no test updates. "
                     "Add a regression case or explain why existing coverage holds.")
    if len(files) > 25 or total > 1000:
        notes.append("- **size** - Large changeset. Consider splitting it so each "
                     "part is reviewable on its own.")
    if not notes:
        notes.append("- No project-specific invariants touched. Standard review "
                     "still applies.")
    lines.extend(notes)
    lines.append("")
    lines.append("---")
    lines.append("_Reviewed by **H.I.F CI** - rules live on the default branch, "
             "PR code is never checked out or executed. "
             "Re-runs edit this comment in place._")
    return "\n".join(lines) + "\n"


def main() -> None:
    global TOKEN, REPO
    TOKEN = os.environ.get("GITHUB_TOKEN", "")
    REPO = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    # Self-check 1: distinguish "scanner broken" from "zero results".
    for key, val in (("GITHUB_TOKEN", TOKEN), ("GITHUB_REPOSITORY", REPO),
                     ("GITHUB_EVENT_PATH", event_path), ("GITHUB_EVENT_NAME", event_name)):
        if not val:
            die(f"missing environment variable {key}")
    if "/" not in REPO:
        die(f"GITHUB_REPOSITORY malformed: {REPO!r}")
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)

    if event_name == "pull_request_target":
        pr_number = event["pull_request"]["number"]
    elif event_name == "workflow_run":
        wr = event["workflow_run"]
        prs = wr.get("pull_requests") or []
        if not prs:
            # A CI run on a push (or a fork PR) has no associated pull_requests.
            # That is a normal no-op, not a scanner failure.
            print("[=] CI run has no associated PR; nothing to report", flush=True)
            return
        pr_number = prs[0]["number"]
    elif event_name == "workflow_dispatch":
        raw = (event.get("inputs") or {}).get("pr", "")
        if not str(raw).strip().isdigit():
            die("workflow_dispatch requires a numeric 'pr' input")
        pr_number = int(raw)
    else:
        die(f"unsupported event: {event_name}")

    short = REPO.split("/")[-1]
    rules = RULES.get(short, [])
    print(f"[*] {REPO} PR #{pr_number} via {event_name}; {len(rules)} rule groups", flush=True)

    pr, _ = request("GET", f"/repos/{REPO}/pulls/{pr_number}")
    if not isinstance(pr, dict) or "head" not in pr:
        die("PR payload malformed")
    head = pr["head"]["sha"]
    if pr.get("state") != "open":
        print(f"[=] PR #{pr_number} is {pr.get('state')}; skipping", flush=True)
        return

    files = paged(f"/repos/{REPO}/pulls/{pr_number}/files")
    # Self-check 2: a PR always has at least one file. Empty means the API
    # scan failed, not that the PR is empty.
    if not files:
        die("file scan returned 0 entries for a PR that must have changes - "
            "treating as scanner failure, not an empty result")
    filenames = [f["filename"] for f in files]

    cr, _ = request("GET", f"/repos/{REPO}/commits/{head}/check-runs?per_page=100")
    checks = (cr or {}).get("check_runs", []) if isinstance(cr, dict) else []
    if not isinstance(checks, list):
        die("check-runs payload malformed")

    rules_hit = []
    for label, paths, text in rules:
        if any(fn == p or fn.startswith(p) for fn in filenames for p in paths):
            rules_hit.append((label, paths, text))

    body = build_report(pr, files, checks, rules_hit, head)

    # --- labels -------------------------------------------------------------
    desired = {classify(fn) for fn in filenames} | {size_label((pr.get("additions") or 0) + (pr.get("deletions") or 0))}
    if any(classify(fn) == "area/core" for fn in filenames) and not any(
            classify(fn) == "area/tests" for fn in filenames) and \
            pr["user"]["login"] not in ("dependabot[bot]", "app/dependabot", "github-actions[bot]"):
        desired.add("risk/high")
    for name in sorted(desired):
        ensure_label(name)
    current, _ = request("GET", f"/repos/{REPO}/issues/{pr_number}/labels?per_page=100")
    have = {entry["name"] for entry in current} if isinstance(current, list) else set()
    # Only remove labels this bot owns; never touch human-applied labels.
    stale = {n for n in have if n.startswith(OWNED_PREFIXES)} - desired
    for name in sorted(stale):
        _, st = request("DELETE", f"/repos/{REPO}/issues/{pr_number}/labels/" + urllib.parse.quote(name, safe=""),
                        ok_errors=(404,) + DENIED)
        if st in DENIED:
            write_denied(st, f"removing stale label {name}")
    to_add = sorted(desired - have)
    if to_add:
        _, st = request("POST", f"/repos/{REPO}/issues/{pr_number}/labels", {"labels": to_add},
                        ok_errors=DENIED)
        if st in DENIED:
            write_denied(st, "applying labels")

    # --- sticky comment -----------------------------------------------------
    comments = paged(f"/repos/{REPO}/issues/{pr_number}/comments")
    mine = [c for c in comments if MARKER in (c.get("body") or "") and
            c.get("user", {}).get("login") == BOT_LOGIN]
    if mine:
        cid = mine[0]["id"]
        _, st = request("PATCH", f"/repos/{REPO}/issues/comments/{cid}", {"body": body},
                        ok_errors=DENIED)
        if st in DENIED:
            write_denied(st, "updating sticky comment")
        action = f"updated comment {cid}"
    else:
        result, status = request("POST", f"/repos/{REPO}/issues/{pr_number}/comments", {"body": body},
                                 ok_errors=DENIED)
        if status in DENIED:
            write_denied(status, "creating sticky comment")
        if status not in (200, 201):
            die(f"comment creation returned HTTP {status}")
        cid = (result or {}).get("id")
        action = f"created comment {cid}"

    # Self-check 3: read back and assert the marker is really on the PR.
    verify = paged(f"/repos/{REPO}/issues/{pr_number}/comments")
    if not any(MARKER in (c.get("body") or "") for c in verify):
        die("read-back failed: marker comment not found after write")
    print(f"[+] {REPO} #{pr_number}: {action} | "
          f"files={len(files)} checks={len(checks)} notes={len(rules_hit)} "
          f"labels_added={len(to_add)} | self-check OK", flush=True)


if __name__ == "__main__":
    main()
