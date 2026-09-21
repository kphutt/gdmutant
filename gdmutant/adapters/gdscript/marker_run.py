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

_RECORDER_SOURCE = f"""class_name {MARKER_AUTOLOAD}
extends Object
## gdmutant's coverage recorder. It exists only in gdmutant's throwaway marked copy of a project.
## Every marker calls `hit` with its spot number, and the first call for a spot records it.

static var hits := {{}}


static func hit(spot: int) -> void:
\thits[spot] = true
"""

_WRITER_SOURCE = f"""extends Node
## Writes the spots `{MARKER_AUTOLOAD}` recorded, once, when Godot frees this autoload at exit.


func _notification(what: int) -> void:
\tif what == NOTIFICATION_PREDELETE:
\t\tvar out := FileAccess.open("res://{HITS_FILE}", FileAccess.WRITE)
\t\tout.store_string(JSON.stringify({{"hits": {MARKER_AUTOLOAD}.hits.keys()}}))
\t\tout.close()
"""

_IMPORT_TIMEOUT = 300.0
#: Godot's list of global classes, written by the import scan.
_CLASS_CACHE = Path(".godot") / "global_script_class_cache.cfg"
_TAKEN_CLASS = re.compile(rf"\bclass_name\s+{MARKER_AUTOLOAD}\b")


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
            _with_writer_autoload(settings.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="",
        )
        self._register(copy)
        return MarkedCopy(hits_path=str(copy / HITS_FILE), placements=placements)

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
        if re.search(rf"^\s*{name}\s*=", text, re.MULTILINE):
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


def _with_writer_autoload(settings: str) -> str:
    """`settings` (a project.godot) with the hits writer registered as the FIRST autoload."""
    entry = f'{WRITER_AUTOLOAD}="*res://{RECORDER_DIR}/writer.gd"'
    lines = settings.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == "[autoload]":
            lines.insert(index + 1, entry)
            return "\n".join(lines)
    return settings.rstrip("\n") + f"\n\n[autoload]\n\n{entry}\n"
