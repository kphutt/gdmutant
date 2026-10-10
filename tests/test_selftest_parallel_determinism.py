"""Live gate: ``--jobs N`` must reach the same verdicts a serial run reaches.

``--jobs N`` gives every worker its own copy of the project, which isolates the *file* each worker
mutates. It does not isolate anything the test run touches **outside** the project directory, and a
Godot project has one of those: ``user://``. Godot resolves it from ``application/config/name`` (and
the custom-user-dir settings beside it), never from the project path, so N copies of one project all
resolve ``user://`` to the same directory on the machine. A suite whose tests write a fixed-name
path there corrupt each other the moment two workers overlap, the suite goes red for a reason that
has nothing to do with the mutant, and gdmutant reads red as KILLED. The error only ever runs one
way: a survivor is reported as killed, so the score comes out *better* than the truth and the
survivor list comes out *shorter*. That is the one failure mode this tool must never have.

A private ``user://`` per worker fixes that, and opens the same hole through a second door, which
the second test here covers. An isolated ``user://`` starts **empty**. A suite that *reads* data
there which no test in the run creates is green in the project's own directory and red in every
worker, and red is still a KILL. So the run came out at 100% with no survivors on a project a
serial run scored 0.0% with two. The baseline is what closes it: under ``--jobs N`` it runs in an
isolated copy, the same conditions the mutants get, so it goes red there and the run stops with a
message instead of a score.

**Why the obvious version of this gate is worthless.** Run the corpus twice at ``--jobs 4`` and
compare: it passes while the bug is live, measured over 25 corpus runs with zero flaps, because no
corpus test writes to ``user://`` at all. A gate that cannot see the defect it is named after is
AGENTS.md's recurring bug one, a gate that passes without checking anything. So this file builds a
fixture with all three of the parts that make the comparison bite:

1. **A test suite that collides.** ``_SUITE_SOURCE`` writes a *fixed-name* file under ``user://``,
   waits long enough for a concurrent worker to overwrite it, and asserts on its contents. Private
   ``user://`` per worker: always green, no timing left in it. Shared: whichever worker wrote second
   wins and the other one goes red. ``_READER_SUITE_SOURCE`` is its opposite number for the second
   door: it reads a file the fixture seeds into the project's real ``user://`` and no test ever
   creates, so it is green where that directory is and red where it is empty.

2. **A mutation target that cannot legitimately be killed.** ``_PROBE_SOURCE`` holds nine mutants in
   functions nothing in the project calls, so every one of them must SURVIVE. A KILLED verdict on
   one can only be a false kill. The inertness is *proved*, not asserted: ``_unreferenced_probe``
   checks mechanically that no other file in the project so much as names the probe, and the serial
   run below confirms all nine survive in practice.

3. **A serial oracle.** The parallel runs are compared against a ``--jobs 1`` run of the same
   project, never against a repeat of themselves. Two parallel runs can agree with each other and
   both be wrong, which is exactly what a flapping race produces once the flap is reproducible.

**It fails rather than skips when it cannot check anything.** With ``GDMUTANT_GODOT`` set but the
GdUnit4 addon missing, this file fails: that is the state where a run looks green and checked
nothing. With ``GDMUTANT_GODOT`` unset it fails too *if this module was named on the pytest command
line*, because somebody asking for this file by name is asking for an answer, not for a skip. Only
a whole-suite run with no Godot configured skips it, which is what keeps a plain ``uv run pytest``
(and the Godot-free ``verify`` job) working. ``tests/conftest.py`` prints the state it is in before
the first test, so even that skip is on screen rather than buried in a skip count.

Run it with::

    GDMUTANT_GODOT="$(mise which godot)" uv run pytest \
        tests/test_selftest_parallel_determinism.py --no-cov -q

It costs a few minutes: one serial run plus `_PARALLEL_RUNS` parallel ones, each booting Godot once
per mutant, and then one more serial run for the reader case. The reader case's parallel run is
nearly free, because the whole point of it is that the run stops at the baseline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript.project_settings import (
    godot_data_path,
    with_setting,
    worker_user_dirs_root,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "corpus"
GDUNIT4_ADDON = CORPUS / "addons" / "gdUnit4"

#: This module's own file name, which is also how it is named on a pytest command line.
MODULE_FILE_NAME = Path(__file__).name

_GODOT = os.environ.get("GDMUTANT_GODOT")

#: The mutation target, and the test suite that makes concurrent workers collide. Both are written
#: into a throwaway copy of the corpus, never into ``corpus/`` itself: the shipped corpus pins the
#: live self-test's exact per-mutant outcomes, and a new suite in it would change them.
_PROBE_TARGET = "parallel_probe.gd"
_PROBE_SUITE = "test_private_user_dir.gd"

#: How many mutants `_PROBE_SOURCE` produces. Pinned so an operator-catalog change that silently
#: empties this gate (zero mutants run, nothing compared) fails instead of passing.
_EXPECTED_MUTANTS = 9

#: Worker count for the parallel runs, and how many of them to compare against the serial oracle.
#: Four is deliberately inside GdUnit4's TCP-port retry budget (it starts a server on a fixed port
#: and retries five times), so a failure here is about ``user://`` and not about ports.
_JOBS = 4
_PARALLEL_RUNS = 3

#: Every identifier the probe introduces shares this prefix, plus its class name. Two tokens are all
#: `_unreferenced_probe` has to search for, which is what makes that search a proof rather than a
#: gesture: a call to any of these functions, by name or through `call("...")`, has to spell one.
_PROBE_TOKENS = ("GdmutantParallelProbe", "gdmutant_probe_")

#: Nine mutants, none of them killable: nothing in the project calls any of these functions, so no
#: mutation of them can change what any test observes. Every mutant still compiles (a token
#: swapped, or the `not` operator dropped; no statement deleted, no value retyped), so none of them
#: lands as INVALID either --
#: an INVALID mutant never runs the suite and would quietly drop out of the comparison.
_PROBE_SOURCE = """class_name GdmutantParallelProbe

