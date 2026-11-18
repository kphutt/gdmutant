"""Tests for ``--resume``: the journal file, and a run that picks up where another stopped."""

import json
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import MarkerRunner
from test_selection import _KILL_5, A, B, Lab
from test_selection import _SOURCE as _LAB_SOURCE

import gdmutant.cli as cli
import gdmutant.engine.journal as journal_mod
from gdmutant import __version__
from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.cli import main, run_mutation, run_mutation_paths
from gdmutant.engine.coverage import CoverageAnalysis
from gdmutant.engine.journal import Journal, JournalRefused, source_key
from gdmutant.engine.loop import (
    WHOLE_SUITE,
    MutantOutcome,
    MutantPlan,
    MutationRun,
    Verdict,
    _journal_entry,
    _Recorder,
    _resumed,
    _Trust,
)
from gdmutant.engine.loop import run as _run
from gdmutant.engine.loop import run_paths as _run_paths
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.runner import (
    SuiteResult,
    allow_commands,
    commands_stopping,
    stop_running_commands,
)
from gdmutant.engine.spans import Span

_SOURCE = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
_OTHER = "func g(a: int) -> int:\n\treturn a + 1\n"
_LONGER = (
    "func f(a, b) -> bool:\n\tvar x = a > b\n\tvar y = a < b\n\tvar z = a + 1 == b\n"
    "\treturn x and y or z\n"
)
_SETTINGS = {"runner": "gdunit4", "timeout": "None"}
_MUTANT = Mutant("f.gd", Span(2, 11, 2, 12), "comparison", ">", ">=")


def _project(root: Path, sources: dict[str, str] | None = None) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, text in (sources or {"f.gd": _SOURCE}).items():
        (root / name).write_text(text, encoding="utf-8")
        written[str(root / name)] = text
    return written


def _open(root: Path, sources: dict[str, str], **settings: str) -> Journal:
    return Journal.open(
        str(root / "run.journal"), str(root), sources, settings or _SETTINGS, __version__
    )


def _mutant(root: Path) -> Mutant:
    return Mutant(str(root / "f.gd"), _MUTANT.span, "comparison", ">", ">=")


# --- the journal file ----------------------------------------------------------------------------


def test_a_verdict_and_a_confirmation_are_there_when_the_journal_is_opened_again(
    tmp_path: Path,
) -> None:
    sources = _project(tmp_path)
    journal = _open(tmp_path, sources)
    assert journal.resumed == 0
    journal.record(_mutant(tmp_path), {"verdict": "killed"})
    journal.confirm(("tests/a.gd", "tests/b.gd"), False)
    journal.close()
    again = _open(tmp_path, sources)
    assert again.resumed == 1
    assert again.recorded(_mutant(tmp_path)) == {"verdict": "killed"}
    assert again.recorded(Mutant(str(tmp_path / "f.gd"), _MUTANT.span, "x", ">", "<")) is None
    assert again.confirmations == {("tests/a.gd", "tests/b.gd"): False}
    again.close()


def test_a_source_spelled_differently_is_the_same_source(tmp_path: Path) -> None:
    sources = _project(tmp_path / "p")
    journal = _open(tmp_path / "p", sources)
    journal.record(_mutant(tmp_path / "p"), {"verdict": "survived"})
    journal.close()
    spelled = {str(tmp_path / "p" / "sub" / ".." / "f.gd"): _SOURCE}
    again = _open(tmp_path / "p", spelled)
    assert again.recorded(Mutant(next(iter(spelled)), _MUTANT.span, "comparison", ">", ">="))
    again.close()


def test_a_source_is_named_relative_to_the_project_or_else_in_full(tmp_path: Path) -> None:
    assert source_key(str(tmp_path / "p" / "a" / "f.gd"), str(tmp_path / "p")) == "a/f.gd"
    outside = tmp_path / "elsewhere.gd"
    assert source_key(str(outside), str(tmp_path / "p")) == outside.resolve().as_posix()


def test_a_changed_source_refuses_the_journal_and_says_how_a_kill_could_cause_it(
    tmp_path: Path,
) -> None:
    sources = _project(tmp_path)
    _open(tmp_path, sources).close()
    with pytest.raises(JournalRefused) as refused:
        _open(tmp_path, {path: text.replace(">", ">=") for path, text in sources.items()})
    message = str(refused.value)
    assert "f.gd has changed since" in message
    assert "can leave a mutant in it" in message
    assert "delete the journal or name a new one" in message


def test_a_different_set_of_sources_refuses_the_journal(tmp_path: Path) -> None:
    both = _project(tmp_path, {"f.gd": _SOURCE, "g.gd": _OTHER})
    _open(tmp_path, {p: t for p, t in both.items() if p.endswith("f.gd")}).close()
    with pytest.raises(JournalRefused) as added:
        _open(tmp_path, both)
    assert "it mutated f.gd, and this run mutates f.gd, g.gd" in str(added.value)
    (tmp_path / "run.journal").unlink()
    _open(tmp_path, both).close()
    with pytest.raises(JournalRefused) as dropped:
        _open(tmp_path, {p: t for p, t in both.items() if p.endswith("f.gd")})
    assert "it mutated f.gd, g.gd, and this run mutates f.gd" in str(dropped.value)


