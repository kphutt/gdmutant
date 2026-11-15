"""A run's verdicts, written to a file as each one lands, so a run that stops part way can resume.

The file is JSON Lines. The first line says what the run was: the gdmutant version, the sources it
mutates, a hash of each of them and of every file its tests may read, and the settings that can
change a verdict. Each later line is one mutant's verdict, or one answer to "does this set of test
files pass unmutated?" (coverage analysis's `_Trust`).

A journal is resumed only by a run of the same sources, with those files unchanged, under the same
settings. Any difference refuses it, since a verdict recorded against other code says nothing
about this code.
The engine decides what goes in a record. This module only keeps records and matches them up.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import IO, Any

from gdmutant.engine.mutants import Mutant

#: Bumped whenever a record's shape changes, so an older journal is refused rather than misread.
FORMAT = 1
_KIND = "gdmutant journal"


class JournalRefused(Exception):
    """The journal cannot be used by this run: it is not a journal, was written for different
    sources or settings, or cannot be written."""


def source_key(path: str, project_dir: str) -> str:
    """`path` as the journal names it: relative to `project_dir` when inside it, so the same file
    matches however the command spelled it, else resolved."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mutant_key(source: str, mutant: Mutant) -> str:
    span = mutant.span
    return json.dumps(
        [
            source,
            span.line,
            span.column,
            span.end_line,
            span.end_column,
            mutant.operator_id,
            mutant.original,
            mutant.replacement,
        ]
    )


