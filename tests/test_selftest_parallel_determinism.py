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

**Why the obvious version of this gate is worthless.** Run the corpus twice at ``--jobs 4`` and
compare: it passes while the bug is live, measured over 25 corpus runs with zero flaps, because no
corpus test writes to ``user://`` at all. A gate that cannot see the defect it is named after is
AGENTS.md's recurring bug one, a gate that passes without checking anything. So this file builds a
fixture with all three of the parts that make the comparison bite:

1. **A test suite that collides.** ``_SUITE_SOURCE`` writes a *fixed-name* file under ``user://``,
   waits long enough for a concurrent worker to overwrite it, and asserts on its contents. Private
   ``user://`` per worker: always green, no timing left in it. Shared: whichever worker wrote second
   wins and the other one goes red.

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
per mutant.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript.project_settings import worker_user_dirs_root

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
#: mutation of them can change what any test observes. Every mutant is also a token swap that still
#: compiles (no deleted statement, no retyped value), so none of them lands as INVALID either --
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


@pytest.fixture(scope="module")
def probe_project(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway Godot project: the corpus, plus the inert probe and the colliding suite.

    Built once for the module (a Godot import scan per test would be minutes of waste), and never
    mutated by the tests themselves -- gdmutant restores every file it touches, and each run below
    starts from the same bytes.
    """
    problems = missing_preconditions(_GODOT, GDUNIT4_ADDON)
    if problems:
        said = "this gate cannot check anything: " + "; ".join(problems)
        if _GODOT or named_on_command_line(request.config.invocation_params.args, MODULE_FILE_NAME):
            pytest.fail(said)
        pytest.skip(said)
    project = tmp_path_factory.mktemp("parallel-gate") / "project"
    # `.godot` and `reports` from an earlier local run are left out of the copy, so it starts cold
    # and the import scan below is the one that fills the cache.
    shutil.copytree(CORPUS, project, ignore=shutil.ignore_patterns(".godot", "reports"))
    (project / _PROBE_TARGET).write_text(_PROBE_SOURCE, encoding="utf-8", newline="\n")
    (project / "test" / _PROBE_SUITE).write_text(_SUITE_SOURCE, encoding="utf-8", newline="\n")
    _unreferenced_probe(project)
    _warm_import(project)
    return project


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
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.suffix in skipped or path.name == _PROBE_TARGET:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [token for token in _PROBE_TOKENS if token in text]
        if hits:
            offenders[path.relative_to(project).as_posix()] = hits
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


def _verdicts(project: Path, out: Path, jobs: int) -> dict[tuple[int, int, str], str]:
    """Run the shipped CLI over the probe at `jobs` workers; return ``{mutant: status}``.

    A mutant is keyed by where it is and what it replaced, never by its index, so a comparison
    cannot be satisfied by two runs that happen to produce the same *number* of each status.
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
    completed = subprocess.run(
        command, capture_output=True, encoding="utf-8", errors="replace", timeout=3600, check=False
    )
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