def test_a_changed_test_file_refuses_the_journal(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_f.gd").write_text("extends Node\n", encoding="utf-8")
    (tests / "gone.gd").write_text("extends Node\n", encoding="utf-8")
    journal = str(tmp_path / "run.journal")

    def checked(*names: str) -> None:
        # The mutated source is listed too, as the CLI lists it: its own hash covers it.
        scripts = [str(tmp_path / "f.gd"), *(str(tests / name) for name in names)]
        Journal.open(journal, str(tmp_path), sources, _SETTINGS, __version__, scripts).close()

    checked("test_f.gd", "gone.gd", "missing.gd")
    checked("test_f.gd", "gone.gd", "missing.gd")  # an unreadable file reads the same each time
    (tests / "test_f.gd").write_text("extends Node\nfunc test_more():\n\tpass\n", encoding="utf-8")
    (tests / "test_g.gd").write_text("extends Node\n", encoding="utf-8")
    with pytest.raises(JournalRefused) as changed:
        checked("test_f.gd", "test_g.gd", "missing.gd")
    message = str(changed.value)
    assert "tests/test_f.gd has changed since" in message
    assert "tests/test_g.gd is new since" in message
    assert "tests/gone.gd is gone since" in message
    assert "f.gd" not in message.replace("test_f.gd", "")


def test_a_different_setting_refuses_the_journal(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    _open(tmp_path, sources, runner="gdunit4", timeout="None").close()
    with pytest.raises(JournalRefused) as changed:
        _open(tmp_path, sources, runner="gut", timeout="None")
    assert "runner was 'gdunit4', and is now 'gut'" in str(changed.value)
    assert "git checkout" not in str(changed.value)  # no source changed, so nothing to restore
    with pytest.raises(JournalRefused) as unset:
        _open(tmp_path, sources, runner="gdunit4")
    assert "timeout was 'None', and is now unset" in str(unset.value)
    with pytest.raises(JournalRefused) as new:
        _open(tmp_path, sources, runner="gdunit4", timeout="None", tests="res://t")
    assert "tests was None, and is now 'res://t'" in str(new.value)


def test_a_journal_from_another_version_is_refused(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    Journal.open(str(tmp_path / "run.journal"), str(tmp_path), sources, _SETTINGS, "0.0.1").close()
    with pytest.raises(JournalRefused) as refused:
        _open(tmp_path, sources)
    assert f"written by gdmutant 0.0.1, and this is gdmutant {__version__}" in str(refused.value)


def test_a_half_written_last_line_is_dropped_and_the_journal_still_appends_cleanly(
    tmp_path: Path,
) -> None:
    sources = _project(tmp_path)
    journal = _open(tmp_path, sources)
    journal.record(_mutant(tmp_path), {"verdict": "killed"})
    journal.close()
    with (tmp_path / "run.journal").open("a", encoding="utf-8") as handle:
        handle.write('{"mutant": ["f.gd", 2')  # a run killed part way through a write
    again = _open(tmp_path, sources)
    assert again.resumed == 1
    other = Mutant(str(tmp_path / "f.gd"), Span(2, 22, 2, 23), "comparison", "<", "<=")
    again.record(other, {"verdict": "survived"})
    again.close()
    lines = (tmp_path / "run.journal").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines]  # every line whole again
    assert _open(tmp_path, sources).resumed == 2


def test_an_empty_file_starts_a_new_journal(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    (tmp_path / "run.journal").write_text("", encoding="utf-8")
    _open(tmp_path, sources).close()
    header = json.loads((tmp_path / "run.journal").read_text(encoding="utf-8"))
    assert header["journal"] == "gdmutant journal"
    assert header["settings"] == _SETTINGS


@pytest.mark.parametrize(
    "content",
    [
        b"not json\n",
        b'{"journal": "something else"}\n',
        b"[1, 2]\n",
        b"\xff\xfe\n",
        b'{\n  "schemaVersion": "2"\n}',
        b'{"journal": "gdmutant journal"}',
    ],
    ids=["text", "other-json", "not-an-object", "not-utf8", "a-report", "one-line"],
)
def test_a_file_that_is_not_a_journal_is_refused(tmp_path: Path, content: bytes) -> None:
    sources = _project(tmp_path)
    (tmp_path / "run.journal").write_bytes(content)
    with pytest.raises(JournalRefused, match="is not a gdmutant journal"):
        _open(tmp_path, sources)
    assert (tmp_path / "run.journal").read_bytes() == content  # left as it was


@pytest.mark.parametrize(
    "entry",
    [
        "garbage",
        '{"something": 1}',
        '{"confirmed": ["a"]}',
        '{"mutant": "f.gd", "outcome": {}}',
    ],
)
def test_an_unreadable_entry_refuses_the_journal(tmp_path: Path, entry: str) -> None:
    sources = _project(tmp_path)
    _open(tmp_path, sources).close()
    with (tmp_path / "run.journal").open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
    with pytest.raises(JournalRefused, match="line 2 could not be read"):
        _open(tmp_path, sources)


# --- what the engine records and reads back -----------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        MutantOutcome(_MUTANT, Verdict.KILLED),
        MutantOutcome(_MUTANT, Verdict.SURVIVED, self_checked=True),
        MutantOutcome(_MUTANT, Verdict.TIMEOUT, selected=2, over_budget=True),
        MutantOutcome(_MUTANT, Verdict.KILLED, order_coupled=True),
    ],
)
def test_a_recorded_verdict_reads_back_as_it_was(tmp_path: Path, outcome: MutantOutcome) -> None:
    sources = _project(tmp_path)
    plan = MutantPlan(files=("tests/a.gd", "tests/b.gd"), self_check=True)
    mutant = _mutant(tmp_path)
    journal = _open(tmp_path, sources)
    journal.record(mutant, _journal_entry(outcome, plan))
    journal.close()
    again = _open(tmp_path, sources)
    assert _resumed(again, mutant, plan) == MutantOutcome(
        mutant,
        outcome.verdict,
        self_checked=outcome.self_checked,
        selected=outcome.selected,
        order_coupled=outcome.order_coupled,
        over_budget=outcome.over_budget,
        error=outcome.error,
    )
    again.close()


def test_a_verdict_reached_under_another_plan_is_not_reused(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    mutant = _mutant(tmp_path)
    journal = _open(tmp_path, sources)
    journal.record(
        mutant, _journal_entry(MutantOutcome(mutant, Verdict.KILLED), MutantPlan(files=("a",)))
    )
    assert _resumed(journal, mutant, WHOLE_SUITE) is None
    assert _resumed(journal, mutant, MutantPlan(files=("a",))) is not None
    journal.close()


def test_a_run_that_failed_is_run_again(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    mutant = _mutant(tmp_path)
    journal = _open(tmp_path, sources)
    failed = MutantOutcome(mutant, Verdict.ERROR, error="Godot fell over")
    journal.record(mutant, _journal_entry(failed, WHOLE_SUITE))
    assert _resumed(journal, mutant, WHOLE_SUITE) is None
    journal.record(mutant, _journal_entry(MutantOutcome(mutant, Verdict.KILLED), WHOLE_SUITE))
    journal.close()
    again = _open(tmp_path, sources)  # the later verdict for the same mutant wins
    assert _resumed(again, mutant, WHOLE_SUITE) == MutantOutcome(mutant, Verdict.KILLED)
    again.close()


@pytest.mark.parametrize(
    "change",
    [
        {"verdict": "squashed"},
        {"self_checked": "yes"},
        {"selected": 1.5},
        {"selected": True},
        {"error": 3},
        {"verdict": None},
    ],
)
def test_a_verdict_this_version_cannot_read_refuses_the_journal(
    tmp_path: Path, change: dict[str, object]
) -> None:
    sources = _project(tmp_path)
    mutant = _mutant(tmp_path)
    journal = _open(tmp_path, sources)
    entry = _journal_entry(MutantOutcome(mutant, Verdict.KILLED), WHOLE_SUITE)
    journal.record(mutant, entry | change)
    with pytest.raises(JournalRefused, match="has a verdict this version cannot read"):
        _resumed(journal, mutant, WHOLE_SUITE)
    missing = {k: v for k, v in entry.items() if k != "over_budget"}
    journal.record(mutant, missing)
    with pytest.raises(JournalRefused):
        _resumed(journal, mutant, WHOLE_SUITE)
    journal.close()


@dataclass
class _Confirming:
    """Says whether a set of test files passes unmutated, and counts how often it was asked."""

    clean: bool
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def run_selected(
        self, project_dir: str, files: tuple[str, ...], timeout: float | None = None
    ) -> SuiteResult:
        self.asked.append(files)
        return SuiteResult(tests=1, failures=0 if self.clean else 1, errors=0)


def test_a_confirmed_set_of_test_files_is_not_confirmed_again_after_a_resume(
    tmp_path: Path,
) -> None:
    sources = _project(tmp_path)
    journal = _open(tmp_path, sources)
    first = _Confirming(clean=False)
    trust = _Trust(first, journal)  # type: ignore[arg-type]
    assert not trust.confirms(str(tmp_path), ("tests/a.gd",), 10.0)
    assert not trust.confirms(str(tmp_path), ("tests/a.gd",), 10.0)  # cached, not recorded twice
    _Recorder(journal, trust).flush()
    _Recorder(journal, trust).flush()
    journal.close()
    assert first.asked == [("tests/a.gd",)]
    again = _open(tmp_path, sources)
    later = _Confirming(clean=True)
    resumed = _Trust(later, again)  # type: ignore[arg-type]
    assert not resumed.confirms(str(tmp_path), ("tests/a.gd",), 10.0)
    assert resumed.coupled == (("tests/a.gd",),)  # the summary still names it
    assert later.asked == []
    again.close()
    lines = (tmp_path / "run.journal").read_text(encoding="utf-8").splitlines()
    assert sum('"confirmed"' in line for line in lines) == 1


# --- a run that stops part way, then resumes ----------------------------------------------------


class _Killed(BaseException):
    """The process dying under the run: nothing in the loop may catch it as a verdict."""


@dataclass
class _DiesAfter:
    """Kills when `marker` is in `relname` inside the project it is handed, and dies outright on
    run number `dies_on` (the baseline is run 1). ``None`` never dies."""

    relname: str
    marker: str
    dies_on: int | None = None
    runs: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        with self._lock:  # --jobs workers count at once
            self.runs += 1
            run = self.runs
        if run == self.dies_on:
            raise _Killed
        text = (Path(project_dir) / self.relname).read_text(encoding="utf-8")
        return SuiteResult(tests=3, failures=int(self.marker in text), errors=0)


def _verdicts(result: MutationRun) -> list[tuple[int, int, str, str, Verdict]]:
    return [
        (
            o.mutant.span.line,
            o.mutant.span.column,
            o.mutant.operator_id,
            o.mutant.replacement,
            o.verdict,
        )
        for o in result.outcomes
    ]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_resumed_run_gives_the_verdicts_an_unbroken_run_gives_and_runs_only_the_rest(
    tmp_path: Path, jobs: int
) -> None:
    unbroken_sources = _project(tmp_path / "u", {"f.gd": _LONGER})
    unbroken_runner = _DiesAfter("f.gd", ">=")
    path = str(tmp_path / "u" / "f.gd")
    unbroken = _run(str(tmp_path / "u"), path, _LONGER, unbroken_runner, ADAPTER, jobs=jobs)
    mutants = unbroken_runner.runs - 1
    assert mutants >= 4
    assert {Verdict.KILLED, Verdict.SURVIVED} <= {o.verdict for o in unbroken.outcomes}

    root = tmp_path / "r"
    sources = _project(root, {"f.gd": _LONGER})
    path = str(root / "f.gd")
    journal = _open(root, sources)
    with pytest.raises(_Killed):
        _run(
            str(root),
            path,
            _LONGER,
            _DiesAfter("f.gd", ">=", dies_on=7),
            ADAPTER,
            jobs=jobs,
            journal=journal,
        )
    journal.close()
    (root / "f.gd").write_text(_LONGER, encoding="utf-8")  # as the run would have restored it

    journal = _open(root, sources)
    recorded = journal.resumed
    assert 1 <= recorded < mutants
    lines: list[str] = []
    runner = _DiesAfter("f.gd", ">=")
    resumed = _run(
        str(root), path, _LONGER, runner, ADAPTER, jobs=jobs, journal=journal, progress=lines.append
    )
    journal.close()
    assert _verdicts(resumed) == _verdicts(unbroken)
    assert runner.runs == 1 + mutants - recorded  # the baseline, then only what was missing
    assert any(f"{recorded} already in the journal" in line for line in lines)
    assert unbroken_sources  # the unbroken project was real files too

    journal = _open(root, sources)
    assert journal.resumed == mutants
    finished = _DiesAfter("f.gd", ">=")
    again = _run(str(root), path, _LONGER, finished, ADAPTER, jobs=jobs, journal=journal)
    journal.close()
    assert _verdicts(again) == _verdicts(unbroken)
    assert finished.runs == 1  # a finished journal still runs the baseline, and nothing else


def test_an_ignored_mutant_is_neither_recorded_nor_resumed(tmp_path: Path) -> None:
    source = (
        "func f(a, b) -> bool:\n\treturn a > b  # gdmutant: ignore\n\n"
        "func g(a, b):\n\treturn a < b\n"
    )
    sources = _project(tmp_path, {"f.gd": source})
    path = str(tmp_path / "f.gd")
    for _ in range(2):
        journal = _open(tmp_path, sources)
        result = _run(
            str(tmp_path), path, source, _DiesAfter("f.gd", "<="), ADAPTER, journal=journal
        )
        journal.close()
        assert result.outcomes[0].verdict is Verdict.IGNORED
        assert Verdict.KILLED in {o.verdict for o in result.outcomes}
    ran = sum(o.verdict is not Verdict.IGNORED for o in result.outcomes)
    lines = (tmp_path / "run.journal").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + ran  # the header, and each mutant, once


def test_a_resumed_multi_file_run_gives_every_file_its_verdicts(tmp_path: Path) -> None:
    files = {"f.gd": _SOURCE, "g.gd": _OTHER}

    @dataclass
    class _Both:
        dies_on: int | None = None
        runs: int = 0
        _lock: threading.Lock = field(default_factory=threading.Lock)

        def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            with self._lock:
                self.runs += 1
                run = self.runs
            if run == self.dies_on:
                raise _Killed
            texts = [(Path(project_dir) / n).read_text(encoding="utf-8") for n in files]
            failed = any(">=" in t or "- 1" in t for t in texts)
            return SuiteResult(tests=3, failures=int(failed), errors=0)

    unbroken_runner = _Both()
    unbroken = _run_paths(
        str(tmp_path / "u"), _project(tmp_path / "u", files), unbroken_runner, ADAPTER, jobs=2
    )
    mutants = unbroken_runner.runs - 1
    root = tmp_path / "r"
    sources = _project(root, files)
    journal = _open(root, sources)
    with pytest.raises(_Killed):
        _run_paths(str(root), sources, _Both(dies_on=5), ADAPTER, jobs=2, journal=journal)
    journal.close()
    journal = _open(root, sources)
    recorded = journal.resumed
    assert recorded >= 1
    runner = _Both()
    resumed = _run_paths(str(root), sources, runner, ADAPTER, jobs=2, journal=journal)
    journal.close()
    assert [_verdicts(r) for r in resumed.values()] == [_verdicts(r) for r in unbroken.values()]
    assert runner.runs == 1 + mutants - recorded  # the baseline, then only what was missing


# --- the CLI ------------------------------------------------------------------------------------


def test_run_mutation_resumes_and_refuses_a_changed_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    journal = str(tmp_path / "run.journal")
    assert (
        run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), resume=journal) == 0
    )
    first = capsys.readouterr()
    assert "Resuming" not in first.err
    assert (
        run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), resume=journal) == 0
    )
    again = capsys.readouterr()
    assert f"Resuming {journal}." in again.err
    assert again.out.split("Mutation score:")[1] == first.out.split("Mutation score:")[1]
    path.write_text(_SOURCE.replace("a > b", "a >= b"), encoding="utf-8")
    assert (
        run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), resume=journal) == 2
    )
    assert "f.gd has changed since" in capsys.readouterr().err


