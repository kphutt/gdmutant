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


def _workflows_dir(workflows: pathlib.Path | None) -> pathlib.Path:
    """Resolve the workflow directory at CALL time.

    `def f(workflows=WORKFLOWS)` would freeze the module global at import, which makes the
    default silently unoverridable and hides a whole class of test from ever running.
    """
    return WORKFLOWS if workflows is None else workflows


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
# Only jobs that report on EVERY pull request belong here. Four things disqualify a job, and
# `required_contexts()` raises on each rather than producing a context nothing can report:
#
#   * its workflow has no `pull_request` trigger at all - it cannot run on a PR, so it cannot
#     report on one. A workflow reduced to `workflow_dispatch` (checks moved to the local
#     pre-commit gate) puts every job in it here;
#   * its workflow's `pull_request` trigger is path-filtered (`action-smoke.yml`, `mutation.yml`),
#     so it stays pending on any PR that misses those paths;
#   * the job is `if:`-gated (`selftest-godot-run`, `selftest-gut-run`) - a skipped job never
#     reports. That is why `ci.yml` carries the always-running `selftest-godot` / `selftest-gut`
#     gate jobs, with the heavy path-filtered workers behind them;
#   * the id is defined in two workflows that both run on every PR, so which one reports is
#     ambiguous.
#
# If this list should be empty - because no check gates a merge in the cloud any more - empty it
# deliberately. Leaving ids here that nothing can report is the same failure as hardcoding a
# stale context string, just one step further back.
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


@dataclass(frozen=True)
class Triggers:
    """The slice of a workflow's `on:` block this script needs: can it run on a pull request?"""

    events: frozenset[str] = frozenset()
    pull_request_filtered: bool = False

    @property
    def on_pull_request(self) -> bool:
        return "pull_request" in self.events or "pull_request_target" in self.events

    @property
    def on_every_pull_request(self) -> bool:
        """True only if every pull request triggers this workflow — no `paths:` filter."""
        return self.on_pull_request and not self.pull_request_filtered

    def describe(self) -> str:
        return ", ".join(sorted(self.events)) or "no events at all"


@dataclass
class Job:
    """The slice of a workflow job this script needs: what GitHub will call its status check."""

    job_id: str
    workflow: str
    name: str
    conditional: bool = False
    matrix: dict[str, list[str]] = field(default_factory=dict)
    triggers: Triggers = field(default_factory=Triggers)

    @property
    def reports_on_every_pull_request(self) -> bool:
        """Whether this job produces a status check on EVERY pull request.

        Only such a job can be a required check: GitHub waits for a required context forever, and
        a job that is filtered out, skipped, or simply never triggered by a pull request never
        reports one.
        """
        return self.triggers.on_every_pull_request and not self.conditional

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


_ON = re.compile(r"^on:\s*(.*)$")


def parse_triggers(lines: list[str]) -> Triggers:
    """The events in a workflow's `on:` block, and whether its pull_request trigger is filtered.

    Handles the three forms GitHub accepts: `on: push`, `on: [push, pull_request]`, and the block
    mapping. A `paths:` / `paths-ignore:` under `pull_request:` means the workflow does not fire on
    every pull request, which disqualifies its jobs from being required checks.
    """
    events: set[str] = set()
    filtered = False
    index = 0
    while index < len(lines):
        match = _ON.match(lines[index])
        if not match:
            index += 1
            continue
        rest = _scalar(match.group(1))
        if rest.startswith("[") and rest.endswith("]"):
            return Triggers(frozenset(_scalar(v) for v in rest[1:-1].split(",") if v.strip()))
        if rest:
            return Triggers(frozenset({rest}))
        event: str | None = None
        for line in lines[index + 1 :]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if re.match(r"^\S", line):
                break  # dedented back out of on:
            key = _KEY_AT.match(line)
            if key and key.group(1) == "  ":
                event = key.group(2)
                events.add(event)
            elif key and key.group(1) == "    " and event == "pull_request":
                filtered = filtered or key.group(2) in ("paths", "paths-ignore")
            elif line.startswith("  - "):
                events.add(_scalar(line[4:]))  # on:\n  - push
        return Triggers(frozenset(events), filtered)
    return Triggers()


def parse_workflow(path: pathlib.Path) -> dict[str, Job]:
    """Every job a workflow file defines, keyed by job id."""
    blocks: dict[str, list[str]] = {}
    in_jobs = False
    current: str | None = None
    block: list[str] = []

    lines = path.read_text(encoding="utf-8").splitlines()
    triggers = parse_triggers(lines)

    for line in lines:
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
            triggers=triggers,
        )
    return jobs


