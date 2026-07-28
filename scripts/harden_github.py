#!/usr/bin/env python3
"""GitHub repo settings as config-as-code — not hand-clicked in the web UI.

Idempotent: re-run any time to converge the remote repo settings to this spec. Each setting is
applied independently and tolerates plan-tier limits (some private-repo protections need a paid plan
— those are reported, not fatal). Re-run after upgrading the plan to pick them up.

    python scripts/harden_github.py                 # converge settings
    python scripts/harden_github.py --check         # read live settings + drift, write nothing
    python scripts/harden_github.py --dry-run       # show what would be sent, send nothing
    python scripts/harden_github.py owner/repo      # a different repo

Requires the `gh` CLI, authenticated. Stdlib only — no jq, no PyYAML.
"""

from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

DEFAULT_REPO = "kphutt/gdmutant"

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The jobs that must block a merge, named by their **job id** (the stable YAML key) rather than by
# the status-check context string GitHub reports. The context is derived from the workflow at run
# time — see `required_contexts()` — because a job's reported context is not simply its `name:`:
#
#   * a matrix job reports one context per combination, suffixed with the matrix values.
#     `verify` is `name: Verify` over `os: [ubuntu-24.04, windows-2025]`, so GitHub reports
#     `Verify (ubuntu-24.04)` and `Verify (windows-2025)` — never a bare `Verify`;
#   * renaming a job's `name:` silently changes its context.
#
# Requiring a context that no job can report blocks every PR on the branch forever, and this script
# is idempotent config-as-code, so a stale literal list does not merely go out of date — re-running
# it actively overwrites a correct live setting with the broken one.
#
# Only jobs that report on EVERY pull request belong here. A path-filtered workflow
# (`action-smoke.yml`, `mutation.yml`) and an `if:`-gated job (`selftest-godot-run`,
# `selftest-gut-run`) do not report at all when they are filtered out, so requiring one would hang
# every PR that misses its paths. That is why `ci.yml` carries the always-running `selftest-godot`
# and `selftest-gut` gate jobs: those are the required names, and the heavy path-filtered workers
# sit behind them.
REQUIRED_JOBS = (
    "verify",
    "secret-scan",
    "selftest-godot",
    "selftest-gut",
)


def _ok(msg: str) -> None:
    print(f"\033[1;32m[ok]\033[0m   {msg}")


def _warn(msg: str) -> None:
    print(f"\033[1;33m[skip]\033[0m {msg}")


def _log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def _gh(args: list[str], *, stdin: str | None = None) -> tuple[bool, str]:
    """Run ``gh <args>``; return (succeeded, combined output).

    Never raises: a failed setting is reported (a warn), not fatal — matching the old script's
    ``set -uo pipefail`` (no ``-e``), which applied each setting independently.
    """
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


# ---------------------------------------------------------------------------------------------
# Deriving the required status-check contexts from the workflow files
# ---------------------------------------------------------------------------------------------


@dataclass
class Job:
    """The slice of a workflow job this script needs: what GitHub will call its status check."""

    job_id: str
    workflow: str
    name: str
    conditional: bool = False
    matrix: dict[str, list[str]] = field(default_factory=dict)

    def contexts(self) -> list[str]:
        """The status-check context(s) GitHub reports for this job.

        A plain job reports its `name:` verbatim. A matrix job reports one context per
        combination, with the matrix values in declaration order joined by ", " in parentheses.
        """
        if "${{" in self.name:
            raise ValueError(
                f"job {self.job_id!r} in {self.workflow} has a templated name ({self.name!r}), "
                "so its status-check context cannot be derived. Give the job a literal name."
            )
        if not self.matrix:
            return [self.name]
        return [
            f"{self.name} ({', '.join(combo)})"
            for combo in itertools.product(*self.matrix.values())
        ]


_JOB_ID = re.compile(r"^  ([A-Za-z0-9_][A-Za-z0-9_.-]*):\s*(?:#.*)?$")
_KEY_AT = re.compile(r"^(\s*)([A-Za-z0-9_-]+):\s*(.*)$")


def _scalar(raw: str) -> str:
    """A YAML scalar with any trailing comment and surrounding quotes removed."""
    value = re.sub(r"\s+#.*$", "", raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _parse_matrix(block: list[str]) -> dict[str, list[str]]:
    """The matrix dimensions of one job block, in declaration order.

    `include` / `exclude` change the set of combinations in ways this parser does not model, so
    they raise rather than silently producing a wrong — and therefore merge-blocking — context.
    """
    matrix: dict[str, list[str]] = {}
    in_strategy = False
    in_matrix = False
    dim: str | None = None
    for line in block:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.rstrip("\n").lstrip())
        match = _KEY_AT.match(line)
        if match and match.group(1) == "    ":
            in_strategy = match.group(2) == "strategy"
            in_matrix = False
            dim = None
            continue
        if in_strategy and match and match.group(1) == "      ":
            in_matrix = match.group(2) == "matrix"
            dim = None
            continue
        if in_matrix and match and match.group(1) == "        ":
            key, rest = match.group(2), _scalar(match.group(3))
            if key in ("include", "exclude"):
                raise ValueError(
                    f"matrix {key!r} is not modelled by this parser, so the derived status-check "
                    "contexts would be wrong — and a wrong required check blocks every PR."
                )
            dim = key
            if rest.startswith("[") and rest.endswith("]"):
                matrix[dim] = [_scalar(v) for v in rest[1:-1].split(",") if v.strip()]
                dim = None
            else:
                matrix[dim] = []
            continue
        if in_matrix and dim is not None and indent == 10 and line.lstrip().startswith("- "):
            matrix[dim].append(_scalar(line.lstrip()[2:]))
    return matrix