def test_run_mutation_paths_resumes_and_refuses_a_changed_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [str(p) for p in map(Path, _project(tmp_path, {"f.gd": _SOURCE, "g.gd": _OTHER}))]
    journal = str(tmp_path / "run.journal")
    runner = MarkerRunner(paths[0], ">=")
    assert run_mutation_paths(paths, str(tmp_path), runner, resume=journal) == 0
    capsys.readouterr()
    assert run_mutation_paths(paths, str(tmp_path), runner, resume=journal) == 0
    assert f"Resuming {journal}." in capsys.readouterr().err
    Path(paths[1]).write_text(_OTHER + "\n", encoding="utf-8")
    assert run_mutation_paths(paths, str(tmp_path), runner, resume=journal) == 2
    assert "g.gd has changed since" in capsys.readouterr().err


def test_a_journal_that_cannot_be_opened_is_a_setup_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    runner = MarkerRunner(str(path), ">=")
    missing = str(tmp_path / "no-such-dir" / "run.journal")
    assert run_mutation(str(path), str(tmp_path), runner, resume=missing) == 2
    assert f"could not write to the journal {missing}" in capsys.readouterr().err
    assert run_mutation(str(path), str(tmp_path), runner, resume=str(tmp_path)) == 2
    assert f"could not open the journal {tmp_path}" in capsys.readouterr().err  # a folder


