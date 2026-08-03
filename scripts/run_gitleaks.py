#!/usr/bin/env python3
"""Run gitleaks's pre-commit secret scan if gitleaks is installed; otherwise degrade to a loud,
unmissable warning (still exit 0) instead of hard-failing the whole pre-commit run (found live in
review: a fresh clone with no local gitleaks binary crashed the pre-commit-stage hook entirely).

Still exit 0 on purpose, not a hard requirement: this repo's own header comment on
`.pre-commit-config.yaml` already explains why a hook that can reliably block a contributor gets
bypassed with `--no-verify` instead, which skips EVERY hook, not just this one. But this repo also
has no automatic cloud CI while private (ADR-0012), which makes this hook the ONLY secret-scan
gate there is — silently exiting 0 with a one-line note a contributor can easily read as "scanned,
clean" is exactly the "gate that passes without checking anything" shape AGENTS.md calls out. So
this stays non-blocking, but the warning is now loud enough that skipping it has to be a conscious
choice, not something that blends into normal hook output.
"""

import shutil
import subprocess
import sys

GITLEAKS_INSTALL_DOCS = "https://github.com/gitleaks/gitleaks#installing"

_NOT_SCANNED_WARNING = f"""
{"!" * 78}
gitleaks NOT FOUND on PATH -- THIS COMMIT WAS NOT SCANNED FOR SECRETS.
Install it ({GITLEAKS_INSTALL_DOCS}), then re-run `pre-commit run gitleaks`
to confirm it works. This repo has no automatic cloud secret scan while private (ADR-0012),
so this hook is the only gate there is -- don't mistake this message for a clean scan.
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
