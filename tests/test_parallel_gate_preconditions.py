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

import pytest

from tests import test_selftest_parallel_determinism as gate
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


# --- the skip says so out loud, the way the vocabulary guard's skip does -------------------------


class _Config:
    """Just enough of `pytest.Config` for `_require_preconditions`: the args pytest was run with."""

    class invocation_params:  # noqa: N801 - mirrors pytest's attribute name
        args: tuple[str, ...] = ("tests",)


class _Request:
    config = _Config()


def test_a_skipped_gate_also_raises_a_warning_so_a_quiet_run_still_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest hides the report header under `-q`, the mode poodle and a quick local run use, so the
    # header alone leaves this skip invisible while the vocabulary guard's skip stays loud through
    # a UserWarning. A warning lands in the summary at every verbosity.
    monkeypatch.setattr(gate, "_GODOT", None)
    monkeypatch.setattr(gate, "GDUNIT4_ADDON", Path("no-such-dir") / "gdUnit4")
    with (
        pytest.warns(UserWarning, match="cannot check anything"),
        pytest.raises(pytest.skip.Exception),
    ):
        gate._require_preconditions(_Request())  # type: ignore[arg-type]


# --- the inertness proof cannot pass by reading nothing ------------------------------------------


def _project(tmp_path: Path, other: dict[str, str]) -> Path:
    (tmp_path / gate._PROBE_TARGET).write_text(gate._PROBE_SOURCE, encoding="utf-8")
    for name, text in other.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_the_proof_passes_on_a_project_that_never_names_the_probe(tmp_path: Path) -> None:
    gate._unreferenced_probe(_project(tmp_path, {"other.gd": "extends Node\n"}))


def test_the_proof_names_a_file_that_references_the_probe(tmp_path: Path) -> None:
    project = _project(tmp_path, {"caller.gd": "GdmutantParallelProbe.gdmutant_probe_above(1, 2)"})
    with pytest.raises(AssertionError, match=r"caller\.gd"):
        gate._unreferenced_probe(project)


def test_the_proof_fails_when_it_scanned_no_other_file(tmp_path: Path) -> None:
    # Only the probe and files of a skipped type: nothing was searched, so "no offenders" is empty.
    project = _project(tmp_path, {"icon.png": "not text"})
    with pytest.raises(AssertionError, match="scanned nothing"):
        gate._unreferenced_probe(project)


def test_the_proof_fails_when_its_tokens_cannot_find_the_probe_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The positive control: the probe's own file must light the search up, or a hit elsewhere
    # could never have been seen.
    monkeypatch.setattr(gate, "_PROBE_TOKENS", ())
    with pytest.raises(AssertionError, match="control"):
        gate._unreferenced_probe(_project(tmp_path, {"other.gd": "extends Node\n"}))