class Journal:
    """An open journal: what earlier runs recorded, and the file new records are appended to.

    `record` and `confirm` may be called from several ``--jobs`` workers at once. Each record is
    flushed to disk before the call returns, so a run killed at any moment loses at most the
    mutants that were still running.
    """

    def __init__(
        self,
        path: str,
        project_dir: str,
        handle: IO[str],
        outcomes: dict[str, dict[str, Any]],
        confirmations: dict[tuple[str, ...], bool],
        project: _ProjectFiles | None = None,
    ) -> None:
        self.path = path
        self._project_dir = project_dir
        self._handle = handle
        self._outcomes = outcomes
        self._confirmations = confirmations
        self._project = project or _ProjectFiles()
        self._other_files: Callable[[], Iterable[str]] = tuple
        self._before: dict[str, str] = {}
        self._sources: frozenset[str] = frozenset()
        self._lock = threading.Lock()
        self._broken = False
        #: How many verdicts earlier runs left, for the CLI to say it is resuming.
        self.resumed = len(outcomes)

    @classmethod
    def open(
        cls,
        path: str,
        project_dir: str,
        sources: Mapping[str, str],
        settings: Mapping[str, str],
        version: str,
        other_files: Callable[[], Iterable[str]] = tuple,
    ) -> Journal:
        """Resume the journal at `path`, or start one there if it is missing or empty.

        `sources` maps each source to mutate to its contents, and `settings` holds whatever else
        can change a verdict, as strings. Raises `JournalRefused` if the file at `path` was written
        for anything else, or is not a gdmutant journal, and then leaves that file as it was.
        `other_files` lists the files the tests may read. They are hashed now, before the baseline,
        and compared by `check_files`.
        """
        header = {
            "journal": _KIND,
            "format": FORMAT,
            "gdmutant": version,
            "sources": {
                source_key(p, project_dir): _digest(t.encode()) for p, t in sources.items()
            },
            "settings": dict(settings),
        }
        target = Path(path)
        lines, cut = _lines(target)
        outcomes: dict[str, dict[str, Any]] = {}
        confirmations: dict[tuple[str, ...], bool] = {}
        project = _ProjectFiles()
        if lines:
            _check_header(path, lines[0], header)
            for number, line in enumerate(lines[1:], start=2):
                _read_entry(path, number, line, outcomes, confirmations, project)
            if cut is not None:  # only once the file is known to be a journal
                with target.open("r+b") as cutting:
                    cutting.truncate(cut)
        else:
            _write_whole(target, json.dumps(header) + "\n")
        handle = target.open("a", encoding="utf-8", newline="\n")
        journal = cls(path, project_dir, handle, outcomes, confirmations, project)
        journal._sources = frozenset(source_key(p, project_dir) for p in sources)
        journal._other_files = other_files
        journal._before = journal._hash_other_files()
        return journal

    def check_files(self) -> None:
        """Compare the `other_files` hashed by `open` with what the run that started the journal
        found, or record them if no run got this far. Called once the baseline has run: a file it
        changed is one the suite writes, which is left out, here and on every later resume.

        Raises `JournalRefused` if any other file differs."""
        after = self._hash_other_files()
        written = {
            key
            for key in self._before.keys() | after.keys()
            if self._before.get(key) != after.get(key)
        }
        recorded = self._project
        if recorded.files is None:
            with self._lock:
                recorded.files, recorded.written = self._before, written
                self._append({"files": self._before, "written": sorted(written)})
            return
        skip = recorded.written | written
        differences = _file_differences(
            {key: digest for key, digest in recorded.files.items() if key not in skip},
            {key: digest for key, digest in self._before.items() if key not in skip},
        )
        if differences:
            raise JournalRefused(
                f"{self.path} was written when the project's files were different:\n"
                + "".join(f"  {difference}\n" for difference in differences)
                + "A verdict reached against other tests or scenes may not hold now. To start "
                "over, delete the journal or name a new one."
            )
        if written - recorded.written:
            with self._lock:
                self._append({"written": sorted(written - recorded.written)})
                recorded.written |= written

    def _hash_other_files(self) -> dict[str, str]:
        return {
            key: _file_digest(file)
            for file in self._other_files()
            if (key := source_key(file, self._project_dir)) not in self._sources
        }

    def recorded(self, mutant: Mutant) -> dict[str, Any] | None:
        """What an earlier run recorded for `mutant`, or ``None``."""
        return self._outcomes.get(self._key(mutant))

    def record(self, mutant: Mutant, outcome: dict[str, Any]) -> None:
        """Record `outcome` for `mutant`, on disk before this returns."""
        key = self._key(mutant)
        with self._lock:
            self._outcomes[key] = outcome
            self._append({"mutant": json.loads(key), "outcome": outcome})

    @property
    def confirmations(self) -> dict[tuple[str, ...], bool]:
        """Every set of test files earlier runs checked unmutated, and whether it passed."""
        return dict(self._confirmations)

    def confirm(self, files: tuple[str, ...], clean: bool) -> None:
        """Record whether `files` passed on the unmutated project."""
        with self._lock:
            self._confirmations[files] = clean
            self._append({"confirmed": list(files), "clean": clean})

    def close(self) -> None:
        # A write already failed and said so; a half line left behind is dropped on open.
        with contextlib.suppress(OSError):
            self._handle.close()

    def _key(self, mutant: Mutant) -> str:
        return _mutant_key(source_key(mutant.path, self._project_dir), mutant)

    def _append(self, entry: dict[str, Any]) -> None:
        # Once a write fails, no later one may follow it, or a half line ends up mid-file.
        if self._broken:
            raise JournalRefused(f"could not write to the journal {self.path}")
        try:
            self._handle.write(json.dumps(entry) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as error:
            self._broken = True
            raise JournalRefused(f"could not write to the journal {self.path}: {error}") from error


class _ProjectFiles:
    """The project files the journal's first run hashed, and those a baseline was seen to write."""

    def __init__(self) -> None:
        self.files: dict[str, str] | None = None
        self.written: set[str] = set()


def _file_digest(path: str) -> str:
    try:
        return _digest(Path(path).read_bytes())
    except OSError:
        return "unreadable"  # still a fact two runs can agree on


def _write_whole(target: Path, text: str) -> None:
    """Write `text` to a new `target` through a rename, so a kill leaves all of it or none."""
    staged = target.with_name(target.name + ".new")
    try:
        with staged.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    except OSError as error:
        raise JournalRefused(f"could not write to the journal {target}: {error}") from error


def _lines(target: Path) -> tuple[list[str], int | None]:
    """The file's complete lines, and where to cut it to drop a last line a killed run left half
    written (``None`` when there is none)."""
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return [], None
    if data and b"\n" not in data:  # every journal ends its first line
        raise JournalRefused(f"{target} is not a gdmutant journal")
    complete = data[: data.rfind(b"\n") + 1]
    try:
        lines = complete.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise JournalRefused(f"{target} is not a gdmutant journal") from None
    return lines, len(complete) if len(complete) < len(data) else None


def _parse(path: str, number: int, line: str) -> dict[str, Any]:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        entry = None
    if not isinstance(entry, dict):
        raise JournalRefused(f"{path} is not a gdmutant journal: line {number} could not be read")
    return entry


def _check_header(path: str, line: str, expected: dict[str, Any]) -> None:
    found = _parse(path, 1, line)
    if found.get("journal") != _KIND:
        raise JournalRefused(f"{path} is not a gdmutant journal")
    if found.get("format") != FORMAT or found.get("gdmutant") != expected["gdmutant"]:
        raise JournalRefused(
            f"{path} was written by gdmutant {found.get('gdmutant')}, and this is gdmutant "
            f"{expected['gdmutant']}. Its verdicts may not match this version's. Delete it, or "
            "name a new journal, to start over."
        )
    sources = found.get("sources")
    recorded: dict[str, str] = sources if isinstance(sources, dict) else {}
    differences = []
    changed: list[str] = []
    if sorted(recorded) != sorted(expected["sources"]):
        differences.append(
            f"it mutated {', '.join(sorted(recorded))}, and this run mutates "
            f"{', '.join(sorted(expected['sources']))}"
        )
    else:
        changed = _file_differences(recorded, expected["sources"])
    differences += changed + _setting_differences(found.get("settings"), expected["settings"])
    if differences:
        # Only a changed source can be a mutant a killed run left behind.
        restore = (
            "A run that was stopped while it mutated a file in place can leave a mutant in it: if "
            "so, restore that file (for example with git checkout), then run again. "
            if changed
            else ""
        )
        raise JournalRefused(
            f"{path} was written for a different run:\n"
            + "".join(f"  {difference}\n" for difference in differences)
            + restore
            + "To start over, delete the journal or name a new one."
        )


def _file_differences(recorded: dict[str, str], expected: dict[str, str]) -> list[str]:
    differences = []
    for file, digest in expected.items():
        if file not in recorded:
            differences.append(f"{file} is new since")
        elif recorded[file] != digest:
            differences.append(f"{file} has changed since")
    differences += [f"{file} is gone since" for file in recorded if file not in expected]
    return differences


def _setting_differences(found: object, expected: dict[str, str]) -> list[str]:
    recorded: dict[str, str] = found if isinstance(found, dict) else {}
    return [
        f"{name} was {recorded.get(name)!r}, and is now {value!r}"
        for name, value in expected.items()
        if recorded.get(name) != value
    ] + [
        f"{name} was {value!r}, and is now unset"
        for name, value in recorded.items()
        if name not in expected
    ]


def _read_entry(
    path: str,
    number: int,
    line: str,
    outcomes: dict[str, dict[str, Any]],
    confirmations: dict[tuple[str, ...], bool],
    project: _ProjectFiles,
) -> None:
    """Read one entry into `outcomes`, `confirmations` or `project`."""
    entry = _parse(path, number, line)
    mutant, outcome = entry.get("mutant"), entry.get("outcome")
    files, clean = entry.get("confirmed"), entry.get("clean")
    project_files, written = entry.get("files"), entry.get("written")
    if isinstance(mutant, list) and isinstance(outcome, dict):
        outcomes[json.dumps(mutant)] = outcome
    elif isinstance(files, list) and isinstance(clean, bool):
        confirmations[tuple(str(file) for file in files)] = clean
    elif isinstance(project_files, dict) and isinstance(written, list) and project.files is None:
        project.files = {str(file): str(digest) for file, digest in project_files.items()}
        project.written = {str(file) for file in written}
    elif project_files is None and isinstance(written, list) and project.files is not None:
        project.written |= {str(file) for file in written}
    else:
        raise JournalRefused(f"{path} is not a gdmutant journal: line {number} could not be read")
