"""The GDScript half of the coverage marker run (docs/decisions/0017, step 2).

`GDScriptMarker` implements the engine's `engine.coverage.Marker`: given a throwaway copy of a
Godot project, it writes each file's marked source (`markers.place_markers`), installs the recorder
that the markers call, registers it with Godot, and says where the hits file will appear. The engine
then runs the suite on that copy and reads the file. Nothing here touches the user's project.

The recorder is two small scripts, and neither is quite what the ADR first described, for a reason
found while building step 1 (recorded in the ADR's update section):

* ``_GdmMarks`` is a ``class_name`` script with a static ``hit``, not an autoload. A test harness
  run with ``--script`` can load the code under test in its ``_init``, before Godot registers any
  autoload, and a marker that names an autoload fails to compile there ("Identifier not found"). A
  global class resolves at any time. The marker text is the same either way: ``_GdmMarks.hit(N);``.
* A separate autoload, ``_GdmHitsWriter``, writes the recorded spots as JSON when Godot frees it at
  exit. Autoloads are freed last-registered first, so it is registered first, which makes it the
  last one freed and lets it see hits made while the others shut down.

For ``--coverage-analysis per-file`` (step 3) the recorder also files each hit under the test file
that was running when it happened. It learns which file that is from the test runner, which
installs a hook of its own at `WINDOW_HOOK` (`engine.runner.FileSelecting.install_windows`); the
writer autoload loads it if it is there. With no hook installed, every hit lands under the load-time
window and the recorder behaves exactly as it did in step 2.

A global class only resolves after Godot's import scan has listed it, so `mark` runs
``godot --import`` on the copy once, and checks that the class really was registered, whatever the
test runner. That is the one step of coverage analysis that needs `godot`, even for
``--runner command``.

Marked files are never checked with gdtoolkit: it cannot parse a compound statement after ``;``
(``_GdmMarks.hit(3); if x:``), which Godot accepts. Godot itself is the check. The marker run must
be clean (`engine.coverage.clean_run_problems`), and a marked file Godot could not load shows up
there as a script error, a failing or missing test, or a lost hits file.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gdmutant.adapters.gdscript.markers import MARKER_AUTOLOAD, place_markers
from gdmutant.engine.coverage import MarkedCopy
from gdmutant.engine.mutants import Mutant

#: The directory inside the copy that holds the recorder and the hits file.
RECORDER_DIR = "_gdmutant"
#: The autoload that writes the hits file at exit.
WRITER_AUTOLOAD = "_GdmHitsWriter"
#: Where the writer puts the hits, relative to the copy.
HITS_FILE = f"{RECORDER_DIR}/hits.json"
#: What a runner calls its "a test file started / ended" hook when the writer autoload is to load
#: it. The runner that installs one writes this name into the recorder directory it is given, so
#: this constant is the one place the two ends agree on it.
WINDOW_HOOK_NAME = "windows.gd"
#: Where that hook sits, relative to the copy. The writer autoload loads it when it is there and
#: runs without windows when it is not, so a runner that cannot select test files needs to install
#: nothing (`engine.runner.FileSelecting`).
WINDOW_HOOK = f"{RECORDER_DIR}/{WINDOW_HOOK_NAME}"

_RECORDER_SOURCE = f"""class_name {MARKER_AUTOLOAD}
extends Object
## gdmutant's coverage recorder. It exists only in gdmutant's throwaway marked copy of a project.
## Every marker calls `hit` with its spot number, which records that the spot was reached while
## the current test file was running. A runner's window hook calls `begin_file` and `end_file`.

## The test file running right now, or "" for load time, between files, and after the last one.
static var window := ""
## spot -> the window it was last recorded under. The whole of `hit`'s fast path.
static var last := {{}}
## window -> the spots reached while it was open, as a set (the values are always true).
static var windows := {{"": {{}}}}
## Every window that opened, in the order the files ran. A file that reaches nothing is still here.
static var opened: Array = []


static func hit(spot: int) -> void:
\t## One dictionary lookup and one comparison, so a marker in a hot loop records once per window
\t## and pays almost nothing on every later pass through it.
\tif last.get(spot) != window:
\t\t_record(spot)


static func _record(spot: int) -> void:
\tlast[spot] = window
\tif not windows.has(window):
\t\twindows[window] = {{}}
\twindows[window][spot] = true


static func begin_file(path: String) -> void:
\twindow = path
\topened.append(path)
\tif not windows.has(path):
\t\twindows[path] = {{}}


