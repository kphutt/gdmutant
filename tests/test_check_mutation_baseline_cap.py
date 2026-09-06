"""Tests for the mutant cap in `scripts/check_mutation_baseline.py`.

PR #186's diff included gdmutant/engine/survivor_reference.py, almost entirely string literals,
and the resulting poodle run took about fifty minutes and produced no output before the
environment killed it. The fix caps the total mutant count and prints exactly what got dropped
instead of running long and silent. These tests pin the two pieces that decide the cap's
behaviour: resolving the configured limit, and choosing which changed files fit inside it.

Loaded by path (like test_check_release_tag.py) rather than imported as a package: scripts/ has
no __init__.py, and this module is invoked directly by pre-commit and pytest, not imported
elsewhere.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_mutation_baseline.py"
_spec = importlib.util.spec_from_file_location("check_mutation_baseline", _SCRIPT)
assert _spec and _spec.loader
check_mutation_baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mutation_baseline)


# --- resolve_max_mutants -----------------------------------------------------------------------


def test_default_is_used_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(check_mutation_baseline.MAX_MUTANTS_ENV_VAR, raising=False)
    assert (
        check_mutation_baseline.resolve_max_mutants(None)
        == check_mutation_baseline.DEFAULT_MAX_MUTANTS
    )


def test_env_var_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(check_mutation_baseline.MAX_MUTANTS_ENV_VAR, "40")
    assert check_mutation_baseline.resolve_max_mutants(None) == 40


def test_command_line_overrides_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(check_mutation_baseline.MAX_MUTANTS_ENV_VAR, "40")
    assert check_mutation_baseline.resolve_max_mutants(999) == 999


def test_an_unparsable_env_var_falls_back_to_the_default_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(check_mutation_baseline.MAX_MUTANTS_ENV_VAR, "not-a-number")
    assert (
        check_mutation_baseline.resolve_max_mutants(None)
        == check_mutation_baseline.DEFAULT_MAX_MUTANTS
    )


# --- select_files_within_cap --------------------------------------------------------------------


def test_everything_fits_when_the_total_is_under_the_cap() -> None:
    files = ["a.py", "b.py"]
    counts = {"a.py": 10, "b.py": 20}
    included, skipped = check_mutation_baseline.select_files_within_cap(files, counts, 100)
    assert sorted(included) == files
    assert skipped == []


def test_the_smallest_files_are_kept_and_the_rest_are_named_as_skipped() -> None:
    # PR #186's own shape: one huge string-literal file alongside ordinary small ones. The small
    # ones should still get checked, and the huge one should be named, not silently dropped.
    files = ["small_a.py", "small_b.py", "huge_strings.py"]
    counts = {"small_a.py": 10, "small_b.py": 15, "huge_strings.py": 500}
    included, skipped = check_mutation_baseline.select_files_within_cap(files, counts, 30)
    assert included == ["small_a.py", "small_b.py"]
    assert skipped == [("huge_strings.py", 500)]


def test_a_single_file_over_the_whole_cap_is_skipped_outright_not_run_partially() -> None:
    files = ["huge.py"]
    counts = {"huge.py": 500}
    included, skipped = check_mutation_baseline.select_files_within_cap(files, counts, 100)
    assert included == []
    assert skipped == [("huge.py", 500)]


def test_a_file_missing_from_counts_is_treated_as_zero_mutants_not_an_error() -> None:
    files = ["untracked.py"]
    included, skipped = check_mutation_baseline.select_files_within_cap(files, {}, 10)
    assert included == ["untracked.py"]
    assert skipped == []


# --- count_mutants_per_file (integration: talks to real poodle through `uv run`) -----------------


def test_count_mutants_per_file_matches_a_real_poodle_generation_pass() -> None:
    # gdmutant/engine/spans.py is small and stable enough to pin an exact count against poodle's
    # own mutant generation (AST parsing only, no test execution) -- if a poodle upgrade changes
    # its mutators, this fails here instead of the cap silently under- or over-counting in the
    # hook.
    counts = check_mutation_baseline.count_mutants_per_file(["gdmutant/engine/spans.py"])
    assert counts.keys() == {"gdmutant/engine/spans.py"}
    assert counts["gdmutant/engine/spans.py"] > 0


# --- main: the zero-mutant case ------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_no_files_changed_still_passes() -> None:
    # Short-circuits before any counting, so this must stay a clean pass.
    with mock.patch.object(check_mutation_baseline, "changed_gdmutant_files", return_value=[]):
        assert check_mutation_baseline.main([]) == 0


def test_zero_measured_mutants_on_changed_files_fails_instead_of_reporting_a_clean_pass() -> None:
    # Files changed, but poodle's own mutant generation found nothing to mutate in any of them.
    # Running poodle here would print "0 survivors out of 0 mutants" -- a passing score that
    # measured nothing. That must not report the same clean exit code as a run that measured
    # everything and found no survivors.
    with (
        mock.patch.object(
            check_mutation_baseline,
            "changed_gdmutant_files",
            return_value=["gdmutant/engine/spans.py"],
        ),
        mock.patch.object(
            check_mutation_baseline,
            "count_mutants_per_file",
            return_value={"gdmutant/engine/spans.py": 0},
        ),
        mock.patch.object(
            check_mutation_baseline.subprocess,
            "run",
            return_value=_FakeResult(0, "no mutants found"),
        ),
    ):
        rc = check_mutation_baseline.main([])
    assert rc != 0, f"zero measured mutants on changed files must not report a clean pass, got {rc}"


def test_every_file_over_the_cap_individually_still_fails_instead_of_a_clean_pass() -> None:
    # Same "gate that passes without checking anything" shape as the zero-measured-mutants case
    # above, reached from the sibling branch: every changed file alone exceeds --max-mutants, so
    # select_files_within_cap() skips all of them, files ends up empty, and poodle never runs.
    # That must not report the same clean exit code as a run that actually measured something.
    with (
        mock.patch.object(
            check_mutation_baseline,
            "changed_gdmutant_files",
            return_value=["huge.py"],
        ),
        mock.patch.object(
            check_mutation_baseline,
            "count_mutants_per_file",
            return_value={"huge.py": 500},
        ),
    ):
        rc = check_mutation_baseline.main(["--max-mutants", "50"])
    assert rc != 0, (
        f"every changed file exceeding the cap individually must not report a clean pass "
        f"(nothing was measured), got {rc}"
    )


def test_a_real_nonzero_mutant_run_reported_clean_by_poodle_still_passes() -> None:
    # Same shape, but with a real nonzero mutant count and poodle reporting a clean run: the
    # ordinary passing case must still pass. Guards against an over-broad fix that fails every run.
    with (
        mock.patch.object(
            check_mutation_baseline,
            "changed_gdmutant_files",
            return_value=["gdmutant/engine/spans.py"],
        ),
        mock.patch.object(
            check_mutation_baseline,
            "count_mutants_per_file",
            return_value={"gdmutant/engine/spans.py": 5},
        ),
        mock.patch.object(
            check_mutation_baseline.subprocess,
            "run",
            return_value=_FakeResult(0, "poodle report: 5 mutants, 0 survivors"),
        ),
    ):
        assert check_mutation_baseline.main([]) == 0