def test_a_journal_that_stops_taking_writes_ends_the_run_with_the_source_restored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    journal = str(tmp_path / "run.journal")
    Journal.open(journal, str(tmp_path), {str(path): _SOURCE}, {}, __version__).close()
    synced: list[int] = []

    def full(fd: int) -> None:
        synced.append(fd)
        raise OSError(28, "No space left on device")

    # Only the journal's own view of `os`: the source file's restore must still work.
    monkeypatch.setattr(journal_mod, "os", SimpleNamespace(fsync=full))
    runner = MarkerRunner(str(path), ">=")
    assert run_mutation(str(path), str(tmp_path), runner, resume=journal, resume_settings={}) == 2
    assert f"could not write to the journal {journal}" in capsys.readouterr().err
    assert len(synced) == 1  # it failed on a verdict, mid-run
    assert path.read_text(encoding="utf-8") == _SOURCE


class _FullDisk:
    """A file handle on a disk that is full: every write is buffered, and nothing reaches it."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        return len(text)

    def flush(self) -> None:
        raise OSError(28, "No space left on device")

    def close(self) -> None:
        self.flush()  # as a real handle does, with the unwritten text still buffered


def test_a_journal_takes_no_write_after_one_failed_and_still_closes(tmp_path: Path) -> None:
    journal = _open(tmp_path, _project(tmp_path))
    journal.close()
    disk = _FullDisk()
    journal._handle = disk  # type: ignore[assignment]
    with pytest.raises(JournalRefused, match="No space left"):
        journal.record(_mutant(tmp_path), {"verdict": "killed"})
    with pytest.raises(JournalRefused, match="could not write to the journal"):
        journal.confirm(("tests/a.gd",), True)
    assert disk.writes == 1  # a second write could have landed after half a line
    journal.close()  # says nothing more: the failed write already did


def test_main_hands_the_journal_and_every_verdict_changing_setting_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    monkeypatch.setattr(cli, "_godot_version", lambda godot: f"4.7 at {godot}")
    argv = ["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4"]
    assert main([*argv, "--resume", "j.jsonl", "--timeout", "30", "--jobs", "2"]) == 0
    assert captured["resume"] == "j.jsonl"
    settings = captured["resume_settings"]
    assert isinstance(settings, dict)
    assert settings["runner"] == "gdunit4"
    assert settings["timeout"] == "30.0"
    assert settings["godot-version"] == "4.7 at godot"
    assert set(settings) == {
        "runner",
        "tests",
        "godot",
        "godot-version",
        "command",
        "timeout",
        "coverage-analysis",
        "coverage-self-check",
        "exclude-tests",
        "since",
    }
    assert "jobs" not in settings  # verdicts do not depend on it, so a resume may change it
    assert main(argv) == 0
    assert captured["resume"] is None


@pytest.mark.parametrize(
    ("extra", "asked"),
    [
        (["--resume", "j.jsonl", "--runner", "gdunit4"], ["godot"]),
        (["--resume", "j.jsonl", "--runner", "command", "--command", "x"], []),
        (
            [
                "--resume",
                "j.jsonl",
                "--runner",
                "command",
                "--command",
                "x",
                "--coverage-analysis",
                "all",
            ],
            ["godot"],
        ),
        (["--runner", "gdunit4"], []),
    ],
    ids=["godot runner", "command runner", "command with coverage", "no resume"],
)
def test_godot_is_asked_its_version_only_for_a_resumed_run_that_uses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str], asked: list[str]
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_mutation", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "_godot_version", lambda godot: calls.append(godot) or "4.7")
    assert main(["run", str(path), "--project", str(tmp_path), *extra]) == 0
    assert calls == asked


@pytest.mark.parametrize(
    ("outcome", "version"),
    [
        (subprocess.CompletedProcess([], 0, "4.7.stable.official\n", ""), "4.7.stable.official"),
        (subprocess.CompletedProcess([], 1, "", "no"), "unknown"),
        (FileNotFoundError("godot"), "unknown"),
        (subprocess.TimeoutExpired("godot", 60), "unknown"),
    ],
    ids=["prints it", "prints nothing", "missing", "hangs"],
)
def test_the_godot_version_is_what_godot_prints_or_unknown(
    monkeypatch: pytest.MonkeyPatch, outcome: object, version: str
) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        seen.append(command)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._godot_version("/opt/godot") == version
    assert seen == [["/opt/godot", "--version"]]


def test_dry_run_says_it_ignores_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    assert main(["run", str(path), "--dry-run", "--resume", str(tmp_path / "j")]) == 0
    assert "--resume is ignored" in capsys.readouterr().err
    assert not (tmp_path / "j").exists()


# --- what a stopped run must never write --------------------------------------------------------


def test_a_verdict_is_written_one_run_behind_and_never_once_a_stop_began(tmp_path: Path) -> None:
    sources = _project(tmp_path)
    journal = _open(tmp_path, sources)
    first, second, third = (
        Mutant(str(tmp_path / "f.gd"), Span(2, column, 2, column + 1), "comparison", ">", ">=")
        for column in (11, 12, 13)
    )
    recorder = _Recorder(journal, None)
    recorder.hold(MutantOutcome(first, Verdict.SURVIVED), WHOLE_SUITE)
    assert journal.recorded(first) is None  # its suite may yet turn out to have been stopped
    recorder.hold(MutantOutcome(second, Verdict.SURVIVED), WHOLE_SUITE)
    assert journal.recorded(first) is not None
    assert journal.recorded(second) is None
    stop_running_commands()
    try:
        recorder.hold(MutantOutcome(third, Verdict.KILLED), WHOLE_SUITE)
        recorder.flush()
    finally:
        allow_commands()
    assert journal.recorded(second) is None
    assert journal.recorded(third) is None
    journal.close()


def test_two_workers_confirming_one_set_at_once_journal_it_once(tmp_path: Path) -> None:
    @dataclass
    class _Overtaken:
        """Another worker answers for the same set while this confirmation is still running."""

        other: _Trust

        def run_selected(
            self, project_dir: str, files: tuple[str, ...], timeout: float | None = None
        ) -> SuiteResult:
            assert not self.other.confirms(project_dir, files, 10.0)  # the other worker: red
            return SuiteResult(tests=1, failures=0, errors=0)  # and this one: green

    trust = _Trust(_Confirming(clean=False))  # type: ignore[arg-type]
    racing = _Trust(_Overtaken(trust))  # type: ignore[arg-type]
    racing._clean = trust._clean  # one cache, two runners: two workers sharing one _Trust
    racing._unrecorded = trust._unrecorded
    racing._lock = trust._lock
    assert not racing.confirms(str(tmp_path), ("tests/a.gd",), 10.0)  # the first answer stands
    assert trust.take_unrecorded() == [(("tests/a.gd",), False)]


class _AnsweredAsTheStopBegan:
    """A `_Trust` whose last answer came from a confirmation run the stop had just killed."""

    def take_unrecorded(self) -> list[tuple[tuple[str, ...], bool]]:
        stop_running_commands()
        return [(("tests/a.gd",), False)]


def test_an_answer_from_a_stopped_confirmation_is_not_journaled(tmp_path: Path) -> None:
    journal = _open(tmp_path, _project(tmp_path))
    try:
        _Recorder(journal, _AnsweredAsTheStopBegan()).flush()  # type: ignore[arg-type]
    finally:
        allow_commands()
    assert journal.confirmations == {}
    journal.close()


def _killed_verdicts(journal: str) -> int:
    lines = Path(journal).read_text(encoding="utf-8").splitlines()
    return sum('"verdict": "killed"' in line for line in lines)


@dataclass
class _InterruptedSerially:
    """Every mutant survives. Run `interrupted` reports a failure, as a suite stopped by the
    console's Ctrl-C does on Windows, and the next run never gets going: the interrupt arrives."""

    interrupted: int
    runs: int = 0

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.runs += 1
        if self.runs == self.interrupted + 1:
            raise _Killed
        return SuiteResult(tests=3, failures=int(self.runs == self.interrupted), errors=0)


