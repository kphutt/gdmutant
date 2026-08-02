"""Tests for the branch-protection spec in `scripts/harden_github.py`.

The script is idempotent config-as-code: it PUTs the whole branch-protection object, so a wrong
required-check list is not a stale value that drifts harmlessly — re-running the script overwrites
the live setting with the wrong one. Two ways that goes bad, both fatal to the repo:

* requiring a context no job reports (a bare `Verify` when the matrix reports
  `Verify (ubuntu-24.04)`) leaves every PR pending forever;
* silently shipping a shorter list than the live one quietly REDUCES the merge gate.

So these tests pin the derivation against the repo's real workflow files and pin the ratchet that
refuses to write a reduced set.

`scripts/` is outside the coverage source (`pyproject.toml` sets `source = ["gdmutant"]`), so this
file adds no coverage obligation; the logic earns a test on its own.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "harden_github.py"
_spec = importlib.util.spec_from_file_location("harden_github", _SCRIPT)
assert _spec and _spec.loader
harden_github = importlib.util.module_from_spec(_spec)
# Register before exec: `@dataclass` resolves the defining module out of `sys.modules`, and a
# module loaded straight from a path is not there unless we put it there.
sys.modules["harden_github"] = harden_github
_spec.loader.exec_module(harden_github)


# The exact contexts GitHub would report for the required jobs, derived from THIS repo's workflow
# files. Written out in full so a workflow rename that changes a context has to be reflected here
# deliberately, not absorbed silently.
#
# One entry, because one job qualifies. ADR-0012 moved the merge-time checks to the local
# pre-commit gate and reduced `ci.yml` to `workflow_dispatch`, so nothing in it can report on a
# pull request; `zizmor.yml` kept its unfiltered `pull_request` trigger to stay the one cloud
# check that gates a merge. This is also the one context branch protection requires on `main`
# today, so the derivation and the live setting agree — but the assertion stays offline (the
# derivation is the thing under test, and a test that phoned GitHub would fail on a fork).
EXPECTED_CONTEXTS = ["Workflow security (zizmor)"]


def test_required_contexts_are_derived_from_this_repos_workflows() -> None:
    """The derived list is exactly what this repo's workflows would make GitHub report."""
    assert harden_github.required_contexts() == EXPECTED_CONTEXTS


def test_a_name_with_parentheses_is_the_context_verbatim() -> None:
    """`zizmor` is `name: Workflow security (zizmor)`, and the whole string is the context.

    A plain job's parenthesised name looks exactly like a matrix suffix, so the derivation must
    not treat it as one — trimming it would require a context nothing reports.
    """
    job = harden_github.all_jobs()["zizmor"][0]
    assert job.matrix == {}
    assert job.contexts() == ["Workflow security (zizmor)"]


def test_matrix_job_never_yields_the_bare_job_name() -> None:
    """The regression guard: `verify` in `ci.yml` is a matrix job, so a bare `Verify` never reports.

    `verify` is not a required check any more (its workflow no longer runs on a pull request), but
    the shape that made the old hardcoded list fatal is still in the tree, so the guard stays
    pinned against the real file rather than only against a synthetic one.
    """
    verify = harden_github.all_jobs()["verify"][0]
    assert verify.name == "Verify"
    assert verify.matrix, "verify is a matrix job; a bare `Verify` context can never report"
    contexts = verify.contexts()
    assert "Verify" not in contexts
    assert all(c.startswith("Verify (") for c in contexts)


def test_every_required_job_id_exists_and_always_runs() -> None:
    jobs = harden_github.all_jobs()
    for job_id in harden_github.REQUIRED_JOBS:
        assert job_id in jobs, f"{job_id!r} is required but defined in no workflow"
        assert not jobs[job_id][0].conditional, f"{job_id!r} is if:-gated: it can skip and hang PRs"


def test_path_filtered_workers_are_not_required() -> None:
    """The heavy Godot workers are `if:`-gated, so they must stay out of the required set."""
    jobs = harden_github.all_jobs()
    for worker in ("selftest-godot-run", "selftest-gut-run"):
        assert jobs[worker][0].conditional
        assert worker not in harden_github.REQUIRED_JOBS
        for context in jobs[worker][0].contexts():
            assert context not in harden_github.required_contexts()