static func end_file() -> void:
\twindow = ""
"""

_WRITER_SOURCE = f"""extends Node
## Writes what `{MARKER_AUTOLOAD}` recorded, once, when Godot frees this autoload at exit, and
## loads the runner's window hook at startup if one was installed.


func _ready() -> void:
\tif ResourceLoader.exists("res://{WINDOW_HOOK}"):
\t\tvar hook: Node = load("res://{WINDOW_HOOK}").new()
\t\tadd_child(hook)


func _notification(what: int) -> void:
\tif what == NOTIFICATION_PREDELETE:
\t\tvar windows := {{}}
\t\tvar hits := {{}}
\t\tfor key: Variant in {MARKER_AUTOLOAD}.windows:
\t\t\tvar spots: Array = {MARKER_AUTOLOAD}.windows[key].keys()
\t\t\twindows[key] = spots
\t\t\tfor spot: Variant in spots:
\t\t\t\thits[spot] = true
\t\tvar out := FileAccess.open("res://{HITS_FILE}", FileAccess.WRITE)
\t\tout.store_string(JSON.stringify({{
\t\t\t"hits": hits.keys(),
\t\t\t"windows": windows,
\t\t\t"opened": {MARKER_AUTOLOAD}.opened,
\t\t}}))
\t\tout.close()
"""

_IMPORT_TIMEOUT = 300.0
#: Godot's list of global classes, written by the import scan.
_CLASS_CACHE = Path(".godot") / "global_script_class_cache.cfg"
#: A real ``class_name _GdmMarks`` declaration: at the start of a line, after optional indentation
#: and any annotations sharing the line (``@tool class_name X``, ``@icon("res://i.svg")``), never
#: the words inside a comment or a string further along a line. A triple-quoted string with a line
#: that starts that way would still match. That errs toward refusing, which is loud and safe.
_TAKEN_CLASS = re.compile(
    rf"^[ \t]*(?:@\w+(?:\([^)\n]*\))?[ \t]+)*class_name[ \t]+{MARKER_AUTOLOAD}\b", re.MULTILINE
)


@dataclass(frozen=True)
class GDScriptMarker:
    """Marks a copy of a Godot project for the marker run. `godot` is the Godot executable used
    for the one import scan that registers the recorder class."""

    godot: str = "godot"

    def mark(self, copy_dir: str, files: Mapping[str, tuple[str, Sequence[Mutant]]]) -> MarkedCopy:
        """Mark the copy at `copy_dir` for `files`, and install the recorder (`Marker.mark`).

        Spot ids are unique across every file, so one hits file can serve them all. Raises
        `RuntimeError` with an actionable message when a name the recorder needs is taken, the copy
        is not a Godot project, or Godot could not register the recorder."""
        copy = Path(copy_dir)
        settings = copy / "project.godot"
        if not settings.is_file():
            raise RuntimeError(
                f"there is no project.godot in {copy_dir}, the copy of the --project directory, "
                "so it is not a Godot project gdmutant can mark"
            )
        _refuse_taken_names(copy, settings)
        placements: dict[str, tuple[int | None, ...]] = {}
        next_spot = 0
        for relative, (source, mutants) in files.items():
            marked = place_markers(source, mutants, first_spot=next_spot)
            next_spot += len(marked.spots)
            placements[relative] = tuple(
                spot if isinstance(spot, int) else None for spot in marked.placements
            )
            (copy / relative).write_text(marked.source, encoding="utf-8", newline="")
        recorder = copy / RECORDER_DIR
        recorder.mkdir()
        (recorder / "marks.gd").write_text(_RECORDER_SOURCE, encoding="utf-8", newline="")
        (recorder / "writer.gd").write_text(_WRITER_SOURCE, encoding="utf-8", newline="")
        settings.write_text(
            _prepared_settings(settings.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="",
        )
        self._register(copy)
        return MarkedCopy(
            hits_path=str(copy / HITS_FILE), placements=placements, recorder_dir=RECORDER_DIR
        )

    def _register(self, copy: Path) -> None:
        """Run Godot's import scan on `copy` so the recorder class resolves, then check it did.

        The exit code is not trusted either way (``--import`` exits non-zero on harmless addon
        chatter), so the class list itself is read back: a scan that did not register the class
        would otherwise surface much later as a marker run full of "Identifier not found"."""
        command = [self.godot, "--headless", "--path", str(copy.resolve()), "--import"]
        try:
            done = subprocess.run(
                command,
                cwd=copy,
                capture_output=True,
                text=True,
                timeout=_IMPORT_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"coverage analysis runs Godot ({self.godot!r}) once, to register its recorder in "
                "the marked copy, and it was not found. Pass the Godot executable with --godot. "
                "Coverage analysis reads --godot with every runner, --runner command included"
            ) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Godot's import scan of the marked copy took longer than {_IMPORT_TIMEOUT:g}s"
            ) from None
        cache = copy / _CLASS_CACHE
        listed = cache.read_text(encoding="utf-8") if cache.is_file() else ""
        if f'"{MARKER_AUTOLOAD}"' not in listed:
            output = (done.stdout + done.stderr).strip()[-1500:]
            raise RuntimeError(
                f"Godot's import scan did not register the {MARKER_AUTOLOAD} recorder class in "
                f"the marked copy, so no marker could call it. Godot said:\n{output}"
            )


def _refuse_taken_names(copy: Path, settings: Path) -> None:
    """Raise if the project already uses a name the recorder needs, rather than overwrite it."""
    if (copy / RECORDER_DIR).exists():
        raise RuntimeError(
            f"the project already has a {RECORDER_DIR}/ directory, which coverage analysis needs "
            "for its recorder. Rename it to use coverage analysis"
        )
    text = settings.read_text(encoding="utf-8")
    for name in (MARKER_AUTOLOAD, WRITER_AUTOLOAD):
        # A key at the start of its line. A commented-out entry starts with `;`, so it is not one.
        if re.search(rf"^[ \t]*{name}[ \t]*=", text, re.MULTILINE):
            raise RuntimeError(
                f"project.godot already registers an autoload named {name}, a name coverage "
                "analysis needs for its recorder. Rename it to use coverage analysis"
            )
    for script in sorted(copy.rglob("*.gd")):
        if _TAKEN_CLASS.search(script.read_text(encoding="utf-8", errors="replace")):
            raise RuntimeError(
                f"{script.relative_to(copy).as_posix()} already declares class_name "
                f"{MARKER_AUTOLOAD}, the name coverage analysis needs for its recorder. Rename it "
                "to use coverage analysis"
            )


#: The project setting that turns every GDScript warning off, and the section it lives under.
_WARNINGS_SECTION = "debug"
_WARNINGS_KEY = "gdscript/warnings/enable"


def _prepared_settings(settings: str) -> str:
    """`settings` (a project.godot) as the marked copy needs it.

    Two changes. The hits writer is registered as the first autoload, so Godot frees it last and it
    can still see a hit made while another autoload shuts down.

    And GDScript warnings are switched off for the copy. A project may set a warning to be treated
    as an error, which is a rule about the code its author writes, and the recorder is not that: it
    is gdmutant's code, dropped into a throwaway copy for one run. gdUnit4's own repository does
    exactly this (``untyped_declaration=2``), and it stopped the recorder from compiling at all, so
    the marker run failed on a project whose own suite is perfectly healthy. Turning the whole
    category off rather than the one warning that bit is deliberate: a later Godot can add a warning
    gdmutant has never heard of, and the recorder would fail the same way again.

    It hides nothing that matters. A warning is not a parse error, so a marked file Godot genuinely
    cannot load still fails the run, and a project whose own code trips a warning-as-error has a red
    baseline long before coverage analysis is asked for.
    """
    with_writer = _with_setting(
        settings, "autoload", WRITER_AUTOLOAD, f'"*res://{RECORDER_DIR}/writer.gd"'
    )
    return _with_setting(with_writer, _WARNINGS_SECTION, _WARNINGS_KEY, "false")


def _with_setting(settings: str, section: str, key: str, value: str) -> str:
    """`settings` (a project.godot) with ``key=value`` set first in ``[section]``.

    First in the section, because the one caller that cares about position needs it: an autoload
    registered first is freed last. A key already in that section is replaced where it stands, and a
    section that is not there at all is added at the end.
    """
    entry = f"{key}={value}"
    lines = settings.split("\n")
    header = f"[{section}]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return settings.rstrip("\n") + f"\n\n{header}\n\n{entry}\n"
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("["):
            break
        if lines[index].split("=", 1)[0].strip() == key:
            lines[index] = entry
            return "\n".join(lines)
    lines.insert(start + 1, entry)
    return "\n".join(lines)