def test_a_suite_the_interrupt_killed_is_not_journaled_as_a_kill(tmp_path: Path) -> None:
    sources = _project(tmp_path, {"f.gd": _LONGER})
    journal = _open(tmp_path, sources)
    with pytest.raises(_Killed):
        _run(
            str(tmp_path),
            str(tmp_path / "f.gd"),
            _LONGER,
            _InterruptedSerially(4),
            ADAPTER,
            journal=journal,
        )
    journal.close()
    assert _killed_verdicts(str(tmp_path / "run.journal")) == 0
    assert _open(tmp_path, sources).resumed == 2  # the two runs before it, and nothing else


@dataclass
class _StoppedUnderJobs:
    """Every mutant survives until run `stops_on`. That run waits until another worker's run is
    going, then stops every command, as Ctrl-C does under --jobs, and dies. The run it waited
    for ends after the stop, and reports a failure, as a stopped suite does."""

    stops_on: int
    runs: int = 0
    after_stop: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    going: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        with self.lock:
            self.runs += 1
            number = self.runs
        if number < self.stops_on:
            return SuiteResult(tests=3, failures=0, errors=0)
        if number == self.stops_on:
            assert self.going.wait(5)
            stop_running_commands()
            self.stopped.set()
            raise _Killed
        self.going.set()
        assert self.stopped.wait(5)
        with self.lock:
            self.after_stop += 1
        return SuiteResult(tests=3, failures=int(commands_stopping()), errors=0)


