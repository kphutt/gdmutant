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
LIVE_GODOT_TEST_FILES = (
    "tests/test_selftest_live.py",
    "tests/test_dogfood_gdunit4.py",
    "tests/test_selftest_parallel_determinism.py",
)

#: How a module says "I need a real Godot (or a real gdUnit4 checkout) to do anything": it
#: reads the environment variable that names one. `_discovered_live_suites` looks for exactly
#: this, so the list above cannot quietly fall behind the tests/ directory.
_READS_A_LIVE_ENV_VAR = re.compile(r'os\.environ\.get\(\s*"GDMUTANT_(?:GODOT|GDUNIT4_CLONE)"')


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


def _discovered_live_suites() -> set[str]:
    """Every test module that reads a live-Godot environment variable, found by reading tests/.

    The list above is a pin, and a pin is only as good as something noticing when reality moves
    past it. A third live suite added later is invisible to a pin: it simply is not in it, and the
    sweep starts booting Godot once per mutant with nothing failing. So the directory is asked.
    """
    found = set()
    for module in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        if _READS_A_LIVE_ENV_VAR.search(module.read_text(encoding="utf-8")):
            found.add(f"tests/{module.name}")
    return found


def test_the_pin_names_every_live_suite_in_the_tests_directory() -> None:
    discovered = _discovered_live_suites()
    assert discovered == set(LIVE_GODOT_TEST_FILES), (
        "the live-Godot suites in tests/ and poodle.toml's --ignore list have drifted apart. "
        f"found in tests/: {sorted(discovered)}; pinned here: {sorted(LIVE_GODOT_TEST_FILES)}. "
        "Add the new suite to both, or a mutation sweep on a machine with GDMUTANT_GODOT set "
        "will invoke real Godot once per mutant."
    )


def test_the_ignored_files_are_exactly_the_env_gated_live_suites() -> None:
    # The other direction from the test above: everything pinned here still has to BE a live suite.
    # One that stops being env-gated belongs back in the ordinary sweep, and leaving it ignored
    # would quietly take real tests out of every mutation run.
    #
    # Two spellings count as gated, because the two kinds of live suite need different ones. A
    # module-level `skipif` skips the file wherever it is collected. A gate whose whole job is to
    # measure a defect cannot do that: asked for by name with its environment unset, it has to
    # fail, so it decides in a fixture (`pytest.skip`/`pytest.fail`) instead of a mark.
    for rel in LIVE_GODOT_TEST_FILES:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        gated = "skipif" in src or "pytest.skip(" in src
        assert gated and ("GDMUTANT_GODOT" in src or "GDMUTANT_GDUNIT4_CLONE" in src), (
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
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in LIVE_GODOT_TEST_FILES:
        assert path not in result.stdout, (
            f"{path} was collected even with poodle.toml's --ignore flags and its live-Godot env "
            f"var set -- the exclusion regressed.\n{result.stdout}"
        )