def test_publish_and_release_jobs_are_not_required() -> None:
    """`publish.yml` / `release.yml` never run on a pull request, so they can never be required."""
    contexts = harden_github.required_contexts()
    for never_on_a_pr in ("Publish to PyPI", "Create GitHub Release", "Build distributions"):
        assert never_on_a_pr not in contexts


def test_protection_payload_keeps_the_hardening_settings() -> None:
    payload = harden_github.protection_payload()
    assert payload["required_status_checks"]["contexts"] == EXPECTED_CONTEXTS
    assert payload["required_status_checks"]["strict"] is True
    assert payload["enforce_admins"] is True
    assert payload["required_conversation_resolution"] is True
    assert payload["allow_force_pushes"] is False
    assert payload["allow_deletions"] is False


# --------------------------------------------------------------------------------------------
# Parser behaviour, against synthetic workflows
# --------------------------------------------------------------------------------------------


def _workflow(tmp_path: Path, body: str, filename: str = "ci.yml") -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")
    return directory


INLINE_MATRIX = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-24.04, windows-2025]
    runs-on: ${{ matrix.os }}
    steps:
      - name: Tests
        run: pytest
"""

BLOCK_MATRIX = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      matrix:
        os:
          - ubuntu-24.04
          - windows-2025
    runs-on: ${{ matrix.os }}
"""

TWO_DIMENSIONS = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      matrix:
        os: [ubuntu-24.04, windows-2025]
        python: ["3.12", "3.13"]
    runs-on: ${{ matrix.os }}