## A mutation target for the parallel-determinism gate: pure logic that NOTHING in this project
## calls, so every mutant of it survives a healthy run. A killed mutant here is a false kill.


static func gdmutant_probe_above(a: int, b: int) -> bool:
\treturn a > b


static func gdmutant_probe_below(a: int, b: int) -> bool:
\treturn a < b


static func gdmutant_probe_both(a: bool, b: bool) -> bool:
\treturn a and b


static func gdmutant_probe_either(a: bool, b: bool) -> bool:
\treturn a or b


static func gdmutant_probe_absent(a: bool) -> bool:
\treturn not a


static func gdmutant_probe_bumped(value: int) -> int:
\treturn value + 1


static func gdmutant_probe_fixed() -> bool:
\treturn true
"""

#: The suite that turns a shared ``user://`` into a red run. One test, one fixed-name file, a unique
#: payload per process, and a wait wide enough that two overlapping workers see each other.
#:
#: The payload is the process id, so the check is "is the file I read back still the one I wrote".
#: It needs no cleanup and no pre-existing state: a leftover file from an earlier run is simply
#: overwritten, so the test cannot go red for anything except another process writing to the same
#: path while this one holds it. That matters in the fixed state, where this suite runs dozens of
#: times in a row inside one worker and must be green every single time.
_SUITE_SOURCE = """extends GdUnitTestSuite

## Asserts that this project's `user://` belongs to this process alone.
##
## `user://` is resolved from the project's name settings, not from its path, so copies of one
## project share it. Under `--jobs N` that makes N workers write to one directory. This suite writes
## a fixed-name file, waits, and reads it back: a concurrent worker's write is then visible as a
## payload that is not the one this process stored.

