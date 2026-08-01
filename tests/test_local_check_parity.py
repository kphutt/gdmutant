"""Pin that scripts/verify_local.py's parsed commands and .pre-commit-config.yaml's hooks agree.

Both read from the same intent (`ci.yml`'s `verify`/`license-check` jobs) but through two
independent paths: `verify_local.py` parses the job's `run:` steps out of the YAML, and
`.pre-commit-config.yaml` restates the same commands as separate hooks (raised in review on
2026-07-29 — verify_local.py's single dynamic read couldn't itself catch the config drifting
away from it). This is the cheap alternative to unifying them onto one mechanism: as long as
this test passes, the duplication is provably harmless; the moment it doesn't, that's the
signal to stop trusting either copy until they're reconciled.

The one command allowed to differ is the test suite, which runs at the `manual` stage rather than
at `pre-push`. That is not a hole in the mirror, it is a named exemption with a written reason and
a check of its own: see `DEFERRED_TO_MANUAL_STAGE` and
`test_every_deferred_command_still_runs_at_the_manual_stage` below.

Also guards the specific failure mode a workflow-YAML parser is exposed to that a real command
never is: a `uses:`-based action step that performs a CHECK (not just environment provisioning)
would be silently skipped by `verify_local.py.load_steps`, which only replays `run:` steps —
reporting "all steps passed" without ever having run it. `ALLOWED_USES_PREFIXES` is the
allow-list of actions known to be pure provisioning (checkout, language/tool setup); anything
else appearing in a job `verify_local.py` is asked to parse fails this test loudly instead of
failing silently at release time.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# Every job scripts/verify_local.py is actually asked to parse today — from the local pre-commit
# hooks (verify, license-check) and publish.yml's live release-time gate (same two). If a future
# release-gate job starts parsing a different ci.yml job, add its name here too.
PARSED_JOBS = ("verify", "license-check")

#: Action name-prefixes that only provision an environment and never themselves constitute a
#: check. Anything else `uses:`d inside a PARSED_JOBS job would be silently invisible to
#: verify_local.py, which only replays `run:` steps.
ALLOWED_USES_PREFIXES = (
    "actions/checkout@",
    "actions/setup-python@",
    "astral-sh/setup-uv@",
)

#: Hooks whose `verify` command deliberately does NOT run at the pre-push stage, keyed by hook id
#: and mapped to why. Same id-based shape as the exclusion list inside `_pre_commit_hook_entries`,
#: pointed the other way: that one drops a HOOK running something `verify` does not, this one drops
#: a `verify` COMMAND that no pre-push hook runs.
#:
#: Every entry here is a real reduction in what `git push` checks, so it earns a written reason and
#: not just a `stages:` edit in the config. The companion test below,
#: `test_every_deferred_command_still_runs_at_the_manual_stage`, pins that the hook is still there
#: and still runs ci.yml's own command, so an exemption can only ever be a move, never a deletion.
DEFERRED_TO_MANUAL_STAGE = {
    "pytest": (
        "the suite is the only slow leg of the chain (35s on the maintainer's machine, 2:40 "
        "measured elsewhere, against six seconds for every other hook combined). That is long "
        "enough to exceed an automated contributor's tool timeout, and a gate that reliably times "
        "out gets bypassed with `--no-verify`, which silently skips every OTHER hook too, the "
        "secret scan included. It still runs unconditionally in publish.yml's release-time gate on "
        "both Linux and Windows, and locally through `uv run python scripts/verify_local.py`."
    ),
}

_spec = importlib.util.spec_from_file_location(
    "verify_local", REPO_ROOT / "scripts" / "verify_local.py"
)
assert _spec and _spec.loader
verify_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_local)


def _ci_jobs() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _pre_commit_hook_entries(stage: str) -> set[str]:
    """Every `run:`-equivalent command a hook at `stage` would execute, flattened.

    `license-check`'s hook wraps verify_local.py rather than restating commands directly (see
    scripts/license_check_local.py) — it is checked separately in
    test_license_check_hook_delegates_to_verify_local, not by string-matching here.
    """
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    default_stages = config.get("default_stages", [])
    entries = set()
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            effective_stages = hook.get("stages", default_stages)
            if stage not in effective_stages:
                continue
            if hook["id"] in ("gitleaks", "mutation", "license-check"):
                continue  # not part of the `verify` job's command set
            entry = hook["entry"]
            # `bash -c "cmd1 && cmd2"` -> {"cmd1", "cmd2"}; a plain entry is one command.
            m = re.match(r'^bash -c "(.*)"$', entry)
            if m:
                entries.update(c.strip() for c in m.group(1).split("&&"))
            else:
                entries.add(entry.strip())
    return entries


@pytest.mark.parametrize("job", PARSED_JOBS)
def test_no_checking_uses_step_is_silently_invisible(job: str) -> None:
    steps = _ci_jobs()[job]["steps"]
    for step in steps:
        if "uses" not in step:
            continue
        action = step["uses"]
        assert any(action.startswith(p) for p in ALLOWED_USES_PREFIXES), (
            f"jobs.{job} in ci.yml has a `uses:` step ({action}) that isn't a known-provisioning "
            "action. scripts/verify_local.py only replays `run:` steps and would silently skip "
            "this one — either add it to ALLOWED_USES_PREFIXES (if it's genuinely just "
            "provisioning) or stop relying on verify_local.py for this job."
        )


def _hook(hook_id: str) -> dict:
    """One hook's config block, by id."""
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook["id"] == hook_id:
                return dict(hook)
    raise AssertionError(f".pre-commit-config.yaml has no hook with id {hook_id!r}")


