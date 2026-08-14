#!/usr/bin/env python3
"""Run gitleaks's pre-commit secret scan if gitleaks is installed; otherwise degrade to a loud,
unmissable warning (still exit 0) instead of hard-failing the whole pre-commit run (found live in
review: a fresh clone with no local gitleaks binary crashed the pre-commit-stage hook entirely).

Still exit 0 on purpose, not a hard requirement: this repo's own header comment on
`.pre-commit-config.yaml` already explains why a hook that can reliably block a contributor gets
bypassed with `--no-verify` instead, which skips EVERY hook, not just this one. That header also
explains the other half of why exit 0 is safe here: `ci.yml`'s `Secret scan (gitleaks)` job runs on
every pull request and push to `main` (triggers restored 2026-08-04, ADR-0012's Correction) and is
a required branch-protection status check on `main` — the real, unbypassable gate every contributor
gets regardless of whether they've installed these local hooks. This script is a fast, optional,
catches-it-earlier convenience layer on top of that, not the only gate. Even so, silently exiting 0
with a one-line note a contributor can easily read as "scanned, clean" is exactly the "gate that
passes without checking anything" shape AGENTS.md calls out — so this stays non-blocking, but the
warning is loud enough that skipping it has to be a conscious choice, not something that blends
into normal hook output.
"""

import shutil
import subprocess
import sys

GITLEAKS_INSTALL_DOCS = "https://github.com/gitleaks/gitleaks#installing"

_NOT_SCANNED_WARNING = f"""
{"!" * 78}
gitleaks NOT FOUND on PATH -- THIS COMMIT WAS NOT SCANNED LOCALLY FOR SECRETS.
Install it ({GITLEAKS_INSTALL_DOCS}), then re-run `pre-commit run gitleaks`
to confirm it works. Cloud CI's "Secret scan (gitleaks)" job still gates every pull request
(a required branch-protection check on main) -- but don't mistake this message for a clean
local scan, and don't rely on the cloud gate alone to catch a secret before it's committed.
{"!" * 78}
"""


def main() -> int:
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        print(_NOT_SCANNED_WARNING, file=sys.stderr)
        return 0
    return subprocess.run(
        [gitleaks, "git", "--pre-commit", "--redact", "--staged", "--verbose"]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