"""


@pytest.mark.parametrize("body", [INLINE_MATRIX, BLOCK_MATRIX])
def test_matrix_expands_for_both_yaml_list_styles(tmp_path: Path, body: str) -> None:
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["verify"][0].contexts() == ["Verify (ubuntu-24.04)", "Verify (windows-2025)"]


def test_two_matrix_dimensions_expand_in_declaration_order(tmp_path: Path) -> None:
    """GitHub joins the values in the order the dimensions are declared, not alphabetically."""
    jobs = harden_github.all_jobs(_workflow(tmp_path, TWO_DIMENSIONS))
    assert jobs["verify"][0].contexts() == [
        "Verify (ubuntu-24.04, 3.12)",
        "Verify (ubuntu-24.04, 3.13)",
        "Verify (windows-2025, 3.12)",
        "Verify (windows-2025, 3.13)",
    ]


def test_a_step_name_is_not_mistaken_for_the_job_name(tmp_path: Path) -> None:
    """`name:` inside a step sits deeper than the job's own key; only the job's own counts."""
    jobs = harden_github.all_jobs(_workflow(tmp_path, INLINE_MATRIX))
    assert jobs["verify"][0].name == "Verify"


def test_job_without_a_name_reports_under_its_job_id(tmp_path: Path) -> None:
    body = "name: CI\non:\n  pull_request:\njobs:\n  lint:\n    runs-on: ubuntu-24.04\n"
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["lint"][0].contexts() == ["lint"]


def test_trailing_comment_is_stripped_from_a_job_name(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify # the fast gate\n    runs-on: ubuntu-24.04\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["verify"][0].contexts() == ["Verify"]


def test_matrix_include_is_rejected_rather_than_guessed(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-24.04]\n        include:\n          - os: macos-15\n"
    )
    with pytest.raises(ValueError, match="include"):
        harden_github.all_jobs(_workflow(tmp_path, body))


def test_templated_job_name_is_rejected(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-24.04]\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    with pytest.raises(ValueError, match="templated name"):
        jobs["verify"][0].contexts()


def test_if_always_still_counts_as_always_running(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  gate:\n    name: Gate\n    if: always()\n    runs-on: ubuntu-24.04\n"
        "  worker:\n    name: Worker\n    if: needs.changes.outputs.godot == 'true'\n"
        "    runs-on: ubuntu-24.04\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["gate"][0].conditional is False
    assert jobs["worker"][0].conditional is True


def test_a_missing_required_job_raises_instead_of_dropping_the_check(tmp_path: Path) -> None:
    """A renamed job id must fail loudly. Silently dropping it would shrink the merge gate."""
    body = "name: CI\non:\n  pull_request:\njobs:\n  something-else:\n    name: Other\n"
    with pytest.raises(ValueError, match="defined in no workflow"):
        harden_github.required_contexts(_workflow(tmp_path, body))


def test_an_if_gated_required_job_raises(tmp_path: Path) -> None:
    lines = ["name: CI", "on:", "  pull_request:", "jobs:"]
    for job_id in harden_github.REQUIRED_JOBS:
        lines += [f"  {job_id}:", f"    name: {job_id}", "    if: github.event_name == 'push'"]
    body = "\n".join(lines) + "\n"
    with pytest.raises(ValueError, match="if:-gated"):
        harden_github.required_contexts(_workflow(tmp_path, body))


# --------------------------------------------------------------------------------------------
# The ratchet: the script refuses to write a set smaller than the live one
# --------------------------------------------------------------------------------------------


class _FakeGh:
    """Stands in for the `gh` CLI: records every call, answers the protection read.

    `live_contexts=None` reproduces the state that matters most: branch protection is enabled but
    carries no `required_status_checks` key at all, so nothing gates a merge.
    """

    def __init__(self, live_contexts: list[str] | None) -> None:
        self.live_contexts = live_contexts
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, stdin: str | None = None) -> tuple[bool, str]:
        self.calls.append(args)
        joined = " ".join(args)
        if "branches/main/protection" in joined and "-X" not in args:
            if self.live_contexts is None:
                return True, '{"enforce_admins": {"enabled": false}}'
            return True, json.dumps(
                {"required_status_checks": {"strict": True, "contexts": self.live_contexts}}
            )
        if joined.startswith("api repos/") and "-X" not in args:
            return True, "{}"
        return True, ""

    def wrote_protection(self) -> bool:
        return any(
            "branches/main/protection" in " ".join(call) and "PUT" in call for call in self.calls
        )


@pytest.fixture
def _has_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harden_github.shutil, "which", lambda _name: "/usr/bin/gh")


@pytest.mark.usefixtures("_has_gh")
def test_write_is_refused_when_it_would_drop_a_live_required_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGh([*EXPECTED_CONTEXTS, "Some check the spec forgot"])
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 1
    assert not fake.wrote_protection()


@pytest.mark.usefixtures("_has_gh")
def test_write_proceeds_when_the_spec_covers_every_live_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 0
    assert fake.wrote_protection()


@pytest.mark.usefixtures("_has_gh")
def test_main_fails_when_every_gh_api_write_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main`'s only signal to the operator is its exit code, and until now `_apply` only warned
    on a failed write -- nothing recorded the failure anywhere `main` checked. Reproduces the
    exact demonstration: every `gh api` write refused, and the run must not still claim success.
    """
    monkeypatch.setattr(harden_github, "_gh", lambda args, stdin=None: (False, "boom"))

    assert harden_github.main(["kphutt/gdmutant"]) == 1
    out = capsys.readouterr().out
    assert "Branch protection NOT set" in out
    # Pins the exact "N of M" wording (not just a substring) -- all six `_apply` calls fail here,
    # so a mutation that corrupts the literal joining them ("of") must change this exact count.
    assert "6 of 6 setting(s) failed to apply" in out