def all_jobs(workflows: pathlib.Path | None = None) -> dict[str, list[Job]]:
    """Every job defined across every workflow file, grouped by job id.

    A job id is unique within one workflow but NOT across workflows: this repo defines
    `secret-scan`, `selftest-godot` and `selftest-gut` in both `ci.yml` (the merge gate) and
    `publish.yml` (the release gate re-running them live on the released commit). Keeping every
    definition, rather than letting the last file parsed win, is what lets `required_contexts()`
    pick the one that can actually report on a pull request instead of silently reading a
    release-only job's triggers.
    """
    workflows = _workflows_dir(workflows)
    jobs: dict[str, list[Job]] = {}
    for wf in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for job_id, job in parse_workflow(wf).items():
            jobs.setdefault(job_id, []).append(job)
    return jobs


def _cannot_be_required(job: Job) -> str | None:
    """Why this job can never report a status check on every PR, or None if it can."""
    if not job.triggers.on_pull_request:
        return (
            f"in {job.workflow}, which no pull request triggers "
            f"(it runs on: {job.triggers.describe()})"
        )
    if job.triggers.pull_request_filtered:
        return (
            f"in {job.workflow}, whose `pull_request` trigger is path-filtered, "
            "so it skips some pull requests"
        )
    if job.conditional:
        return f"in {job.workflow}, but the job is if:-gated, and a skipped job never reports"
    return None


def required_contexts(workflows: pathlib.Path | None = None) -> list[str]:
    """The status-check contexts for `REQUIRED_JOBS`, derived from the workflow files.

    Raises if a required job id is missing, lives in a workflow no pull request triggers, sits
    behind a `paths:` filter, or is `if:`-gated. Every one of those would require a context that
    nothing reports, which blocks every PR on the branch forever.
    """
    workflows = _workflows_dir(workflows)
    jobs = all_jobs(workflows)
    contexts: list[str] = []
    for job_id in REQUIRED_JOBS:
        candidates = jobs.get(job_id) or []
        if not candidates:
            raise ValueError(
                f"required job id {job_id!r} is defined in no workflow under {workflows}. "
                "It was renamed or removed, so update REQUIRED_JOBS - requiring a check that "
                "nothing reports blocks every PR."
            )
        eligible = [job for job in candidates if _cannot_be_required(job) is None]
        if not eligible:
            reasons = "; ".join(str(_cannot_be_required(job)) for job in candidates)
            raise ValueError(
                f"required job {job_id!r} can never report a status check on a pull request "
                f"({reasons}). GitHub waits for a required context forever, so requiring it "
                "leaves every PR on the branch pending. Either give the workflow a "
                "`pull_request` trigger, or drop the id from REQUIRED_JOBS - a check that runs "
                "locally instead of in CI is not a required check."
            )
        if len(eligible) > 1:
            where = ", ".join(job.workflow for job in eligible)
            raise ValueError(
                f"job id {job_id!r} is defined in more than one workflow that runs on every pull "
                f"request ({where}), so which job reports the required context is ambiguous. "
                "Rename one of them."
            )
        contexts.extend(eligible[0].contexts())
    return contexts


def producible_contexts(workflows: pathlib.Path | None = None) -> set[str]:
    """Every status-check context some job actually reports on EVERY pull request.

    This is the universe a required check may be drawn from. A job whose name cannot be predicted
    (a templated `name:`) is left out rather than guessed: it cannot vouch for a required context,
    and leaving it out can only cause a refusal to write, never a bad write.
    """
    contexts: set[str] = set()
    for definitions in all_jobs(workflows).values():
        for job in definitions:
            if not job.reports_on_every_pull_request:
                continue
            try:
                contexts.update(job.contexts())
            except ValueError:
                continue
    return contexts


# ---------------------------------------------------------------------------------------------
# Reading live state
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveRequiredChecks:
    """What branch protection requires on `main` today — as three states, not one list.

    "The branch requires no checks at all" and "the branch requires exactly these checks" are
    different facts with opposite consequences, and collapsing both to an empty list is how a
    report ends up reading as a match when nothing is gating anything. `configured` keeps them
    apart; `readable` keeps "we could not ask" apart from both.
    """

    readable: bool
    configured: bool
    contexts: tuple[str, ...] = ()

    def describe(self) -> str:
        # ASCII only: this string is printed, and a Windows console in cp1252 mangles anything else.
        if not self.readable:
            return "UNREADABLE: the protection endpoint did not answer"
        if not self.configured:
            return "ABSENT: protection carries no `required_status_checks` at all"
        if not self.contexts:
            return "EMPTY: `required_status_checks` is present but lists no contexts"
        return f"{len(self.contexts)} context(s) required"

    @property
    def gates_nothing(self) -> bool:
        return self.readable and not self.contexts


def required_checks_of(protection: dict) -> LiveRequiredChecks:
    """Read the required-check state out of an already-fetched protection payload."""
    block = protection.get("required_status_checks")
    if not isinstance(block, dict):
        return LiveRequiredChecks(readable=True, configured=False)
    return LiveRequiredChecks(
        readable=True, configured=True, contexts=tuple(block.get("contexts") or [])
    )


def live_required_checks(repo: str) -> LiveRequiredChecks:
    """The required-check state of `main` today, fetched read-only."""
    succeeded, out = _gh(["api", f"repos/{repo}/branches/main/protection"])
    if not succeeded:
        return LiveRequiredChecks(readable=False, configured=False)
    return required_checks_of(json.loads(out))


