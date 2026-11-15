"""A run's verdicts, written to a file as each one lands, so a run that stops part way can resume.

The file is JSON Lines. The first line says what the run was: the gdmutant version, each source's
content hash, and the settings that can change a verdict. Each later line is one mutant's verdict,
or one answer to "does this set of test files pass unmutated?" (coverage analysis's `_Trust`).

A journal is resumed only by a run of the same sources, unchanged, under the same settings. Any
difference refuses it, since a verdict recorded against other code says nothing about this code.
The engine decides what goes in a record. This module only keeps records and matches them up.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    ) -> None:
        self.path = path
        self._project_dir = project_dir
        self._handle = handle
        self._outcomes = outcomes
        self._confirmations = confirmations
        self._lock = threading.Lock()
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
    ) -> Journal:
        """Resume the journal at `path`, or start one there if it is missing or empty.

        `sources` maps each source path to its contents, and `settings` holds whatever else can
        change a verdict, as strings. Raises `JournalRefused` if the file at `path` was written for
        anything else, or is not a gdmutant journal.
        """
        header = {
            "journal": _KIND,
            "format": FORMAT,
            "gdmutant": version,
            "sources": {source_key(p, project_dir): _digest(text) for p, text in sources.items()},
            "settings": dict(settings),
        }
        target = Path(path)
        lines = _complete_lines(target)
        outcomes: dict[str, dict[str, Any]] = {}
        confirmations: dict[tuple[str, ...], bool] = {}
        if lines:
            _check_header(path, lines[0], header)
            for number, line in enumerate(lines[1:], start=2):
                _read_entry(path, number, line, outcomes, confirmations)
        handle = target.open("a", encoding="utf-8", newline="\n")
        journal = cls(path, project_dir, handle, outcomes, confirmations)
        if not lines:
            journal._append(header)
        return journal

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
        self._handle.close()

    def _key(self, mutant: Mutant) -> str:
        return _mutant_key(source_key(mutant.path, self._project_dir), mutant)

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            self._handle.write(json.dumps(entry) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as error:
            raise JournalRefused(f"could not write to the journal {self.path}: {error}") from error


def _complete_lines(target: Path) -> list[str]:
    """The journal's lines, after cutting off a last line a killed run left half written."""
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return []
    complete = data[: data.rfind(b"\n") + 1]
    if len(complete) < len(data):
        with target.open("r+b") as handle:
            handle.truncate(len(complete))
    try:
        return complete.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise JournalRefused(f"{target} is not a gdmutant journal") from None


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
    differences = _source_differences(found.get("sources"), expected["sources"])
    differences += _setting_differences(found.get("settings"), expected["settings"])
    if differences:
        raise JournalRefused(
            f"{path} was written for a different run:\n"
            + "".join(f"  {difference}\n" for difference in differences)
            + "A run that was stopped while it mutated a file in place can leave a mutant in it: "
            "if so, restore that file (for example with git checkout), then run again. To start "
            "over instead, delete the journal or name a new one."
        )


def _source_differences(found: object, expected: dict[str, str]) -> list[str]:
    recorded: dict[str, str] = found if isinstance(found, dict) else {}
    differences = []
    for source, digest in expected.items():
        if source not in recorded:
            differences.append(f"{source} was not part of it")
        elif recorded[source] != digest:
            differences.append(f"{source} has changed since")
    differences += [f"{source} was part of it" for source in recorded if source not in expected]
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
) -> None:
    entry = _parse(path, number, line)
    mutant, outcome = entry.get("mutant"), entry.get("outcome")
    files, clean = entry.get("confirmed"), entry.get("clean")
    if isinstance(mutant, list) and isinstance(outcome, dict):
        outcomes[json.dumps(mutant)] = outcome
    elif isinstance(files, list) and isinstance(clean, bool):
        confirmations[tuple(str(file) for file in files)] = clean
    else:
        raise JournalRefused(f"{path} is not a gdmutant journal: line {number} could not be read")