def parse_workflow(path: pathlib.Path) -> dict[str, Job]:
    """Every job a workflow file defines, keyed by job id."""
    blocks: dict[str, list[str]] = {}
    in_jobs = False
    current: str | None = None
    block: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and line.strip() and re.match(r"^\S", line):
            in_jobs = False  # dedented back out of jobs:
        if not in_jobs:
            continue
        match = _JOB_ID.match(line)
        if match:
            if current is not None:
                blocks[current] = block
            current = match.group(1)
            block = []
            continue
        if current is not None:
            block.append(line)
    if current is not None:
        blocks[current] = block

    jobs: dict[str, Job] = {}
    for job_id, job_block in blocks.items():
        name = job_id
        conditional = False
        for line in job_block:
            match = _KEY_AT.match(line)
            if not match or match.group(1) != "    ":
                continue
            if match.group(2) == "name":
                name = _scalar(match.group(3))
            elif match.group(2) == "if":
                # `if: always()` runs unconditionally. Anything else can skip, and a skipped job
                # never reports a status, so it can never be a required check.
                conditional = _scalar(match.group(3)) != "always()"
        jobs[job_id] = Job(
            job_id=job_id,
            workflow=path.name,
            name=name,
            conditional=conditional,
            matrix=_parse_matrix(job_block),
        )
    return jobs


def all_jobs(workflows: pathlib.Path = WORKFLOWS) -> dict[str, Job]:
    """Every job defined across every workflow file, keyed by job id."""
    jobs: dict[str, Job] = {}
    for wf in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        jobs.update(parse_workflow(wf))
    return jobs


def required_contexts(workflows: pathlib.Path = WORKFLOWS) -> list[str]:
    """The status-check contexts for `REQUIRED_JOBS`, derived from the workflow files.

    Raises if a required job id is missing or is `if:`-gated: both would require a context that
    nothing reports, which blocks every PR on the branch forever.
    """
    jobs = all_jobs(workflows)
    contexts: list[str] = []
    for job_id in REQUIRED_JOBS:
        job = jobs.get(job_id)
        if job is None:
            raise ValueError(
                f"required job id {job_id!r} is defined in no workflow under {workflows}. "
                "It was renamed or removed, so update REQUIRED_JOBS — requiring a check that "
                "nothing reports blocks every PR."
            )
        if job.conditional:
            raise ValueError(
                f"required job {job_id!r} in {job.workflow} is if:-gated, so it can be skipped, "
                "and a skipped job never reports. Require an always-running gate job instead."
            )
        contexts.extend(job.contexts())
    return contexts


# ---------------------------------------------------------------------------------------------
# Reading live state
# ---------------------------------------------------------------------------------------------


def live_required_contexts(repo: str) -> list[str] | None:
    """The contexts branch protection requires on `main` today, or None if it is unreadable."""
    succeeded, out = _gh(["api", f"repos/{repo}/branches/main/protection"])
    if not succeeded:
        return None
    checks = json.loads(out).get("required_status_checks") or {}
    return list(checks.get("contexts") or [])


def report_state(repo: str) -> None:
    """Print live settings plus a two-way comparison against what this script would require."""
    succeeded, out = _gh(["api", f"repos/{repo}"])
    if succeeded:
        data = json.loads(out)
        _log("Live repo settings:")
        for key in (
            "private",
            "delete_branch_on_merge",
            "allow_squash_merge",
            "allow_merge_commit",
            "allow_rebase_merge",
            "allow_auto_merge",
        ):
            print(f"    {key:34} {data.get(key)}")
        security = data.get("security_and_analysis") or {}
        for key in ("secret_scanning", "secret_scanning_push_protection"):
            print(f"    {key:34} {(security.get(key) or {}).get('status')}")
    else:
        _warn("Could not read repo settings.")

    succeeded, out = _gh(["api", f"repos/{repo}/branches/main/protection"])
    print(f"    {'branch protection (main)':34} {'ENABLED' if succeeded else 'not set'}")
    live: set[str] = set()
    if succeeded:
        protection = json.loads(out)
        live = set((protection.get("required_status_checks") or {}).get("contexts") or [])
        for key in ("enforce_admins", "required_conversation_resolution"):
            print(f"    {key:34} {(protection.get(key) or {}).get('enabled')}")

    computed = set(required_contexts())
    _log("Required status checks  [live/spec]:")
    for context in sorted(live | computed):
        left = "live" if context in live else "----"
        right = "spec" if context in computed else "----"
        print(f"    [{left}/{right}] {context}")

    dropped = live - computed
    if dropped:
        _warn(f"Live checks this spec would DROP: {sorted(dropped)}")
        print("         Running this script would REDUCE protection. Fix REQUIRED_JOBS first.")
    added = computed - live
    if added:
        _warn(f"Checks this spec would ADD: {sorted(added)}")

    advisory = sorted(
        {c for job in all_jobs().values() if not job.conditional for c in job.contexts()} - computed
    )
    if advisory:
        _log("Jobs that are not required checks (they run, but cannot block a merge):")
        for context in advisory:
            print(f"    {context}")
        print("    Some only fire on their own trigger or path filter and must stay advisory.")


