#!/usr/bin/env python3
"""GitHub repo settings as config-as-code — not hand-clicked in the web UI.

Idempotent: re-run any time to converge the remote repo settings to this spec. Each setting is
applied independently and tolerates plan-tier limits (some private-repo protections need a paid plan
— those are reported, not fatal). Re-run after upgrading the plan to pick them up.

Usage: python scripts/harden_github.py [owner/repo]
"""

import json
import subprocess
import sys


def _ok(msg: str) -> None:
    print(f"\033[1;32m[ok]\033[0m   {msg}")


def _warn(msg: str) -> None:
    print(f"\033[1;33m[skip]\033[0m {msg}")


def _log(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def _gh(args: list[str], *, stdin: str | None = None) -> bool:
    """Run ``gh <args>``, silencing its output; return True on exit 0, False otherwise.

    Never raises: a failed setting is reported (a warn), not fatal — matching the old script's
    ``set -uo pipefail`` (no ``-e``), which applied each setting independently.
    """
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0


def main(argv: list[str]) -> int:
    repo = argv[1] if len(argv) > 1 else "kphutt/gdmutant"

    _log(f"Hardening {repo}")

    # --- Merge hygiene: squash-only, auto-delete merged branches ---------------
    if _gh(
        [
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}",
            "-F",
            "delete_branch_on_merge=true",
            "-F",
            "allow_squash_merge=true",
            "-F",
            "allow_merge_commit=false",
            "-F",
            "allow_rebase_merge=false",
            "-F",
            "allow_auto_merge=true",
        ]
    ):
        _ok("Merge hygiene: squash-only, delete-branch-on-merge, auto-merge allowed.")
    else:
        _warn("Could not set merge hygiene (permissions?).")

    # --- Actions: default token read-only --------------------------------------
    if _gh(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/actions/permissions/workflow",
            "-F",
            "default_workflow_permissions=read",
            "-F",
            "can_approve_pull_request_reviews=false",
        ]
    ):
        _ok("Actions default token set read-only.")
    else:
        _warn("Could not set Actions token permissions.")

    # --- Dependabot alerts + automated security fixes --------------------------
    if _gh(["api", "-X", "PUT", f"repos/{repo}/vulnerability-alerts"]):
        _ok("Dependabot vulnerability alerts enabled.")
    else:
        _warn("Could not enable vulnerability alerts.")
    if _gh(["api", "-X", "PUT", f"repos/{repo}/automated-security-fixes"]):
        _ok("Dependabot automated security fixes enabled.")
    else:
        _warn("Could not enable automated security fixes.")

    # --- Secret scanning + push protection (needs GHAS on private repos) -------
    secret_scanning = (
        '{"security_and_analysis":{"secret_scanning":{"status":"enabled"},'
        '"secret_scanning_push_protection":{"status":"enabled"}}}'
    )
    if _gh(["api", "-X", "PATCH", f"repos/{repo}", "--raw-field", secret_scanning]):
        _ok("Native secret scanning + push protection enabled.")
    else:
        _warn(
            "Secret scanning/push protection unavailable (private repos need GitHub Advanced "
            "Security). gitleaks in CI is the backstop."
        )

    # --- Branch protection on main (needs a paid plan for PRIVATE repos) --------
    # Require PR + the CI status checks, block force-push/deletion, admins included.
    # Solo repo => required approving reviews = 0 (self-approval is impossible; a
    # nonzero count would deadlock). CODEOWNERS review stays advisory.
    protection = {
        "required_status_checks": {
            "strict": True,
            "contexts": ["Verify", "Secret scan (gitleaks)", "Self-test (real Godot)"],
        },
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
    if _gh(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/branches/main/protection",
            "-H",
            "Accept: application/vnd.github+json",
            "--input",
            "-",
        ],
        stdin=json.dumps(protection),
    ):
        _ok("Branch protection on 'main': PR required, CI checks required, no force-push.")
    else:
        _warn("Branch protection NOT set — private-repo protection needs a paid GitHub plan.")
        _warn("  Until then, PR discipline is convention-only. Upgrade, then re-run this script.")

    _log(
        f"Done. Review live settings:  gh api repos/{repo} "
        "--jq '{private,delete_branch_on_merge}'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