@pytest.mark.usefixtures("_has_gh")
def test_main_fails_when_one_write_fails_among_otherwise_successful_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial failure must be caught too, not just the all-fail case -- a mutant that swaps
    `all()` for `any()` (or only tracks the last call) would still pass the all-fail test above."""
    fake = _FakeGh(list(EXPECTED_CONTEXTS))

    def flaky(args: list[str], *, stdin: str | None = None) -> tuple[bool, str]:
        if "vulnerability-alerts" in " ".join(args):
            return False, "rate limited"
        return fake(args, stdin=stdin)

    monkeypatch.setattr(harden_github, "_gh", flaky)

    assert harden_github.main(["kphutt/gdmutant"]) == 1


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([True, True, True], 0),
        ([True, False, True], 1),
        ([False, False], 2),
        ([True, None, True], 1),  # a stray falsy non-bool must still count as a failure
    ],
)
def test_failure_count_treats_any_falsy_result_as_a_failure(
    results: list[object], expected: int
) -> None:
    """`_apply` is contracted to return `bool`, but the count must not rely on that holding
    forever: a plain `results.count(False)` would treat a stray `None` as a success, which is
    exactly the shape of bug this file exists to close. Caught by a mutation run over the new
    aggregation logic: mutating `_apply`'s dry-run `return True` to `return None` survived against
    `applied.count(False)` because `None != False`, and `main` kept exiting 0."""
    assert harden_github._failure_count(results) == expected  # type: ignore[arg-type]


@pytest.mark.usefixtures("_has_gh")
def test_dry_run_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant", "--dry-run"]) == 0
    assert all("-X" not in call for call in fake.calls), "a dry run must not send a write"


@pytest.mark.usefixtures("_has_gh")
def test_check_mode_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant", "--check"]) == 0
    assert all("-X" not in call for call in fake.calls), "--check must not send a write"


# --------------------------------------------------------------------------------------------
# Triggers: a job that cannot run on a pull request cannot be a required check
# --------------------------------------------------------------------------------------------


NO_PR_TRIGGER = """\
name: CI
on:
  workflow_dispatch: # manual only
jobs:
  verify:
    name: Verify
    runs-on: ubuntu-24.04
"""

PATH_FILTERED_PR = """\
name: CI
on:
  pull_request:
    paths:
      - "src/**"
jobs:
  verify:
    name: Verify
    runs-on: ubuntu-24.04