const PRIVATE_DIR := "user://gdmutant_parallel_gate"
const PRIVATE_FILE := PRIVATE_DIR + "/owner.txt"

## How long to hold the file before reading it back. Wide enough to overlap a concurrent worker's
## own window, and far below GdUnit4's 300s per-test timeout.
const OVERLAP_MSEC := 2000


func test_user_dir_belongs_to_this_process_alone() -> void:
\tDirAccess.make_dir_recursive_absolute(PRIVATE_DIR)
\tvar mine := str(OS.get_process_id())
\tvar writer := FileAccess.open(PRIVATE_FILE, FileAccess.WRITE)
\tassert_that(writer).is_not_null()
\twriter.store_string(mine)
\twriter.close()
\tOS.delay_msec(OVERLAP_MSEC)
\tassert_bool(FileAccess.file_exists(PRIVATE_FILE)).is_true()
\tassert_str(FileAccess.get_file_as_string(PRIVATE_FILE)).is_equal(mine)
"""

#: The reader case's suite file, and the project name that decides where its `user://` lands. Its
#: own name, not the corpus's: this gate seeds state into that directory and deletes it afterwards,
#: and doing either to `gdmutant-corpus` would reach into the directory the live self-test runs
#: against.
_READER_SUITE = "test_reads_seeded_user_dir.gd"
_READER_PROJECT_NAME = "gdmutant-user-dir-read-gate"

#: What the fixture seeds into that project's real ``user://``, and what the suite reads back. No
#: test ever writes it, which is the whole condition: the data is there in the project's own
#: directory and absent from every isolated copy.
_SEEDED_DIR = "gdmutant_read_gate"
_SEEDED_NAME = "seed.txt"
_SEEDED_PAYLOAD = "seeded by the gate before the run, never by a test"

#: The suite that turns an *empty* ``user://`` into a red run -- the opposite number of
#: `_SUITE_SOURCE`. It reads and never writes, so it cannot create what it is missing, and there is
#: no timing in it at all: green wherever the seeded file is, red wherever it is not.
_READER_SUITE_SOURCE = f"""extends GdUnitTestSuite

## Reads data under `user://` that no test in this run creates.
##
## The gate writes this file into the project's real `user://` before the run. Every `--jobs` worker
## gets a `user://` of its own, which starts empty, so this suite is green in the project's own
## directory and red in every worker. A red suite is a KILL, so a run that went ahead and scored
## this project would report every mutant killed and no survivors at all.

const SEEDED := "user://{_SEEDED_DIR}/{_SEEDED_NAME}"


