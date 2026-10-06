"""GDScript adapter: reading and editing a copy's ``project.godot``.

Two callers need to change a *copy* of a Godot project's settings before a run, and both must do it
without disturbing anything else in the file: the coverage marker run registers its recorder
autoload (`marker_run`), and a ``--jobs`` worker gives its copy a ``user://`` of its own
(`isolate_user_dir` below). `with_setting` is the one editor both use, so there is no second
implementation to drift.

**Why the user-dir half lives here and not in the engine.** The engine's parallel path copies the
project once per worker and asks the language adapter to give each copy whatever state it would
otherwise share (`engine.adapter.Adapter.isolate_copy`). For GDScript that state is ``user://``, and
isolating it means writing two Godot project settings -- a sentence the engine must never have to
understand (NF-3, AGENTS.md's language-neutral rule).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

#: The section and keys that decide where Godot resolves ``user://``.
_APPLICATION = "application"
_USE_CUSTOM_USER_DIR = "config/use_custom_user_dir"
_CUSTOM_USER_DIR_NAME = "config/custom_user_dir_name"
_PROJECT_NAME = "config/name"

#: Every isolated worker directory is nested under this one name inside the machine's data
#: directory, rather than scattered beside the user's own Godot projects. It is what makes the
#: leftovers of an interrupted run obvious, findable and safe to delete by hand -- and it is the
#: directory `release_user_dir` deliberately leaves behind, because its existence after a run is the
#: evidence that `godot_data_path` computed the same location Godot itself used.
_WORKER_PARENT = "gdmutant"

#: A ``config/name`` that Godot will actually use: the key, then at least one character inside the
#: quotes. An empty name is the case that matters -- see `isolate_user_dir`. The key
#: ``config/name_localized`` does not match it, because ``=`` has to follow the name itself.
_NAMED = re.compile(rf'^[ \t]*{_PROJECT_NAME}[ \t]*=[ \t]*"[^"]', re.MULTILINE)


def with_setting(settings: str, section: str, key: str, value: str) -> str:
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


def worker_user_dir_name(token: str) -> str:
    """The ``config/custom_user_dir_name`` an isolated copy identified by `token` gets.

    A path, not a bare name: Godot joins this onto the machine's data directory and creates it
    recursively, so every worker of every run lands under one ``gdmutant/`` parent instead of
    littering the directory that holds the user's own projects.
    """
    return f"{_WORKER_PARENT}/{token}"


def worker_user_dirs_root() -> Path:
    """The directory on this machine that every isolated worker's ``user://`` lands inside.

    Where `release_user_dir` deletes from, and what the live parallel gate watches: after a parallel
    run this directory has to *exist* (Godot put a worker's ``user://`` exactly where
    `godot_data_path` says it would) and hold nothing new (every worker's directory was released).
    Checking only the second half would pass by default if the first were wrong.
    """
    return godot_data_path() / _WORKER_PARENT


def isolate_user_dir(copy_dir: str, token: str) -> None:
    """Give the project copy at `copy_dir` a ``user://`` that no other copy resolves to.

    This is the GDScript half of `engine.adapter.Adapter.isolate_copy`. Godot resolves ``user://``
    from the project's *name settings*, never from the project's path, so N copies of one project
    all write to one directory on the machine -- which is a shared, mutable resource in the middle
    of a run that is supposed to be N independent runs. A suite whose tests write a fixed-name file
    under ``user://`` then corrupts itself across workers, goes red for a reason that has nothing to
    do with the mutant, and the run records a survivor as KILLED.

    Three settings, and the third is the one that is easy to miss:

    * ``config/use_custom_user_dir=true`` and ``config/custom_user_dir_name=<token>`` move
      ``user://`` to ``<data dir>/gdmutant/<token>``. Both are written unconditionally, not added
      only when absent: a project that already sets ``use_custom_user_dir=false``, or that names its
      own custom directory, would otherwise keep every worker pointed at one shared directory.
      Overwriting them in a throwaway copy costs that project nothing -- the copy exists for the
      length of one mutant and is then deleted.
    * ``config/name``, but only when the project does not already have a non-empty one. Godot reads
      the project name *first* and ignores both custom-user-dir settings when it is empty, falling
      back to one ``app_userdata/[unnamed project]`` directory shared by every nameless project on
      the machine (verified against Godot 4.7). A nameless project would therefore look isolated and
      not be.

    One consequence to know about: a worker's ``user://`` starts **empty**. A suite whose tests read
    data there that no test in the run created fails in every worker, and consistently failing tests
    kill every mutant, so such a run once came back at 100% with no survivors off a baseline that
    was green in the project's own directory. That is no longer reachable: under ``--jobs N`` the
    engine runs the baseline in an isolated copy too (`engine.loop._baseline_project`), so the
    baseline is red as well and the run stops with a message instead of a score. Such a suite
    depends on state it does not set up, which is the same fault this isolation exists to stop one
    worker inflicting on another, so the answer is to make the suite create what it reads rather
    than to go back to sharing.

    A copy with no ``project.godot`` is left alone, and that is a decision rather than an oversight:
    without one there is no Godot project, so there is no ``user://`` for anything to share and
    nothing here to isolate. It is reachable: the framework-neutral command runner (ADR-0005) can
    drive a project that is not a Godot project at all, its baseline runs fine, and every worker
    then runs unisolated. That is correct for ``user://`` and is the whole of what this function
    is responsible for. Anything else such a command shares across workers is outside it.
    """
    settings = Path(copy_dir) / "project.godot"
    if not settings.is_file():
        return
    # Written back with newline="" for the same reason the marked copy is: `write_text` would
    # translate every "\n" to the platform's line ending and silently rewrite the whole file.
    text = settings.read_text(encoding="utf-8")
    text = with_setting(text, _APPLICATION, _USE_CUSTOM_USER_DIR, "true")
    text = with_setting(
        text, _APPLICATION, _CUSTOM_USER_DIR_NAME, f'"{worker_user_dir_name(token)}"'
    )
    if not _NAMED.search(text):
        text = with_setting(text, _APPLICATION, _PROJECT_NAME, f'"{token}"')
    settings.write_text(text, encoding="utf-8", newline="")


def release_user_dir(token: str) -> None:
    """Delete the ``user://`` directory `isolate_user_dir` gave the copy identified by `token`.

    `engine.adapter.Adapter.release_copy`: the engine deletes the project copy itself, but the
    directory Godot wrote outside it is ours to remove. Nothing reads it after the worker's last
    suite run -- it holds that framework's temporary files and Godot's own log -- and leaving it
    would grow the machine's data directory by one directory per worker per run, forever.

    Best effort on purpose, and it never raises: this runs when the mutants are already scored, so a
    directory a virus scanner still has open must not turn a finished run into a failed one. The
    path is built from the run's own token, so there is nothing else it can name.
    """
    shutil.rmtree(worker_user_dirs_root() / token, ignore_errors=True)


def godot_data_path() -> Path:
    """Where Godot puts a project's ``user://`` directory on this machine.

    A second copy of a rule Godot owns (``OS::get_data_path``), which is worth saying out loud. It
    is only ever used to *delete a directory this process asked Godot to create*, so the whole cost
    of the two drifting apart is a leftover directory, never a wrong verdict and never a deletion
    somewhere else. The live parallel gate checks the two still agree the only way that can be
    checked: it runs real Godot and then asserts the directory turned up here.
    """
    if sys.platform == "win32":
        # Godot falls back to the current directory when APPDATA is somehow unset, and so does this.
        return Path(os.environ.get("APPDATA") or ".")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    # Linux/BSD: XDG_DATA_HOME, which Godot uses only when it is absolute, else ~/.local/share.
    xdg = os.environ.get("XDG_DATA_HOME", "")
    return Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "share"