"""


@pytest.mark.parametrize(
    ("body", "events", "filtered"),
    [
        ("on:\n  pull_request:\n  push:\n    branches: [main]\n", {"pull_request", "push"}, False),
        ("on: [push, pull_request]\n", {"push", "pull_request"}, False),
        ("on: push\n", {"push"}, False),
        ("on:\n  - push\n  - pull_request\n", {"push", "pull_request"}, False),
        (NO_PR_TRIGGER, {"workflow_dispatch"}, False),
        (PATH_FILTERED_PR, {"pull_request"}, True),
        ("on:\n  pull_request:\n    paths-ignore:\n      - docs/**\n", {"pull_request"}, True),
    ],
)
def test_trigger_parsing_covers_every_on_form(body: str, events: set[str], filtered: bool) -> None:
    triggers = harden_github.parse_triggers(body.splitlines())
    assert set(triggers.events) == events
    assert triggers.pull_request_filtered is filtered


def test_a_workflow_with_no_on_block_reports_no_events() -> None:
    triggers = harden_github.parse_triggers(["name: CI", "jobs:"])
    assert not triggers.events
    assert triggers.on_pull_request is False
    assert "no events at all" in triggers.describe()


def _all_required(tmp_path: Path, on_block: str, filename: str = "a-ci.yml") -> Path:
    """A workflow defining every REQUIRED_JOBS id under `on_block`.

    Every id is present and unconditional, so the only thing left to fail is the trigger - which
    keeps each trigger test pinned to the reason it is testing.
    """
    lines = ["name: CI", *on_block.splitlines(), "jobs:"]
    for job_id in harden_github.REQUIRED_JOBS:
        lines += [f"  {job_id}:", f"    name: {job_id}", "    runs-on: ubuntu-24.04"]
    return _workflow(tmp_path, "\n".join(lines) + "\n", filename=filename)


DISPATCH_ONLY = "on:\n  workflow_dispatch: # manual only"
FILTERED_ONLY = 'on:\n  pull_request:\n    paths:\n      - "src/**"'

# The ambiguity and shadowing tests need a SECOND definition of a job id that `required_contexts()`
# actually looks at; a duplicate of any other id is invisible to it. Read out of REQUIRED_JOBS so
# those tests keep testing what they claim to when the required set changes.
A_REQUIRED_JOB_ID = harden_github.REQUIRED_JOBS[0]


def test_a_required_job_whose_workflow_no_pull_request_triggers_raises(tmp_path: Path) -> None:
    """The migration case: a `ci.yml` reduced to `workflow_dispatch` can gate nothing."""
    with pytest.raises(ValueError, match="no pull request triggers"):
        harden_github.required_contexts(_all_required(tmp_path, DISPATCH_ONLY))


def test_a_required_job_behind_a_paths_filter_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path-filtered"):
        harden_github.required_contexts(_all_required(tmp_path, FILTERED_ONLY))


def test_a_job_id_defined_in_two_pull_request_workflows_is_ambiguous(tmp_path: Path) -> None:
    """This repo shares job ids between `ci.yml` and `publish.yml`.

    Letting the last file parsed win would read the wrong workflow's triggers, which is exactly
    what the trigger check above exists to catch.
    """
    directory = _all_required(tmp_path, "on:\n  pull_request:")
    (directory / "b-other.yml").write_text(
        "name: Other\non:\n  pull_request:\njobs:\n"
        f"  {A_REQUIRED_JOB_ID}:\n    name: Same id elsewhere\n    runs-on: ubuntu-24.04\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="more than one workflow"):
        harden_github.required_contexts(directory)


def test_a_release_only_twin_does_not_shadow_the_pull_request_job(tmp_path: Path) -> None:
    """The real shape: an id in both `ci.yml` and a release-only workflow resolves to `ci.yml`."""
    directory = _all_required(tmp_path, "on:\n  pull_request:")
    (directory / "z-publish.yml").write_text(
        "name: Publish\non:\n  release:\n    types: [published]\njobs:\n"
        f"  {A_REQUIRED_JOB_ID}:\n    name: The same job, release-time\n"
        "    runs-on: ubuntu-24.04\n",
        encoding="utf-8",
    )
    assert [job.workflow for job in harden_github.all_jobs(directory)[A_REQUIRED_JOB_ID]] == [
        "a-ci.yml",
        "z-publish.yml",
    ]
    assert A_REQUIRED_JOB_ID in harden_github.required_contexts(directory)
    assert "The same job, release-time" not in harden_github.required_contexts(directory)


def test_producible_contexts_excludes_anything_that_misses_a_pull_request(tmp_path: Path) -> None:
    directory = _workflow(
        tmp_path,
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  gate:\n    name: Gate\n    runs-on: ubuntu-24.04\n"
        "  gated:\n    name: Gated\n    if: needs.changes.outputs.x == 'true'\n",
        filename="a-ci.yml",
    )
    (directory / "b-filtered.yml").write_text(PATH_FILTERED_PR, encoding="utf-8")
    (directory / "c-dispatch.yml").write_text(NO_PR_TRIGGER, encoding="utf-8")
    assert harden_github.producible_contexts(directory) == {"Gate"}


def test_a_templated_name_cannot_vouch_for_a_required_context(tmp_path: Path) -> None:
    """An underivable name is left out rather than guessed: that can only cause a refusal."""
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-24.04]\n"
    )
    assert harden_github.producible_contexts(_workflow(tmp_path, body)) == set()


# --------------------------------------------------------------------------------------------
# The live read is three states, not one list
# --------------------------------------------------------------------------------------------


def test_absent_required_checks_are_not_collapsed_into_an_empty_match() -> None:
    """The swallowed-key bug, pinned.

    `(payload.get("required_status_checks") or {}).get("contexts") or []` gives the same empty
    list for "the branch requires nothing" and "the branch requires exactly these", which is how
    a report ends up reading as a match while nothing gates anything.
    """
    absent = harden_github.required_checks_of({"enforce_admins": {"enabled": False}})
    assert absent.configured is False
    assert absent.contexts == ()
    assert "ABSENT" in absent.describe()

    empty = harden_github.required_checks_of({"required_status_checks": {"contexts": []}})
    assert empty.configured is True
    assert "EMPTY" in empty.describe()

    populated = harden_github.required_checks_of(
        {"required_status_checks": {"contexts": ["Verify (ubuntu-24.04)"]}}
    )
    assert populated.contexts == ("Verify (ubuntu-24.04)",)
    assert "1 context(s) required" in populated.describe()

    assert len({absent.describe(), empty.describe(), populated.describe()}) == 3
    assert absent.gates_nothing and empty.gates_nothing and not populated.gates_nothing


def test_an_unreadable_protection_endpoint_is_its_own_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harden_github, "_gh", lambda args, stdin=None: (False, "404"))
    checks = harden_github.live_required_checks("kphutt/gdmutant")
    assert checks.readable is False
    assert "UNREADABLE" in checks.describe()
    assert not checks.gates_nothing, "we do not know that it gates nothing; we could not ask"


@pytest.mark.usefixtures("_has_gh")
def test_check_says_absent_loudly_instead_of_printing_a_row_of_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live gdmutant state: protection is enabled, but no required check exists at all."""
    monkeypatch.setattr(harden_github, "_gh", _FakeGh(None))

    assert harden_github.main(["kphutt/gdmutant", "--check"]) == 0
    out = capsys.readouterr().out

    assert "requires NO status checks at all" in out
    for context in EXPECTED_CONTEXTS:
        assert f"[ABSENT/spec] {context}" in out, "an absent live check must never read as a match"
        assert f"[  live/spec] {context}" not in out
    assert "would ADD" in out