def test_suites_a_stop_killed_under_jobs_are_not_journaled_as_kills(tmp_path: Path) -> None:
    sources = _project(tmp_path, {"f.gd": _LONGER})
    journal = _open(tmp_path, sources)
    runner = _StoppedUnderJobs(5)
    try:
        with pytest.raises(_Killed):
            _run(
                str(tmp_path),
                str(tmp_path / "f.gd"),
                _LONGER,
                runner,
                ADAPTER,
                jobs=3,
                journal=journal,
            )
    finally:
        allow_commands()
    journal.close()
    assert runner.after_stop >= 1  # a suite did end after the stop, so there was one to keep out
    assert _killed_verdicts(str(tmp_path / "run.journal")) == 0


# --- resuming with coverage analysis on ---------------------------------------------------------


@dataclass
class _DyingLab(Lab):
    """`Lab`, dying on its suite run number `dies_on`."""

    dies_on: int | None = None
    calls: int = 0

    def _tick(self) -> None:
        self.calls += 1
        if self.calls == self.dies_on:
            raise _Killed

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self._tick()
        return super().run(project_dir, timeout)

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        self._tick()
        return super().run_selected(project_dir, files, timeout)


def _lab(dies_on: int | None = None) -> _DyingLab:
    # Line 5 is reached by a.gd, which fails on its own, so its kills are order-coupled. Line 9 is
    # reached by nothing, so its mutants are no coverage, and the self-check samples them.
    return _DyingLab(
        dirty={(A,)}, forward={A: {0}, B: set()}, killers={_KILL_5: A}, dies_on=dies_on
    )


