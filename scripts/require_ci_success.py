#!/usr/bin/env python3
"""Fail unless ``ci.yml`` concluded ``success`` for one exact commit.

This is the *CI half* of the release gate. It exists as a script rather than an inline ``run:``
block for one reason: the same check has to run in two different workflows, and an inline copy in
each is a copy that can drift. ``release.yml`` calls it when a version tag is pushed (early, cheap
feedback); ``publish.yml`` calls it again immediately before the PyPI upload, which is the step
that cannot be undone.

**Why it runs twice.** ``release.yml`` only stages a *draft* Release. Nothing forces a real PyPI
upload to have come from that path: a maintainer can create and publish a GitHub Release directly
in the web UI, which fires ``release: published`` and runs ``publish.yml`` without ``release.yml``
ever executing. A gate that lives only in ``release.yml`` therefore guards the convenient path and
not the irreversible one. The authoritative call is the one in ``publish.yml``.

**Fail closed, on every branch.** An API error, a malformed response, zero matching runs, a run
still queued or in progress, and a ``cancelled``/``failure``/``timed_out`` conclusion all return
non-zero. There is no code path that defaults to permitting a publish.

Usage::

    python3 scripts/require_ci_success.py --repo owner/name --sha <40-hex>
"""

import argparse
import json
import subprocess
import sys

#: The workflow whose conclusion decides the gate. Matched against the API's ``path`` field as
#: well as being the endpoint, so a rename can't quietly satisfy the gate with a different file.
CI_WORKFLOW = ".github/workflows/ci.yml"

#: Only a ``push``-triggered run counts. `main` is squash-merge-only, so every commit that became
#: main's tip produced exactly one such run at that SHA. Excluding `pull_request` stops a fork PR
#: whose head shares this content from satisfying the gate.
EVENT = "push"


def fetch_runs(repo: str, sha: str, workflow: str = CI_WORKFLOW) -> tuple[list[dict] | None, str]:
    """Query the Actions API for `workflow`'s runs at `sha`. Returns (runs, error_message)."""
    result = subprocess.run(
        [
            # -X GET is load-bearing: `gh api` silently switches to POST whenever `-f` params are
            # present unless the method is pinned explicitly. Omitting it 404s instead of listing
            # runs, which would make this gate fail closed for the wrong reason - "API error" on
            # every release, forever, with a message pointing at the wrong problem.
            "gh",
            "api",
            "-X",
            "GET",
            f"repos/{repo}/actions/workflows/{workflow.rsplit('/', 1)[-1]}/runs",
            "-f",
            f"head_sha={sha}",
            "-f",
            f"event={EVENT}",
            "-f",
            "per_page=10",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, (
            f"could not query the Actions API for {workflow} runs on {sha}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)["workflow_runs"], ""
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return None, f"unexpected Actions API response for {workflow} runs on {sha}: {exc!r}"


def matching(runs: list[dict], repo: str, sha: str, workflow: str = CI_WORKFLOW) -> list[dict]:
    """Runs that really are `workflow` at `sha` in `repo`, newest first.

    Defense in depth on top of the query's own filters: restated here so a future change to the
    query can't silently widen what counts as a match.
    """
    kept = [
        run
        for run in runs
        if run.get("path") == workflow
        and run.get("head_sha") == sha
        and run.get("event") == EVENT
        and run.get("repository", {}).get("full_name") == repo
    ]
    # The API returns runs newest-first, but sort explicitly rather than trusting that. A re-run
    # after a failure produces a second run for the same SHA, and it is the MOST RECENT one that
    # reflects the commit's true status - not "any run ever succeeded", which would let an old
    # failure be shadowed, and not "all runs succeeded", which would keep a since-fixed re-run
    # from ever passing.
    kept.sort(key=lambda run: run.get("run_started_at") or "", reverse=True)
    return kept


def verdict(runs: list[dict], sha: str) -> str | None:
    """An error message if `runs` does not prove CI passed at `sha`, else None."""
    if not runs:
        # ASCII only: this string is printed, and gdmutant already shipped a Windows bug where
        # console output crashed under the legacy cp1252 code page. A guard that crashes instead
        # of reporting the problem is worse than no guard.
        return (
            f"no {EVENT}-triggered {CI_WORKFLOW} run found for commit {sha} - refusing to publish "
            "a commit CI never verified"
        )
    latest = runs[0]
    status, conclusion = latest.get("status"), latest.get("conclusion")
    if status != "completed" or conclusion != "success":
        return (
            f"{CI_WORKFLOW} has not concluded success for {sha} "
            f"(status={status!r}, conclusion={conclusion!r}) - refusing to publish"
        )
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", required=True, help="the exact commit to check")
    args = parser.parse_args(argv[1:])

    runs, error = fetch_runs(args.repo, args.sha)
    if runs is None:
        print(f"::error::{error}")
        return 1

    kept = matching(runs, args.repo, args.sha)
    if kept:
        latest = kept[0]
        print(
            f"most recent {EVENT}-triggered {CI_WORKFLOW} run for {args.sha}: "
            f"status={latest.get('status')!r} conclusion={latest.get('conclusion')!r} "
            f"({latest.get('html_url')})"
        )

    problem = verdict(kept, args.sha)
    if problem:
        print(f"::error::{problem}")
        return 1

    print(f"{CI_WORKFLOW} concluded success for {args.sha} - publishing may proceed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
