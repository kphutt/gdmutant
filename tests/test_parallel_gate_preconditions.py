"""The live parallel gate's own fail-rather-than-skip rule, checked without a Godot.

`tests/test_selftest_parallel_determinism.py` needs a real Godot and the GdUnit4 addon. Those are
not on a CI `verify` runner or a fresh clone, so it cannot simply fail when they are missing -- and
a test that silently skips is the shape that gate exists to catch in the first place (AGENTS.md's
recurring bug one: a gate that passes without checking anything). Its rule splits the difference,
and the rule is the part worth testing here, because getting it wrong is invisible: the gate would
go on reporting success while running nothing.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_selftest_parallel_determinism import (
    MODULE_FILE_NAME,
    missing_preconditions,
    named_on_command_line,
)


def test_a_configured_machine_has_nothing_missing(tmp_path: Path) -> None:
    assert missing_preconditions("/usr/bin/godot", tmp_path) == []


def test_an_unset_godot_is_reported_with_the_way_to_set_it(tmp_path: Path) -> None:
    (said,) = missing_preconditions("", tmp_path)
    assert "GDMUTANT_GODOT" in said
    assert "mise which godot" in said


def test_a_missing_addon_is_reported_with_the_script_that_installs_it(tmp_path: Path) -> None:
    (said,) = missing_preconditions("/usr/bin/godot", tmp_path / "gdUnit4")
    assert "install_gdunit4.py" in said


def test_both_missing_are_both_reported(tmp_path: Path) -> None:
    # Not the first one and then silence: somebody fixing a two-step setup should learn both steps
    # from one run.
    assert len(missing_preconditions(None, tmp_path / "gdUnit4")) == 2


def test_naming_the_module_on_the_command_line_counts_as_asking_for_it() -> None:
    for args in (
        [f"tests/{MODULE_FILE_NAME}"],
        [f"tests\\{MODULE_FILE_NAME}", "--no-cov"],
        ["-q", f"tests/{MODULE_FILE_NAME}::test_parallel_runs_agree_with_the_serial_verdicts"],
    ):
        assert named_on_command_line(args, MODULE_FILE_NAME), args


def test_a_whole_suite_run_is_not_asking_for_it() -> None:
    for args in (["tests"], [], ["-q", "--no-cov"], ["-k", "parallel"], ["tests/test_loop.py"]):
        assert not named_on_command_line(args, MODULE_FILE_NAME), args