def _lab_run(lab: _DyingLab, project: Path, journal: Journal | None) -> MutationRun:
    target = project / "t.gd"
    target.write_text(_LAB_SOURCE, encoding="utf-8")  # as an interrupted run restores it
    lab.target = target
    return _run(
        str(project),
        str(target),
        _LAB_SOURCE,
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.PER_FILE,
        marker=lab,
        journal=journal,
    )


def _everything(result: MutationRun) -> list[object]:
    return [
        (o.verdict, o.self_checked, o.selected, o.order_coupled, o.over_budget, o.error)
        for o in result.outcomes
    ] + [result.order_coupled_sets]


def test_a_resumed_per_file_run_gives_the_report_an_unbroken_one_gives(tmp_path: Path) -> None:
    (tmp_path / "u").mkdir()
    unbroken_lab = _lab()
    unbroken = _lab_run(unbroken_lab, tmp_path / "u", None)
    assert {o.verdict for o in unbroken.outcomes} >= {Verdict.KILLED, Verdict.NO_COVERAGE}
    assert unbroken.order_coupled_sets == ((A,),)
    assert any(o.self_checked for o in unbroken.outcomes)

    project = tmp_path / "r"
    project.mkdir()
    sources = {str(project / "t.gd"): _LAB_SOURCE}
    journal = Journal.open(str(tmp_path / "run.journal"), str(project), sources, {}, __version__)
    with pytest.raises(_Killed):
        _lab_run(_lab(dies_on=unbroken_lab.calls - 2), project, journal)
    journal.close()

    journal = Journal.open(str(tmp_path / "run.journal"), str(project), sources, {}, __version__)
    assert journal.resumed
    resumed_lab = _lab()
    resumed = _lab_run(resumed_lab, project, journal)
    journal.close()
    assert _everything(resumed) == _everything(unbroken)
    assert resumed_lab.calls < unbroken_lab.calls