def report_state(
    repo: str,
    contexts: list[str] | None = None,
    derivation_error: ValueError | None = None,
) -> None:
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
    checks = LiveRequiredChecks(readable=False, configured=False)
    if succeeded:
        protection = json.loads(out)
        checks = required_checks_of(protection)
        for key in ("enforce_admins", "required_conversation_resolution"):
            print(f"    {key:34} {(protection.get(key) or {}).get('enabled')}")
    print(f"    {'required status checks':34} {checks.describe()}")

    live = set(checks.contexts)
    computed = set(contexts or [])

    _log("Required status checks  [live/spec]:")
    if derivation_error is not None:
        _warn(f"This spec cannot be derived from the workflow files: {derivation_error}")
        print("         The spec column below is therefore empty: nothing is proposed.")
    # The live column says ABSENT rather than "----" when protection carries no required-check
    # object at all, so an empty live set can never be read as a row of confirmed checks.
    absent = checks.readable and not checks.configured
    live_miss = "ABSENT" if absent else ("none" if checks.readable else "?")
    if not (live | computed):
        print(f"    (no contexts on either side. live: {checks.describe()})")
    for context in sorted(live | computed):
        left = "live" if context in live else live_miss
        right = "spec" if context in computed else "----"
        print(f"    [{left:>6}/{right}] {context}")

    if checks.gates_nothing:
        _warn(f"'main' requires NO status checks at all. Live state: {checks.describe()}.")
        print("         No CI result gates a merge on this branch today. If that is deliberate,")
        print(
            "         REQUIRED_JOBS must be empty: every id left in it would be ADDED by a write."
        )

    dropped = live - computed
    if dropped:
        _warn(f"Live checks this spec would DROP: {sorted(dropped)}")
        print("         Running this script would REDUCE protection. Fix REQUIRED_JOBS first.")
    added = computed - live
    if added:
        _warn(f"Checks this spec would ADD: {sorted(added)}")

    if computed:
        unproducible = sorted(computed - producible_contexts())
        if unproducible:
            _warn(f"Required contexts that NO job can report on a pull request: {unproducible}")
            print("         GitHub waits for a required context forever, so writing this spec")
            print("         would block every PR on 'main' permanently. Fix REQUIRED_JOBS first.")

    advisory = sorted(producible_contexts() - computed)
    if advisory:
        _log("Jobs that report on every PR but are not required checks (they cannot block):")
        for context in advisory:
            print(f"    {context}")


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


def protection_payload(workflows: pathlib.Path | None = None) -> dict:
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

    contexts: list[str] | None = None
    derivation_error: ValueError | None = None
    try:
        contexts = required_contexts()
    except ValueError as exc:
        derivation_error = exc

    # `--check` is the diagnostic, so it reports even when the spec cannot be derived — that
    # failure is exactly what the operator needs to see, and hiding the live state behind it
    # would leave them guessing at what protection actually looks like.
    if args.check:
        report_state(args.repo, contexts, derivation_error)
        return 1 if derivation_error is not None else 0

    if derivation_error is not None or contexts is None:
        print(
            f"[error] cannot derive the required status checks: {derivation_error}", file=sys.stderr
        )
        return 1

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

    # The ratchet, in both directions. Removing a live check and adding a check nothing reports are
    # equally destructive, and only one of them used to be caught.
    #
    # DROP direction: never trade a live required check away for a derived one. If protection
    # already requires something this spec does not, the spec is behind, and writing REDUCES
    # the gate.
    live = live_required_checks(args.repo)
    dropped = set(live.contexts) - set(contexts)
    if dropped:
        _warn(f"Branch protection NOT written: this spec would drop {sorted(dropped)}.")
        print("         Add the matching job ids to REQUIRED_JOBS, then re-run.")
        print("         Review with:  python scripts/harden_github.py --check")
        return 1

    # ADD direction: never require a context no job can report. The write is a whole-object PUT
    # with `strict: True`, so one unreportable context leaves every PR on `main` pending forever —
    # and the same PUT sets `enforce_admins: True`, which closes the escape hatch that would let
    # an admin merge past it. This is the failure that follows removing a workflow's pull_request
    # trigger: the required list stops matching anything CI can produce.
    unproducible = sorted(set(contexts) - producible_contexts())
    if unproducible:
        _warn(f"Branch protection NOT written: no job reports {unproducible} on a pull request.")
        print("         With strict: True that would block every PR on 'main' permanently, and")
        print("         the same write sets enforce_admins: True, removing the way back out.")
        print("         Fix REQUIRED_JOBS (or the workflow triggers), then re-run.")
        print("         Review with:  python scripts/harden_github.py --check")
        return 1

    if live.gates_nothing and contexts:
        _log(f"'main' currently requires no status checks; this write adds {len(contexts)}.")

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
        report_state(args.repo, contexts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
