"""Pin poodle.toml's runner command against regressing into a no-op filter.

poodle (docs/decisions/0013-windows-local-mutation-testing.md) reruns pytest, via
``[poodle.runner_opts] command_line``, over a temp copy of the tree once per mutant. The two
live-Godot integration suites (``test_selftest_live.py``, ``test_dogfood_gdunit4.py``) are
env-gated by ``GDMUTANT_GODOT`` / ``GDMUTANT_GDUNIT4_CLONE`` (see their own module docstrings and
``pytestmark = pytest.mark.skipif(...)``) -- not by a pytest *mark*. This repo registers no
``real_godot`` / ``real_gdunit4`` / ``live`` marks anywhere. An earlier version of
``command_line`` tried to exclude the two suites with ``-m "not real_godot and not real_gdunit4
and not live"``, which pytest silently treats as excluding nothing (an unknown mark named in a
``-m`` expression matches no test). On a machine that already has those env vars set for other
Godot work, that no-op let a mutation sweep shell out to real Godot once per mutant -- exactly the
slow/flaky behavior the diff-scoped hook (``scripts/check_mutation_baseline.py``) exists to avoid.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POODLE_TOML = REPO_ROOT / "poodle.toml"
LIVE_GODOT_TEST_FILES = ("tests/test_selftest_live.py", "tests/test_dogfood_gdunit4.py")


def _command_line() -> str:
    text = POODLE_TOML.read_text(encoding="utf-8")
    match = re.search(r'command_line\s*=\s*"((?:[^"\\]|\\.)*)"', text)
    assert match, "poodle.toml's [poodle.runner_opts] command_line not found"
    return match.group(1)


def test_command_line_does_not_filter_on_the_unregistered_marks() -> None:
    # Regression guard for the exact bug this file's docstring describes: these marks are not
    # registered anywhere in this repo, so an `-m` expression naming any of them is a silent no-op
    # that excludes nothing. Match the `-m`/`--mark` flag itself (word-bounded), not a bare
    # substring search for "live" -- that also matches inside "test_selftest_live.py", a file name
    # this command line legitimately (and correctly) does mention via --ignore.
    command = _command_line()
    assert not re.search(r"(?:^|\s)(-m|--mark(?:expr)?)\b", command), (
        "poodle.toml's command_line uses a pytest `-m` mark filter again -- this repo registers "
        "no real_godot/real_gdunit4/live marks anywhere, so any `-m` expression naming them is a "
        "silent no-op (see this file's docstring). Exclude the live-Godot suites by path "
        "(--ignore=...) instead."
    )


def test_command_line_ignores_both_live_godot_suites() -> None:
    command = _command_line()
    for path in LIVE_GODOT_TEST_FILES:
        assert f"--ignore={path}" in command, (
            f"poodle.toml's command_line does not --ignore {path}, so a mutation sweep on a "
            "machine with GDMUTANT_GODOT/GDMUTANT_GDUNIT4_CLONE set would invoke real Godot "
            "once per mutant."
        )


def test_the_ignored_files_are_exactly_the_env_gated_live_suites() -> None:
    # If a third live-Godot suite is ever added, or one of these two stops being env-gated, this
    # pin needs to move with it -- fail loudly rather than silently under- or over-excluding.
    for rel in LIVE_GODOT_TEST_FILES:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "skipif" in src and ("GDMUTANT_GODOT" in src or "GDMUTANT_GDUNIT4_CLONE" in src), (
            f"{rel} no longer looks env-gated by GDMUTANT_GODOT/GDMUTANT_GDUNIT4_CLONE -- "
            "poodle.toml's --ignore list (and this test) needs to be reconsidered alongside it."
        )


def test_collection_excludes_the_live_suites_even_with_their_env_vars_set() -> None:
    """Empirical, not just textual: actually collect with poodle's own --ignore flags and fake
    live-Godot env vars set, and assert neither live suite is collected.

    Reproduces the bug directly: with the old `-m "not real_godot and not ..."` filter and these
    same env vars, `test_selftest_live.py` was collected and RUN, and blew up trying to exec
    `/fake/godot` (FileNotFoundError). `--collect-only` keeps this fast while still exercising the
    real pytest collection path poodle's per-mutant runs go through.
    """
    command = _command_line()
    # Strip poodle's own template placeholder -- pythonpath isn't needed for a plain collection
    # run from the repo root, and the literal `{PYTHONPATH}` text would otherwise be passed as a
    # bogus argument.
    args = [
        part for part in command.split() if not part.startswith("-o") and "pythonpath" not in part
    ]
    env = os.environ.copy()
    env["GDMUTANT_GODOT"] = "/fake/godot-should-never-be-invoked"
    env["GDMUTANT_GDUNIT4_CLONE"] = "/fake/clone-should-never-be-read"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args[1:]],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in LIVE_GODOT_TEST_FILES:
        assert path not in result.stdout, (
            f"{path} was collected even with poodle.toml's --ignore flags and its live-Godot env "
            f"var set -- the exclusion regressed.\n{result.stdout}"
        )
