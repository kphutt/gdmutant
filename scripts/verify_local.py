#!/usr/bin/env python3
"""Run one of ci.yml's jobs locally, on this machine's OS, by reading its steps out of the
workflow file rather than restating them.

`files/global-conventions.md` requires local checks to mirror CI 1:1, "the same commands,
never a reimplementation, so the two cannot drift." A hand-copied list of commands drifts
the first time someone edits ci.yml and forgets to update this file; a list parsed from
ci.yml cannot.

Originally written for `verify` alone (Windows is no longer part of the CI matrix — Actions
cost — so the maintainer's Windows machine runs this instead, reproducing what
`Verify (windows-2025)` used to do in CI). Generalized to `--job` so the same mechanism also
backs `publish.yml`'s release-time gate, which runs `verify` and `license-check` live rather
than trusting a separate `ci.yml` run happened in the cloud — see ADR-0012.

Usage, from the repo root:

    uv run python scripts/verify_local.py                        # run `verify`
    uv run python scripts/verify_local.py --job license-check     # run a different job
    uv run python scripts/verify_local.py --list                  # show the steps, run nothing

Exit code is 0 only if every step passed.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def load_steps(job: str) -> list[dict]:
    """Return `job`'s shell steps, in order, straight from ci.yml."""
    try:
        import yaml
    except ImportError:
        sys.exit(
            "PyYAML is required to read ci.yml.\n"
            "Run this through the project environment: uv run python scripts/verify_local.py"
        )

    if not WORKFLOW.is_file():
        sys.exit(f"Workflow not found: {WORKFLOW}")

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    try:
        raw_steps = workflow["jobs"][job]["steps"]
    except (KeyError, TypeError):
        sys.exit(
            f"Could not find jobs.{job}.steps in {WORKFLOW.name}. "
            "If the job was renamed, pass the new name via --job — do not copy its commands here."
        )

    # Only `run:` steps are reproducible locally. `uses:` steps (checkout, setup-python,
    # setup-uv) provision a fresh runner; on a dev machine that toolchain already exists.
    steps = []
    for step in raw_steps:
        if not isinstance(step, dict) or "run" not in step:
            continue
        steps.append(
            {
                "name": step.get("name") or step["run"].strip().splitlines()[0],
                "run": step["run"].strip(),
                "shell": step.get("shell"),
            }
        )
    if not steps:
        sys.exit(f"No `run:` steps found in jobs.{job} — refusing to report success on nothing.")
    return steps


def run_step(step: dict) -> bool:
    """Execute one step. Honours an explicit `shell: bash`, which CI relies on."""
    command = step["run"]

    # ci.yml pins `shell: bash` on the multi-command lint step for a real reason: GitHub's
    # default Windows shell (pwsh) does not stop on a native command's non-zero exit, so a
    # `ruff check` failure could be masked by a passing `ruff format --check`. cmd.exe has
    # the same hazard, so honour that request here rather than quietly using the OS default.
    if step["shell"] == "bash":
        bash = shutil.which("bash")
        if not bash:
            print(
                "  SKIPPED — this step requires bash and none is on PATH.\n"
                "  On Windows, Git Bash provides it. Not treating a skip as a pass.",
                file=sys.stderr,
            )
            return False
        proc = subprocess.run([bash, "-euo", "pipefail", "-c", command], cwd=REPO_ROOT)
    else:
        proc = subprocess.run(command, cwd=REPO_ROOT, shell=True)

    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", default="verify", help="the ci.yml job to run (default: verify)")
    parser.add_argument("--list", action="store_true", help="print the steps and exit")
    args = parser.parse_args()

    steps = load_steps(args.job)

    print(f"{args.job} — {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"steps read from {WORKFLOW.relative_to(REPO_ROOT).as_posix()} :: jobs.{args.job}\n")

    if args.list:
        for i, step in enumerate(steps, 1):
            print(f"{i}. {step['name']}")
            for line in step["run"].splitlines():
                print(f"     {line}")
        return 0

    failed: list[str] = []
    for i, step in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {step['name']}")
        if not run_step(step):
            failed.append(step["name"])
            print(f"  FAILED: {step['name']}\n")
        else:
            print("  ok\n")

    print("-" * 60)
    if failed:
        print(f"FAILED ({len(failed)}/{len(steps)}):")
        for name in failed:
            print(f"  - {name}")
        return 1

    print(f"All {len(steps)} steps of jobs.{args.job} passed on {platform.system()}.")
    if args.job == "verify" and platform.system() == "Windows":
        print("This is the coverage that `Verify (windows-2025)` used to provide in CI.")
    elif args.job == "verify":
        print(
            "Note: CI already runs these on Linux. "
            "Run this on Windows for the coverage CI no longer has."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