def _verify_job_commands() -> set[str]:
    commands = set()
    for step in verify_local.load_steps("verify"):
        # A step's `run:` block can hold several newline-joined commands (e.g. the ruff lint
        # step runs `check` then `format --check`); flatten to match the hook granularity.
        commands.update(line.strip() for line in step["run"].splitlines() if line.strip())
    return commands


def _deferred_commands() -> set[str]:
    """The `verify` commands DEFERRED_TO_MANUAL_STAGE excuses, read off the config, not restated.

    Taking the command from the hook rather than writing it out again here means the exemption
    cannot drift from what the hook actually runs: rename the entry and the parity check below
    starts failing, which is the point.
    """
    return {_hook(hook_id)["entry"].strip() for hook_id in DEFERRED_TO_MANUAL_STAGE}


def test_verify_job_commands_match_the_pre_push_hooks() -> None:
    ci_commands = _verify_job_commands()
    hook_commands = _pre_commit_hook_entries("pre-push")

    missing_from_hooks = ci_commands - hook_commands - _deferred_commands()
    extra_in_hooks = hook_commands - ci_commands
    assert not missing_from_hooks, (
        f"ci.yml's verify job runs commands no pre-push hook does: {missing_from_hooks}. "
        "A local `git push` would pass without having run these. If that is deliberate, say so "
        "in DEFERRED_TO_MANUAL_STAGE with the reason, and move the hook to `stages: [manual]` "
        "rather than deleting it."
    )
    assert not extra_in_hooks, (
        f"a pre-push hook runs commands ci.yml's verify job doesn't: {extra_in_hooks} — "
        "either verify_local.py is missing what CI actually checks, or a hook is stale."
    )


def test_every_deferred_command_still_runs_at_the_manual_stage() -> None:
    """An exemption above has to be a move, not a deletion.

    Without this, DEFERRED_TO_MANUAL_STAGE would be a way to delete a check and keep the parity
    test green: drop the hook entirely, name its command here, and nothing complains. So each
    deferred hook must still exist, must run the command `verify` runs, and must stay reachable
    with `pre-commit run --hook-stage manual <id>`.
    """
    ci_commands = _verify_job_commands()
    manual_entries = _pre_commit_hook_entries("manual")
    for hook_id, reason in DEFERRED_TO_MANUAL_STAGE.items():
        hook = _hook(hook_id)
        entry = hook["entry"].strip()
        assert hook.get("stages") == ["manual"], (
            f"hook {hook_id!r} is excused from the pre-push stage but is not a manual-stage hook "
            f"either (stages: {hook.get('stages')}). The exemption and the config now disagree "
            "about where this command runs: give the hook `stages: [manual]`, or drop its "
            "DEFERRED_TO_MANUAL_STAGE entry."
        )
        assert entry in manual_entries, (
            f"hook {hook_id!r} is excused from the pre-push stage but "
            "`_pre_commit_hook_entries('manual')` does not see its command, so nothing here "
            "proves it can still be run."
        )
        assert entry in ci_commands, (
            f"hook {hook_id!r} is excused from the pre-push stage, but {entry!r} is not one of "
            "ci.yml's verify commands at all, so the exemption excuses nothing and is dead "
            "weight. Delete the entry, or fix the hook to run what CI runs."
        )
        assert reason.strip(), f"the DEFERRED_TO_MANUAL_STAGE entry for {hook_id!r} has no reason"


def test_license_check_hook_delegates_to_verify_local() -> None:
    """scripts/license_check_local.py must have no license-checking logic of its own — its only
    job is redirecting verify_local.py's replay into an isolated venv (see its own docstring for
    why). If this ever grows independent logic, it becomes a second implementation of the check
    ci.yml defines, which is exactly the drift class this file exists to catch."""
    src = (REPO_ROOT / "scripts" / "license_check_local.py").read_text(encoding="utf-8")
    assert "verify_local.py" in src, (
        "license_check_local.py no longer delegates to verify_local.py — it may have grown its "
        "own license-check logic, a second definition of what ci.yml's license-check job means."
    )
    assert "pip-licenses" not in src and "check_licenses.py" not in src, (
        "license_check_local.py references the license-check tooling directly instead of only "
        "through verify_local.py --job license-check — that's the second-implementation drift "
        "this test exists to catch."
    )