func test_reads_data_that_no_test_creates() -> void:
\tassert_bool(FileAccess.file_exists(SEEDED)).is_true()
\tassert_str(FileAccess.get_file_as_string(SEEDED)).is_equal("{_SEEDED_PAYLOAD}")
"""


def godot_dir_name() -> str:
    """The directory Godot keeps ``app_userdata/`` in, under the machine's data path.

    Godot's own rule (``OS::get_godot_dir_name``) is the short name lowercased, overridden to
    ``Godot`` on Windows and macOS. The case is the whole of it, which is why getting this wrong
    hides: Windows and macOS filesystems are case-insensitive by default, so the capitalized
    spelling this used everywhere worked on a Windows dev machine and sent the Linux CI job
    looking in ``~/.local/share/Godot/app_userdata/...`` for a file seeded into
    ``~/.local/share/godot/app_userdata/...``. Godot found nothing, the suite that reads it went
    red, and the serial run this gate needs green failed instead.

    `tests/test_parallel_gate_preconditions.py` pins it per platform, so the Godot-free `verify`
    job on both runners covers it rather than only a live Godot on one of them.
    """
    return "Godot" if sys.platform in ("win32", "darwin") else "godot"


def _reader_user_dir() -> Path:
    """Where Godot resolves ``user://`` for a project named `_READER_PROJECT_NAME`.

    ``<data dir>/<godot dir>/app_userdata/<project name>``, a second copy of a rule Godot owns,
    exactly as `godot_data_path` is. It is checked the same way, too: the serial run below can only
    come back green if Godot read the file the fixture wrote here, so a wrong path turns that run
    red and fails this test. There is no reading of "wrong path" that passes quietly.
    """
    return godot_data_path() / godot_dir_name() / "app_userdata" / _READER_PROJECT_NAME


def missing_preconditions(godot: str | None, addon: Path) -> list[str]:
    """What this gate needs and does not have, each phrased as the thing to go do.

    Empty means it can run. Separate from the fixture that acts on it so the decision is testable
    without a Godot anywhere (`tests/test_parallel_gate_preconditions.py`).
    """
    problems = []
    if not godot:
        problems.append(
            "GDMUTANT_GODOT is unset or empty: set it to a Godot executable "
            '(GDMUTANT_GODOT="$(mise which godot)")'
        )
    if not addon.is_dir():
        problems.append(
            f"the GdUnit4 addon is not installed at {addon.name}/: run "
            "python scripts/install_gdunit4.py"
        )
    return problems


def named_on_command_line(args: Sequence[str], file_name: str) -> bool:
    """True if `file_name` appears in pytest's own invocation `args`.

    The one case this separates: somebody ran this module on purpose. Then an unmet precondition is
    a failure, because they asked this gate a question and a skip is not an answer. Collected as
    part of a whole-suite run instead, the same precondition is just "not applicable here", and
    failing would take the Godot-free `verify` job down with it.

    A `-k` expression is deliberately not counted. It selects tests without naming the file, and the
    conservative reading of an unnamed selection is the whole-suite one.
    """
    return any(file_name in str(arg) for arg in args)


def _require_preconditions(request: pytest.FixtureRequest) -> None:
    """Stop the run here when this gate cannot check anything: fail, or skip in the one case that
    was not a question (`named_on_command_line`).

    Both fixtures go through this rather than carrying a copy each. Two copies of one decision is
    AGENTS.md's recurring bug two, and the half that drifted would be a live gate that quietly
    skips -- which is recurring bug one, reached from the other side.
    """
    problems = missing_preconditions(_GODOT, GDUNIT4_ADDON)
    if not problems:
        return
    said = "this gate cannot check anything: " + "; ".join(problems)
    if _GODOT or named_on_command_line(request.config.invocation_params.args, MODULE_FILE_NAME):
        pytest.fail(said)
    # A warning as well as a skip, like the vocabulary guard's skip. pytest hides the report header
    # under `-q`, so without it this skip would be the one silent half of the pair in that mode.
    warnings.warn(said, stacklevel=2)
    pytest.skip(said)


def _corpus_with_probe(root: Path, suite_name: str, suite_source: str) -> Path:
    """A throwaway copy of the corpus at `root`, holding the inert probe and one of the two suites.

    The shared half of both fixtures below, so the probe, its inertness proof and the copy's
    exclusions are written once. `_warm_import` is left to the caller: the reader fixture has to
    rename the project and seed its ``user://`` before Godot ever looks at it.
    """
    project = root / "project"
    # `.godot` and `reports` from an earlier local run are left out of the copy, so it starts cold
    # and the caller's import scan is the one that fills the cache.
    shutil.copytree(CORPUS, project, ignore=shutil.ignore_patterns(".godot", "reports"))
    (project / _PROBE_TARGET).write_text(_PROBE_SOURCE, encoding="utf-8", newline="\n")
    (project / "test" / suite_name).write_text(suite_source, encoding="utf-8", newline="\n")
    _unreferenced_probe(project)
    return project


@pytest.fixture(scope="module")
def probe_project(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway Godot project: the corpus, plus the inert probe and the colliding suite.

    Built once for the module (a Godot import scan per test would be minutes of waste), and never
    mutated by the tests themselves -- gdmutant restores every file it touches, and each run below
    starts from the same bytes.
    """
    _require_preconditions(request)
    project = _corpus_with_probe(
        tmp_path_factory.mktemp("parallel-gate"), _PROBE_SUITE, _SUITE_SOURCE
    )
    _warm_import(project)
    return project