# --- the files a resumed run checks -------------------------------------------------------------


def test_the_project_files_a_journal_checks_are_its_scripts(tmp_path: Path) -> None:
    names = (
        "project.godot",
        "a.gd",
        "scenes/s.tscn",
        "data/r.tres",
        "fx/x.gdshader",
        "report.json",
        "a.gd.uid",
        ".godot/imported/a.gd",
        "notes.md",
    )
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    found = {Path(p).relative_to(tmp_path).as_posix() for p in cli._project_scripts(str(tmp_path))}
    assert found == {"a.gd", "fx/x.gdshader"}


def test_run_mutation_refuses_a_journal_once_a_test_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    test = tmp_path / "tests" / "test_f.gd"
    test.parent.mkdir()
    test.write_text("extends Node\n", encoding="utf-8")
    journal = str(tmp_path / "run.journal")
    runner = MarkerRunner(str(path), ">=")
    assert run_mutation(str(path), str(tmp_path), runner, resume=journal) == 0
    test.write_text("extends Node\nfunc test_more():\n\tpass\n", encoding="utf-8")
    capsys.readouterr()
    assert run_mutation(str(path), str(tmp_path), runner, resume=journal) == 2
    assert "tests/test_f.gd has changed since" in capsys.readouterr().err


@dataclass
class _WritesIntoTheProject:
    """Kills when ``>=`` is in the f.gd it is handed, and saves a resource into that project on
    every run, stamped with the run's number."""

    runs: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        with self._lock:
            self.runs += 1
            run = self.runs
        root = Path(project_dir)
        saved = root / "test_out" / "state.tres"
        saved.parent.mkdir(exist_ok=True)
        saved.write_text(f"[resource]\nrun = {run}\n", encoding="utf-8")
        killed = ">=" in (root / "f.gd").read_text(encoding="utf-8")
        return SuiteResult(tests=3, failures=int(killed), errors=0)


@pytest.mark.parametrize("jobs", [(1, 1), (2, 1), (1, 2), (2, 2)], ids=str)
def test_a_suite_that_writes_into_the_project_can_still_be_resumed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], jobs: tuple[int, int]
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    test = tmp_path / "tests" / "test_f.gd"
    test.parent.mkdir()
    test.write_text("extends Node\n", encoding="utf-8")
    journal = str(tmp_path / "run.journal")
    runner = _WritesIntoTheProject()
    # Under --jobs the suite writes only into copies, so the project is left as it was.
    for run_jobs in (*jobs, 1, 1):
        assert run_mutation(str(path), str(tmp_path), runner, resume=journal, jobs=run_jobs) == 0
    assert capsys.readouterr().err.count(f"Resuming {journal}.") == 3
    test.write_text("extends Node\nfunc test_more():\n\tpass\n", encoding="utf-8")
    assert run_mutation(str(path), str(tmp_path), runner, resume=journal) == 2
    err = capsys.readouterr().err
    assert "tests/test_f.gd has changed since" in err
    assert "state.tres" not in err


@dataclass
class _SavesTheSource:
    """Kills on ``>=``, and saves the f.gd it ran against into the project, so an unmutated run
    saves the same file each time and a mutant's run leaves a different one behind."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        text = (Path(project_dir) / "f.gd").read_text(encoding="utf-8")
        (Path(project_dir) / "saved.tres").write_text(text, encoding="utf-8")
        return SuiteResult(tests=3, failures=int(">=" in text), errors=0)


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_resource_a_mutant_left_behind_does_not_refuse_the_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], jobs: int
) -> None:
    path = tmp_path / "f.gd"
    path.write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "saved.tres").write_text(_SOURCE, encoding="utf-8")  # an earlier run's
    journal = str(tmp_path / "run.journal")
    assert run_mutation(str(path), str(tmp_path), _SavesTheSource(), resume=journal) == 0
    assert (tmp_path / "saved.tres").read_text(encoding="utf-8") != _SOURCE  # the last mutant's
    runner = _SavesTheSource()
    assert run_mutation(str(path), str(tmp_path), runner, resume=journal, jobs=jobs) == 0
    assert f"Resuming {journal}." in capsys.readouterr().err