# ---------------------------------------------------------------------------------------------
# Applying settings
# ---------------------------------------------------------------------------------------------


def _apply(desc: str, args: list[str], payload: dict | None, on_fail: str, dry_run: bool) -> None:
    if dry_run:
        body = json.dumps(payload) if payload is not None else "(no body)"
        _warn(f"[dry-run] would set: {desc}")
        print(f"         gh api {' '.join(args)}  {body}")
        return
    stdin = None
    if payload is not None:
        args = [*args, "--input", "-"]
        stdin = json.dumps(payload)
    succeeded, out = _gh(["api", *args], stdin=stdin)
    if succeeded:
        _ok(desc)
    else:
        _warn(on_fail)
        if out:
            print(f"         gh: {out.splitlines()[0][:140]}")


def protection_payload(workflows: pathlib.Path = WORKFLOWS) -> dict:
    """The branch-protection spec for `main`.

    Solo repo => required approving reviews = 0 (self-approval is impossible, so a nonzero count
    would deadlock every PR). CODEOWNERS review stays advisory.
    """
    return {
        "required_status_checks": {"strict": True, "contexts": required_contexts(workflows)},
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "require_code_owner_reviews": False,
        },
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "required_conversation_resolution": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("repo", nargs="?", default=DEFAULT_REPO)
    parser.add_argument(
        "--check", action="store_true", help="report live settings and drift; write nothing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; send nothing"
    )
    args = parser.parse_args(argv)

    if not shutil.which("gh"):
        print("[error] gh CLI not found. Install it, then `gh auth login`.", file=sys.stderr)
        return 1

    try:
        contexts = required_contexts()
    except ValueError as exc:
        print(f"[error] cannot derive the required status checks: {exc}", file=sys.stderr)
        return 1

    if args.check:
        report_state(args.repo)
        return 0

    _log(f"Hardening {args.repo}{' (dry run)' if args.dry_run else ''}")
    _log(f"Required status checks, derived from the workflows: {contexts}")

    _apply(
        "Merge hygiene: squash-only, delete-branch-on-merge, auto-merge allowed.",
        ["-X", "PATCH", f"repos/{args.repo}"],
        {
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "allow_auto_merge": True,
        },
        "Could not set merge hygiene (permissions?).",
        args.dry_run,
    )

    _apply(
        "Actions default token set read-only.",
        ["-X", "PUT", f"repos/{args.repo}/actions/permissions/workflow"],
        {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
        "Could not set Actions token permissions.",
        args.dry_run,
    )

    _apply(
        "Dependabot vulnerability alerts enabled.",
        ["-X", "PUT", f"repos/{args.repo}/vulnerability-alerts"],
        None,
        "Could not enable vulnerability alerts.",
        args.dry_run,
    )
    _apply(
        "Dependabot automated security fixes enabled.",
        ["-X", "PUT", f"repos/{args.repo}/automated-security-fixes"],
        None,
        "Could not enable automated security fixes.",
        args.dry_run,
    )

    # Nested JSON, so it goes in on stdin via `--input -`: `--raw-field` expects key=value and
    # cannot round-trip a nested object.
    _apply(
        "Native secret scanning + push protection enabled.",
        ["-X", "PATCH", f"repos/{args.repo}"],
        {
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            }
        },
        "Secret scanning/push protection unavailable (private repos need GitHub Advanced "
        "Security). gitleaks in CI is the backstop.",
        args.dry_run,
    )

    # Ratchet: never trade a live required check away for a derived one. If protection already
    # requires something this spec does not, the spec is behind, and writing would REDUCE the gate.
    live = live_required_contexts(args.repo)
    if live is not None:
        dropped = set(live) - set(contexts)
        if dropped:
            _warn(f"Branch protection NOT written: this spec would drop {sorted(dropped)}.")
            print("         Add the matching job ids to REQUIRED_JOBS, then re-run.")
            print("         Review with:  python scripts/harden_github.py --check")
            return 1

    _apply(
        "Branch protection on 'main': PR required, CI checks required, no force-push.",
        [
            "-X",
            "PUT",
            f"repos/{args.repo}/branches/main/protection",
            "-H",
            "Accept: application/vnd.github+json",
        ],
        protection_payload(),
        "Branch protection NOT set — private-repo protection needs a paid GitHub plan. "
        "Until then, PR discipline is convention-only. Upgrade, then re-run this script.",
        args.dry_run,
    )

    if not args.dry_run:
        print()
        report_state(args.repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