@pytest.fixture(scope="module")
def reader_project(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """A throwaway Godot project whose suite reads ``user://`` data no test in the run creates.

    Three things make it that, and the order they happen in matters:

    * Its own ``application/config/name``, so the ``user://`` this fixture is about to write into is
      a directory the gate owns. The corpus's own name would put the seeded file in the directory
      the live self-test's projects resolve ``user://`` to, and the teardown below would then delete
      it from underneath them.
    * The seeded file, written before any Godot runs, and never written by a test.
    * The import scan last, so it is the renamed, seeded project Godot caches.

    The seeded directory is removed afterwards. It is the one thing in this file that lives outside
    a `tmp_path`, because `user://` is outside the project by definition, which is the whole reason
    any of this exists.
    """
    _require_preconditions(request)
    project = _corpus_with_probe(
        tmp_path_factory.mktemp("reader-gate"), _READER_SUITE, _READER_SUITE_SOURCE
    )
    settings = project / "project.godot"
    settings.write_text(
        with_setting(
            settings.read_text(encoding="utf-8"),
            "application",
            "config/name",
            f'"{_READER_PROJECT_NAME}"',
        ),
        encoding="utf-8",
        newline="",
    )
    seeded = _reader_user_dir() / _SEEDED_DIR
    seeded.mkdir(parents=True, exist_ok=True)
    (seeded / _SEEDED_NAME).write_text(_SEEDED_PAYLOAD, encoding="utf-8")
    _warm_import(project)
    try:
        yield project
    finally:
        shutil.rmtree(_reader_user_dir(), ignore_errors=True)


def _unreferenced_probe(project: Path) -> None:
    """Prove, mechanically, that nothing in `project` names the probe except the probe itself.

    This is the half of "the probe's mutants cannot be killed" that does not need a Godot run. A
    mutant can only change a test's outcome if some test reaches the code it changed, and in
    GDScript reaching a static function means spelling its name -- in a call, in an `@export`, in a
    scene file, in a `call("...")` string. So one search over every text file in the project
    answers it. Both tokens are distinctive enough that a hit is a real reference and not an English
    word that happens to appear in an addon's comment.
    """
    offenders: dict[str, list[str]] = {}
    skipped = {".png", ".svg", ".ttf", ".webp", ".jpg", ".import", ".uid"}
    scanned = 0
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.suffix in skipped:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [token for token in _PROBE_TOKENS if token in text]
        if path.name == _PROBE_TARGET:
            # The positive control: the probe is the one file that must name itself, so a search
            # that cannot see both tokens here (an emptied `_PROBE_TOKENS`) could not have seen a
            # reference anywhere else either.
            assert sorted(hits) == sorted(_PROBE_TOKENS) and hits, (
                f"the search's control failed: {_PROBE_TARGET} should contain every one of "
                f"{_PROBE_TOKENS}, but only {hits} were found, so a clean scan would prove nothing"
            )
            continue
        scanned += 1
        if hits:
            offenders[path.relative_to(project).as_posix()] = hits
    assert scanned, (
        f"the inertness proof scanned nothing: no file besides {_PROBE_TARGET} was readable text, "
        "so 'no file names the probe' is empty rather than true"
    )
    assert not offenders, (
        "the mutation target is supposed to be unreferenced, so that every mutant of it must "
        f"survive, but these files name it: {offenders}"
    )


def _warm_import(project: Path) -> None:
    """Run Godot's import scan once so `class_name` types resolve on this cold copy.

    The exit code is ignored (``--import`` exits non-zero on benign addon chatter); the artifact it
    had to produce is asserted instead, which is the same discipline the live self-test uses.
    """
    assert _GODOT is not None  # the fixture refuses to build without it
    subprocess.run(
        [_GODOT, "--headless", "--path", str(project), "--import"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert (project / ".godot").is_dir(), "Godot --import did not create the .godot cache"


def _gdmutant(project: Path, out: Path, jobs: int) -> subprocess.CompletedProcess[str]:
    """Run the shipped CLI over the probe in `project` at `jobs` workers, whatever it exits with.

    The one place this file builds that command. Both cases below drive the same tool with the same
    flags and differ only in which project they point it at, which is what makes their answers
    comparable at all -- and the reader case needs the failing run's own words, which `_verdicts`
    never gets to see because it refuses anything but exit 0.
    """
    assert _GODOT is not None
    command = [
        sys.executable,
        "-m",
        "gdmutant.cli",
        "run",
        str(project / _PROBE_TARGET),
        "--project",
        str(project),
        "--json",
        str(out),
        "--runner",
        "gdunit4",
        "--godot",
        _GODOT,
        "--jobs",
        str(jobs),
    ]
    return subprocess.run(
        command, capture_output=True, encoding="utf-8", errors="replace", timeout=3600, check=False
    )


def _verdicts(project: Path, out: Path, jobs: int) -> dict[tuple[int, int, str], str]:
    """A successful run's ``{mutant: status}``.

    A mutant is keyed by where it is and what it replaced, never by its index, so a comparison
    cannot be satisfied by two runs that happen to produce the same *number* of each status.
    """
    completed = _gdmutant(project, out, jobs)
    assert completed.returncode == 0, (
        f"gdmutant --jobs {jobs} exited {completed.returncode}\n--- stdout ---\n"
        f"{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    (file_obj,) = report["files"].values()
    return {
        (m["location"]["start"]["line"], m["location"]["start"]["column"], m["replacement"]): m[
            "status"
        ]
        for m in file_obj["mutants"]
    }


def _table(verdicts: dict[tuple[int, int, str], str]) -> str:
    """One line per mutant, so a failure is diagnosable from the log alone."""
    return "\n".join(
        f"  {line}:{column}  -> {replacement!r}  {status}"
        for (line, column, replacement), status in sorted(verdicts.items())
    )


def test_parallel_runs_agree_with_the_serial_verdicts(probe_project: Path, tmp_path: Path) -> None:
    """Every ``--jobs 4`` run must reach exactly the verdicts the ``--jobs 1`` run reached.

    The serial run is the oracle twice over: it is the verdict set the parallel runs are measured
    against, and it is the dynamic half of the inertness proof -- all nine mutants survive it, so
    any KILLED in a parallel run is a mutant the suite cannot legitimately catch.
    """
    root = worker_user_dirs_root()
    before = {entry.name for entry in root.iterdir()} if root.is_dir() else set()
    serial = _verdicts(probe_project, tmp_path / "serial.json", jobs=1)
    assert len(serial) == _EXPECTED_MUTANTS, (
        f"the probe produced {len(serial)} mutants, not {_EXPECTED_MUTANTS} -- this gate compares "
        f"verdicts, so an empty or shrunken mutant set would compare nothing\n{_table(serial)}"
    )
    survived = {key for key, status in serial.items() if status == "Survived"}
    assert survived == set(serial), (
        "the serial run must survive every mutant of an unreferenced target; a mutant it kills or "
        f"refuses is not inert and cannot measure a false kill\n{_table(serial)}"
    )
    for attempt in range(1, _PARALLEL_RUNS + 1):
        parallel = _verdicts(probe_project, tmp_path / f"parallel-{attempt}.json", jobs=_JOBS)
        # Over the union of both key sets, not just the serial one: a parallel run that invented a
        # mutant the serial run never produced is also a disagreement, and iterating one side would
        # read it as agreement.
        differing = {
            key: (serial.get(key), parallel.get(key))
            for key in serial.keys() | parallel.keys()
            if serial.get(key) != parallel.get(key)
        }
        assert not differing, (
            f"--jobs {_JOBS} run {attempt} of {_PARALLEL_RUNS} disagreed with the serial run on "
            f"{len(differing)} mutant(s): {{mutant: (serial, parallel)}} = {differing}\n"
            f"--- serial ---\n{_table(serial)}\n--- parallel ---\n{_table(parallel)}"
        )
    # The isolation moved `user://` somewhere, and both halves of "somewhere" have to hold. The
    # directory exists, so the path gdmutant computes for its own cleanup is the one Godot really
    # used -- without this leg, a wrong path would make the leftover check below pass *because* it
    # was wrong: nothing is ever created there, so nothing can be left behind. And nothing new is
    # in it, so every worker's directory was released. An entry that was already there is left
    # alone: it belongs to an interrupted earlier run, or to another gdmutant running right now.
    assert root.is_dir(), (
        f"no worker user:// directory turned up at {root}, so gdmutant and Godot disagree about "
        "where user:// goes; the isolation may be a no-op and its cleanup certainly is"
    )
    leftover = {entry.name for entry in root.iterdir()} - before
    assert not leftover, (
        f"these isolated user:// directories were not released in {root}: {leftover}"
    )


def test_a_parallel_run_refuses_a_suite_that_reads_user_dir_data_no_test_creates(
    reader_project: Path, tmp_path: Path
) -> None:
    """A suite reading ``user://`` data no test creates must stop a ``--jobs N`` run, and say why.

    The second door into the one failure mode this tool must never have, and the one the first test
    cannot see. Giving each worker its own ``user://`` means a worker's starts **empty**, so a suite
    that reads data it never created is red in every worker, and red is still a KILL. Measured on
    this project before the baseline moved: ``--jobs 1`` scored 0.0% and listed both of its
    survivors, ``--jobs 4`` scored 100.0% with no survivors, exit code 0, and no warning anywhere.

    Both halves are asserted, because either one on its own passes while the bug is live:

    * The serial run has to be green and every mutant has to survive it. That is what makes the
      parallel run's answer a *disagreement* rather than a project that was broken from the start,
      and it is also the only check on `_reader_user_dir` -- green here means Godot read the file
      the fixture seeded, at the path the fixture computed.
    * The parallel run has to fail, write no report, and name the isolated copy. A refusal whose
      message does not name it leaves a reader whose suite passes everywhere they try it with
      nothing at all to connect the two.
    """
    serial = _verdicts(reader_project, tmp_path / "reader-serial.json", jobs=1)
    assert len(serial) == _EXPECTED_MUTANTS, (
        f"the probe produced {len(serial)} mutants, not {_EXPECTED_MUTANTS} -- an empty or "
        f"shrunken mutant set would make the comparison below vacuous\n{_table(serial)}"
    )
    assert set(serial.values()) == {"Survived"}, (
        "the serial run must survive every mutant of an unreferenced target. A kill here means the "
        "seeded user:// data never reached the suite, so this project cannot measure anything\n"
        f"{_table(serial)}"
    )
    out = tmp_path / "reader-parallel.json"
    parallel = _gdmutant(reader_project, out, jobs=_JOBS)
    said = f"--- stdout ---\n{parallel.stdout}\n--- stderr ---\n{parallel.stderr}"
    assert parallel.returncode != 0, (
        f"--jobs {_JOBS} scored a project whose suite is red in every worker and exited 0. Every "
        f"one of the {len(serial)} survivors the serial run found would be reported as killed, and "
        f"the score as 100%.\n{said}"
    )
    assert not out.is_file(), (
        f"the refused run still wrote a report to {out}, which is a score for a run that never "
        f"happened\n{said}"
    )
    for phrase in ("isolated copy", "--jobs 1"):
        assert phrase in said, (
            f"the refusal never says {phrase!r}, so it does not tell a reader whose suite passes "
            f"everywhere else what happened or what to do\n{said}"
        )