@pytest.mark.usefixtures("_has_gh")
def test_check_reports_a_true_match_differently_from_an_absent_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harden_github, "_gh", _FakeGh(list(EXPECTED_CONTEXTS)))

    assert harden_github.main(["kphutt/gdmutant", "--check"]) == 0
    out = capsys.readouterr().out

    assert "requires NO status checks at all" not in out
    assert "ABSENT" not in out
    assert "would ADD" not in out
    for context in EXPECTED_CONTEXTS:
        assert f"[  live/spec] {context}" in out


@pytest.mark.usefixtures("_has_gh")
def test_check_still_reports_when_the_spec_cannot_be_derived(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--check` is the diagnostic, so a derivation failure is reported, not hidden behind."""

    def _boom(*_args: object, **_kwargs: object) -> list[str]:
        raise ValueError("no pull request triggers ci.yml")

    monkeypatch.setattr(harden_github, "required_contexts", _boom)
    fake = _FakeGh(None)
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant", "--check"]) == 1
    out = capsys.readouterr().out
    assert "cannot be derived" in out
    assert "no pull request triggers ci.yml" in out
    assert "no contexts on either side" in out
    assert all("-X" not in call for call in fake.calls), "--check must not send a write"


# --------------------------------------------------------------------------------------------
# The ratchet guards BOTH directions
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("_has_gh")
def test_write_is_refused_when_a_required_context_can_never_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The add direction.

    Dropping a live check was already refused; adding a context nothing can report was not, and
    that is precisely what a `workflow_dispatch`-only workflow leaves behind.
    """
    monkeypatch.setattr(
        harden_github, "required_contexts", lambda *a, **k: ["Verify", *EXPECTED_CONTEXTS]
    )
    fake = _FakeGh(None)
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 1
    assert not fake.wrote_protection()
    out = capsys.readouterr().out
    assert "no job reports ['Verify'] on a pull request" in out
    assert "enforce_admins" in out, "the refusal must name the escape hatch the same write closes"


@pytest.mark.usefixtures("_has_gh")
def test_write_is_refused_when_no_required_job_can_run_on_a_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: workflows with no pull_request trigger refuse the run before any write."""
    monkeypatch.setattr(harden_github, "WORKFLOWS", _all_required(tmp_path, DISPATCH_ONLY))
    fake = _FakeGh(None)
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 1
    assert not fake.wrote_protection()


@pytest.mark.usefixtures("_has_gh")
def test_write_proceeds_onto_a_branch_that_requires_nothing_when_the_checks_are_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding checks to an unguarded branch is the script's job.

    Only unreportable ones are refused, so the add-direction ratchet must not become a blanket
    refusal to ever raise the gate.
    """
    fake = _FakeGh(None)
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 0
    assert fake.wrote_protection()
