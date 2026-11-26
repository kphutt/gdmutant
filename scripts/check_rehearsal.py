#!/usr/bin/env python3
"""Fail unless the release rehearsal passed on the exact commit being released.

`.github/workflows/rehearse-release.yml` walks the release path on every merge to main. Until this
guard existed it was advisory, and 0.1.7 shipped from a commit whose rehearsal was red: the
rehearsal had caught a real bug the release gate's own suite did not. Both release.yml and
publish.yml call this with the released commit, so a red, cancelled or missing rehearsal stops a
release on either path.

A rehearsal still running is waited for, up to ``--wait-minutes``. Anything else that is not a
success fails, including no run at all and an API answer this cannot read: a guard that passes
when it saw nothing is the bug this repository keeps finding.

The usual reason for no successful run is that a later merge cancelled it (the workflow cancels a
superseded run on the same branch). Re-run that run, which keeps its commit, then re-run the job
this guard failed in. The refusal names the run:

    gh run rerun <run-id>

Rehearsing the tag instead cannot pass: on the tag's tree the version is already tagged and the
changelog already dated, so the rehearsal's runbook edits refuse it.

Usage::

    python3 scripts/check_rehearsal.py <commit-sha> [--repo owner/name] [--wait-minutes N]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

WORKFLOW = "rehearse-release.yml"

#: Statuses GitHub reports for a run that has not finished yet.
UNFINISHED = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

PASS, WAIT, FAIL = "pass", "wait", "fail"


def decide(runs: list[dict[str, Any]], sha: str) -> tuple[str, str]:
    """The verdict for `sha` from its rehearsal runs: pass, wait, or fail, with the reason."""
    mine = [run for run in runs if run.get("head_sha") == sha]
    if any(run.get("conclusion") == "success" for run in mine):
        return PASS, f"the release rehearsal passed on {sha}"
    if any(run.get("status") in UNFINISHED for run in mine):
        return WAIT, f"the release rehearsal on {sha} is still running"
    refused = f"no successful release rehearsal for {sha}. A release needs one on its exact commit."
    if not mine:
        return FAIL, (
            f"{refused} No run exists to re-run. Before tagging, `gh workflow run "
            "rehearse-release.yml --ref main` rehearses main's tip. Once the tag exists that "
            "cannot pass (the version is already tagged), so release a later commit instead."
        )
    latest = max(mine, key=lambda run: int(run.get("id", 0)))
    return FAIL, (
        f"{refused} Run {latest.get('id')} ended {latest.get('conclusion')}. Re-run it with "
        f"`gh run rerun {latest.get('id')}` (a re-run keeps this commit), then re-run this job. "
        "If it fails again, the release path is broken at this commit."
    )


def fetch_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    """Every rehearsal run GitHub has for `sha`. Raises on anything it cannot read."""
    out = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&per_page=100",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # a stray byte must not lose the whole answer
        check=True,
    ).stdout
    runs = json.loads(out)["workflow_runs"]
    if not isinstance(runs, list):
        raise ValueError("workflow_runs is not a list")
    return runs


def check(
    sha: str,
    repo: str,
    wait_minutes: float,
    fetch: Callable[[str, str], list[dict[str, Any]]] = fetch_runs,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 30,
) -> int:
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        try:
            verdict, reason = decide(fetch(repo, sha), sha)
        except (
            OSError,
            subprocess.CalledProcessError,
            ValueError,
            LookupError,
            TypeError,
            AttributeError,
        ) as exc:
            print(f"::error::could not read the release rehearsal's runs: {exc}")
            return 1
        if verdict == PASS:
            print(reason)
            return 0
        if verdict == FAIL or time.monotonic() >= deadline:
            print(f"::error::{reason}")
            return 1
        print(f"{reason}, checking again in {poll_seconds:.0f}s")
        sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="the full commit SHA being released")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--wait-minutes", type=float, default=20)
    args = parser.parse_args(argv)
    if len(args.sha) != 40 or not args.repo:
        print("::error::need a full 40-character commit SHA and --repo (or GITHUB_REPOSITORY)")
        return 2
    return check(args.sha, args.repo, args.wait_minutes)


if __name__ == "__main__":
    sys.exit(main())
