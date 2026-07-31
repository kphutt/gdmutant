"""Standalone CLI: ``gdmutant run <file.gd>`` mutates a GDScript file and reports survivors.

No AI required — a normal command-line tool (the #1 design goal). `run_mutation` is the testable
core (inject any `Runner`); `main` wires the real `GdUnit4Runner`.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from gdmutant import __version__
from gdmutant.adapters.gdscript import (
    ADAPTER,
    generate_mutants,
    is_valid_gdscript,
    unknown_ignore_operators,
)
from gdmutant.adapters.gdscript.runner import (
    DEFAULT_GUT_REPORT_PATH,
    DEFAULT_REPORT_PATH,
    DEFAULT_TIMEOUT,
    GdUnit4Runner,
    GutRunner,
)
from gdmutant.engine.adapter import Adapter
from gdmutant.engine.loop import BaselineFailed, MutationRun, run, run_paths
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import Operator
from gdmutant.engine.report import (
    all_survived_warning,
    console_summary,
    html_report,
    job_summary_markdown,
    stryker_report,
    stryker_report_multi,
)
from gdmutant.engine.runner import CommandRunner, Runner, RunWarning

#: Where the Godot editor's own binary lives inside the macOS app bundle — the path a macOS user
#: needs, whichever flag they end up putting it in.
_MACOS_GODOT_BINARY = "/Applications/Godot.app/Contents/MacOS/Godot"
_MISSING_GODOT_MACOS_HINT = (
    "  On macOS, Godot ships as an app bundle and is never on PATH — pass the binary directly:\n"
    f"    --godot {_MACOS_GODOT_BINARY}"
)
#: The same fact for ``--runner command``, where the binary goes inside ``--command`` and naming
#: ``--godot`` here would repeat the very mistake the hint above it is correcting.
_MISSING_GODOT_MACOS_COMMAND_HINT = (
    "  On macOS, Godot ships as an app bundle and is never on PATH — its binary is at\n"
    f"    {_MACOS_GODOT_BINARY}"
)


def _load_gdscript(source_path: str) -> str | None:
    """Read and validate a `.gd` source. Prints an error and returns None (the caller exits 2) if it
    can't be read or doesn't parse — never leaks an OSError or a lark traceback."""
    try:
        source = Path(source_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        # UnicodeDecodeError is a ValueError, not an OSError — catch it so a non-UTF-8 .gd file
        # exits gracefully instead of crashing.
        print(f"error: cannot read {source_path}: {error}", file=sys.stderr)
        return None
    if not is_valid_gdscript(source):
        print(f"error: {source_path} is not valid GDScript", file=sys.stderr)
        return None
    return source


#: Location vars git exports into a hook's environment (e.g. pre-push/pre-commit). If gdmutant is
#: invoked from such a hook (or any process git already set these for), inheriting them makes every
#: git call below operate on the *hook's* repo/worktree/index instead of the target file's own
#: repo — silently pointing --since / --require-clean at the wrong tree. Mirrors
#: tests/conftest.py's `_GIT_ENV_LEAKS` (kept separate: production code must not import from tests).
_GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


def _clean_git_env() -> dict[str, str]:
    """A copy of the current environment with `_GIT_ENV_LEAKS` removed, for every git subprocess
    gdmutant runs — so an inherited GIT_DIR/etc. (e.g. from a hook) can never redirect a git call
    away from the file/ref the user actually asked about."""
    return {key: value for key, value in os.environ.items() if key not in _GIT_ENV_LEAKS}


def _has_uncommitted_changes(source_path: str) -> bool:
    """True only if `source_path` is inside a git work tree and has uncommitted changes (tracked
    and modified, or untracked). False when it is clean, not under git, or git isn't available.

    gdmutant mutates the file in place and restores it in a ``finally`` — safe for a normal exit or
    Ctrl-C, but a hard kill (SIGKILL / power loss) could leave one swap on disk. Warning when the
    file is dirty lets the user commit or stash first. We only positively confirm *dirty*: anything
    we can't check (no git, not a repo) returns False, because gdmutant must run fine outside git.
    """
    path = Path(source_path)
    try:
        # No check=: git exits non-zero outside a repo (the default check=False), and we read the
        # returncode ourselves below. The `--` guards a filename that starts with "-".
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--", path.name],
            cwd=path.parent,
            capture_output=True,
            text=True,
            env=_clean_git_env(),
        )
    except OSError:
        return False  # git not installed, or the working directory is gone
    if completed.returncode != 0:
        return False  # not a git work tree, etc.
    # `--porcelain` prints one line per changed path and nothing at all when clean. Comparing to ""
    # (not bool()) also kills a `text=True`->False mutant: raw bytes never equal the str "".
    return completed.stdout != ""


def _missing_executable(error: BaselineFailed) -> str | None:
    """The executable name if `error` was caused by a not-found test runner binary, else None.

    The baseline runner raising `FileNotFoundError` (e.g. no ``godot`` on PATH) is a *setup*
    error, not a red suite — the loop wraps it as `BaselineFailed` but preserves the original via
    ``__cause__``. Reading the missing name here lets the CLI print an actionable hint instead of a
    raw errno, and exit 2 (bad setup) rather than 1 (baseline failed)."""
    cause = error.__cause__
    if isinstance(cause, FileNotFoundError):
        return cause.filename or "the test runner"
    return None


def _command_argv(runner: Runner) -> Sequence[str] | None:
    """The ``--command`` argv when `runner` is the exit-code `CommandRunner`, else ``None``.

    The one thing an error message needs to know to give advice that works: the JUnit runners take
    their executable from ``--godot``, and the exit-code runner takes it from the ``--command``
    string. Reading it off the runner object keeps the two callers from threading a mode string
    through every layer."""
    return runner.command if isinstance(runner, CommandRunner) else None


def _missing_executable_hint(filename: str, command: Sequence[str] | None = None) -> str:
    """A friendly, actionable message for a not-found test-runner executable named `filename`.

    `command` is the ``--command`` argv when the run used ``--runner command``, and ``None`` for
    the JUnit runners — it picks the advice, because **the two modes take their executable from
    different places**. Telling a ``--runner command`` user to "pass its full path with --godot"
    sends them to a flag that mode never reads: they set it, get the byte-identical error, and have
    to find the caveat in the README to get unstuck. So under ``--runner command`` the message says
    where the executable actually comes from, that ``--godot`` has no effect there, and shows their
    own command back with the path slot marked.

    Either way, a Godot-looking binary on macOS also gets the app-bundle path (the single most
    common first-run failure — most Godot users are on macOS, where Godot is never on PATH), phrased
    for the flag that mode actually uses."""
    lines = [f"error: could not run the test suite — executable {filename!r} not found."]
    if command is None:
        lines.append("  Install it and put it on your PATH, or pass its full path with --godot.")
        if sys.platform == "darwin" and "godot" in filename.lower():
            lines.append(_MISSING_GODOT_MACOS_HINT)
        return "\n".join(lines)
    # main() rejects an empty --command before a runner is built, so command[0] always exists — and
    # it is the authoritative name of what could not be executed, even if the OS error lost it.
    executable = command[0]
    rest = " ".join(command[1:])
    lines += [
        "  With --runner command the executable comes from the --command string itself. --godot",
        "  is not read in this mode, so setting it changes nothing. Put the full path inside",
        "  --command instead, quoted if it contains spaces:",
        f'    --command "<full path to {executable}>{" " + rest if rest else ""}"',
    ]
    if sys.platform == "darwin" and "godot" in executable.lower():
        lines.append(_MISSING_GODOT_MACOS_COMMAND_HINT)
    return "\n".join(lines)


#: The JUnit adapters' addon locations inside a Godot project (relative to the project dir).
_GDUNIT_ADDON_REL = Path("addons") / "gdUnit4"
_GUT_ADDON_REL = Path("addons") / "gut"


def _addon_hint(
    error: BaselineFailed,
    project_dir: str,
    *,
    framework: str,
    addon_rel: Path,
    install_hint: str,
) -> str | None:
    """An actionable message when a JUnit-adapter baseline failed *because its addon isn't
    installed*, else None. This is the second most common first-run failure after a missing godot
    binary — but unlike a missing binary it surfaces as an opaque ``RuntimeError`` ("<framework>
    wrote no report"), not ``FileNotFoundError``, so without this it fell through to a raw stderr
    dump with no next step. Gated on both the framework-specific error signature (the message names
    `framework`) *and* the addon being absent, so it never fires for the other JUnit runner or for
    the `command` runner (whose projects have no addon by design)."""
    cause = error.__cause__
    if not (
        isinstance(cause, RuntimeError)
        and framework in str(cause)
        and "wrote no report" in str(cause)
    ):
        return None
    if (Path(project_dir) / addon_rel).is_dir():
        return None  # addon is present — some other framework/Godot failure, not a setup problem
    return (
        f"error: the {framework} addon was not found in the project — {addon_rel}/ is missing "
        f"under {project_dir}.\n"
        f"  {install_hint}, or run without the addon via "
        '--runner command --command "<your headless test command>".'
    )


def _gdunit4_addon_hint(error: BaselineFailed, project_dir: str) -> str | None:
    """`_addon_hint` for GdUnit4 — fires only on GdUnit4's "wrote no report" signature."""
    return _addon_hint(
        error,
        project_dir,
        framework="GdUnit4",
        addon_rel=_GDUNIT_ADDON_REL,
        install_hint="Install GdUnit4 (Godot Asset Library)",
    )


def _gut_addon_hint(error: BaselineFailed, project_dir: str) -> str | None:
    """`_addon_hint` for GUT — fires only on GUT's "wrote no report" signature (a missing GUT addon
    can't load ``gut_cmdln.gd``, so the run writes no report). GUT's other crash-safety error
    (``tests == 0`` from a compile crash) does not match "wrote no report", so this never misfires
    on it."""
    return _addon_hint(
        error,
        project_dir,
        framework="GUT",
        addon_rel=_GUT_ADDON_REL,
        install_hint="Install GUT (Godot Asset Library)",
    )


#: Godot writes its import cache here the first time it opens a project. Its absence is a *fact*
#: about the checkout, not a guess about speed — which is why the notice below can be stated
#: outright without ever accusing a merely-slow project of being broken.
_GODOT_IMPORT_CACHE = ".godot"


def _cold_import_notice(project_dir: str) -> str | None:
    """A heads-up that `project_dir` has never been imported, or ``None`` when it has.

    On a fresh checkout Godot imports every asset before it will run anything, which on a real game
    is minutes of total silence — indistinguishable, to someone running gdmutant for the first time,
    from a hung tool. The JUnit runners pay that cost inside `Preparable.prepare`, which the engine
    announces. ``--runner command`` has no such hook and cannot grow one honestly: it is handed an
    opaque command, so it does not know which Godot binary (if any) to warm the cache with, and
    guessing one to run would be gdmutant executing a program the user never named. So it says so
    instead, and names the one command that fixes it.

    Reads only whether ``.godot/`` exists, so a project that is simply slow is never accused of
    anything — and a project that has been imported gets no notice at all."""
    if (Path(project_dir) / _GODOT_IMPORT_CACHE).is_dir():
        return None
    return (
        f"note: {project_dir} has no Godot import cache ({_GODOT_IMPORT_CACHE}/ is not there), so "
        "the first\n"
        "  run of --command imports every asset in the project before a single test executes. On a "
        "real\n"
        "  game that is minutes of silence. --runner command cannot warm the cache for you — it "
        "only\n"
        "  knows the command you gave it. Run this once, then run gdmutant again:\n"
        f"    godot --headless --path {project_dir} --import"
    )


def _report_baseline_failure(error: BaselineFailed, project_dir: str, runner: Runner) -> int:
    """Print the most actionable message for a `BaselineFailed` and return the exit code: 2 for a
    *setup* error (missing runner binary, or a GdUnit4 project with no addon), else 1 for a
    genuinely red baseline. Shared by the single- and multi-file run paths. `runner` is the one that
    failed — the missing-executable hint reads the mode off it (`_command_argv`) so its advice names
    the flag that mode actually uses."""
    missing = _missing_executable(error)
    if missing is not None:
        print(_missing_executable_hint(missing, _command_argv(runner)), file=sys.stderr)
        return 2
    # Try each JUnit adapter's addon hint. Each is gated on its own framework's error signature, so
    # at most one fires — no need to thread the runner kind through: a GUT "wrote no report" never
    # matches the GdUnit4 hint, and vice versa.
    for hint in (_gdunit4_addon_hint(error, project_dir), _gut_addon_hint(error, project_dir)):
        if hint is not None:
            print(hint, file=sys.stderr)
            return 2
    print(f"error: {error}", file=sys.stderr)
    return 1


#: Directories skipped when expanding a directory target to `.gd` files: third-party addons and the
#: engine/VCS dirs. A dot-prefixed dir (``.godot``, ``.git``) is skipped too (`_gd_files_under`).
_SKIP_DIRS = frozenset({"addons"})
#: Directory segments conventionally holding tests (GdUnit4's default lookup folder is ``test``;
#: GUT projects use ``test``/``tests``). A ``.gd`` under one of these is skipped on dir expansion.
_TEST_DIRS = frozenset({"test", "tests"})
#: Base classes a GDScript test suite extends — GdUnit4 (``GdUnitTestSuite``) and GUT (``GutTest``).
#: This is the *robust* signal: the two frameworks disagree on filename affixes, but a real test
#: suite always extends one of these, so a content check catches unconventionally-named tests. Not
#: anchored to the start of a line — Godot 4.x allows the single-line ``class_name Foo extends
#: GdUnitTestSuite`` form, where ``extends`` is mid-line; ``.`` never spans a newline, so the class
#: and its base still have to sit on one line to match.
_TEST_BASE_RE = re.compile(r"\bextends\b.*\b(GdUnitTestSuite|GutTest)\b")


def _has_test_name(name: str) -> bool:
    """True if `name` matches a GdUnit4/GUT test-file naming convention — ``test_*.gd`` (GUT / the
    GdUnit4 getting-started form), ``*_test.gd`` (GdUnit4 snake_case auto-detect), or ``*Test.gd``
    (GdUnit4 PascalCase auto-detect)."""
    return name.startswith("test_") or name.endswith("_test.gd") or name.endswith("Test.gd")


def _is_test_file(full_path: Path, relative: Path) -> bool:
    """True if the ``.gd`` at `full_path` (with path `relative` to the scanned directory) is a test
    suite — it lives under a ``test``/``tests`` directory, its name matches a test convention
    (`_has_test_name`), or its script ``extends GdUnitTestSuite``/``GutTest`` (`_TEST_BASE_RE`, the
    signal that survives the frameworks' conflicting naming). Mirrors how StrykerJS/cargo-mutants
    keep test code out of the mutation set by default."""
    if any(part in _TEST_DIRS for part in relative.parts[:-1]):
        return True
    if _has_test_name(relative.name):
        return True
    try:
        return _TEST_BASE_RE.search(full_path.read_text(encoding="utf-8")) is not None
    except (OSError, UnicodeDecodeError):
        return False


def _gd_files_under(directory: Path) -> tuple[list[str], list[str]]:
    """Every mutable ``.gd`` file under `directory` (recursive), sorted, plus the test-file paths
    skipped. Skips ``addons/`` (third-party), any dot-directory (``.godot``, ``.git``, …), and — so
    a directory target reports mutants on *your source*, not your test machinery —
    GdUnit4 / GUT test suites (`_is_test_file`). Name a test file explicitly to mutate it anyway;
    only directory expansion applies the test skip."""
    files: list[str] = []
    skipped: list[str] = []
    for path in directory.rglob("*.gd"):
        relative = path.relative_to(directory)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        if _is_test_file(path, relative):
            skipped.append(str(path))
            continue
        files.append(str(path))
    return sorted(files), skipped


def _unique_by_resolved(candidates: list[str]) -> list[str]:
    """`candidates` in order, dropping any whose resolved path was already seen — so overlapping or
    repeated directory args don't double-count the same file."""
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        resolved = str(Path(candidate).resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def _resolved_set(candidates: list[str]) -> set[str]:
    """The resolved absolute paths of `candidates`, de-duplicated — for set arithmetic against the
    final mutated set (e.g. was a skipped/excluded file mutated anyway via an explicit arg?)."""
    return {str(Path(candidate).resolve()) for candidate in candidates}


def _matches_glob(path: str, patterns: list[str]) -> bool:
    """True if `path` matches any of `patterns` (`fnmatch` — ``*`` spans ``/``, so ``*.gd`` matches
    everything and ``*/vendor/*`` any file under a ``vendor`` dir). Each pattern is tried against
    both the full path and the bare filename, so ``foo.gd`` skips that file anywhere without needing
    the leading directories."""
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def _expand_sources(paths: list[str], exclude: list[str] | None = None) -> list[str] | None:
    """Expand each given path (a ``.gd`` file or a directory) into a de-duplicated, sorted list of
    `.gd` files. A directory skips GdUnit4 test files (`_gd_files_under`) and any file matching an
    `exclude` glob, noting how many of each (counted per *unique* file, so duplicate dir
    args don't inflate the note); an **explicit** file path is always included (name a test/excluded
    file to mutate it). Returns None (after printing an error) when nothing resolves to a `.gd` file
    — the "point it at a directory" adoption case. Existence/validity of each file is
    checked later by `_load_gdscript`."""
    exclude = exclude or []
    collected: list[str] = []
    skipped: list[str] = []
    excluded: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found, dir_skipped = _gd_files_under(path)
            skipped.extend(dir_skipped)
            for gd_file in found:
                (excluded if _matches_glob(gd_file, exclude) else collected).append(gd_file)
        else:
            collected.append(raw)
    unique = _unique_by_resolved(collected)
    # A file matched by a skip/exclude rule during a directory scan but *also* named explicitly (or
    # reached via another dir arg) is mutated regardless — subtract the final set so the note never
    # tells the user to "name one explicitly to mutate it" for a file they already did.
    included = _resolved_set(unique)
    n_excluded = len(_resolved_set(excluded) - included)
    if not unique:
        detail = " (every .gd file matched --exclude)" if n_excluded else ""
        print(f"error: no .gd files found in the given path(s){detail}", file=sys.stderr)
        return None
    tests_skipped = len(_resolved_set(skipped) - included)
    if tests_skipped:
        print(
            f"note: skipped {tests_skipped} test file(s) (test/ dirs, *_test.gd, or "
            "extends GdUnitTestSuite/GutTest) — name one explicitly to mutate it",
            file=sys.stderr,
        )
    if n_excluded:
        print(
            f"note: excluded {n_excluded} file(s) matching --exclude — name one explicitly "
            "to mutate it",
            file=sys.stderr,
        )
    return sorted(unique)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _changed_lines(ref: str, files: list[str]) -> dict[str, set[int]] | None:
    """Map each of `files` (by resolved path) to the line numbers added/modified since git `ref`
    the ``+`` side of ``git diff --unified=0``, i.e. the current-tree lines a change
    touched. A file with no changes maps to an empty set. Returns None (after printing an error) if
    `ref` is unknown or a file isn't in a git repo — a bad base ref is a setup error, not a silently
    empty run."""
    changed: dict[str, set[int]] = {}
    for raw in files:
        path = Path(raw).resolve()
        try:
            result = subprocess.run(
                ["git", "diff", "--unified=0", ref, "--", str(path)],
                cwd=str(path.parent),
                capture_output=True,
                text=True,
                env=_clean_git_env(),
            )
        except OSError as error:
            print(f"error: could not run git for --since {ref}: {error}", file=sys.stderr)
            return None
        if result.returncode != 0:
            lines_err = result.stderr.strip().splitlines()
            detail = f": {lines_err[-1]}" if lines_err else ""
            print(f"error: git diff for --since {ref} failed{detail}", file=sys.stderr)
            return None
        touched: set[int] = set()
        for match in _HUNK_RE.finditer(result.stdout):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            touched.update(range(start, start + count))  # count 0 (pure deletion) adds nothing
        if not touched and path.is_file():
            # `git diff` is silent on a brand-new file that was never `git add`-ed, so an untracked
            # file maps to an empty diff. Treat it as *fully* changed (every line is new), not
            # "nothing to mutate" — silently skipping a new file is exactly the wrong-report failure
            # mode a mutation tool must avoid. (A committed-new file already diffs as fully added.)
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(path)],
                cwd=str(path.parent),
                capture_output=True,
                text=True,
                env=_clean_git_env(),
            )
            if tracked.returncode != 0:  # untracked
                touched = set(range(1, len(path.read_text(encoding="utf-8").splitlines()) + 1))
        changed[str(path)] = touched
    return changed


def _diff_scoped(base: Adapter, changed: dict[str, set[int]]) -> Adapter:
    """Wrap `base` so it emits only mutants on lines changed since the base ref — a filter
    on the generated set, keyed by resolved path; a file with no changed lines yields no mutants.
    Application is unchanged, so this rides the engine's adapter seam (NF-3) with no engine edit."""

    def generate(path: str, source: str, catalog: tuple[Operator, ...]) -> list[Mutant]:
        lines = changed.get(str(Path(path).resolve()), set())
        return [m for m in base.generate_mutants(path, source, catalog) if m.span.line in lines]

    return Adapter(generate_mutants=generate, apply_mutant=base.apply_mutant)


def _drop_unparseable(files: list[str]) -> tuple[list[str], list[str]]:
    """Partition `files` into (parseable, unparseable). A file that can't be read or that gdtoolkit
    can't parse — e.g. a grammar gap on real-world GDScript (a comment inside a line-continuation, a
    newer Godot annotation) — is dropped so one odd file in a directory doesn't abort the whole run
    Silent; the caller warns with a summary."""
    good: list[str] = []
    bad: list[str] = []
    for candidate in files:
        try:
            source = Path(candidate).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            bad.append(candidate)
            continue
        (good if is_valid_gdscript(source) else bad).append(candidate)
    return good, bad


def _default_project_dir(raw_paths: list[str], files: list[str]) -> str:
    """The Godot project dir to run tests from when ``--project`` isn't given: a lone directory
    target *is* the project; otherwise the (first) source file's own directory. That last case is a
    **best-effort** guess for multiple/nested targets — there's no single project root for disparate
    paths, so pass ``--project`` when the guess is wrong."""
    if len(raw_paths) == 1 and Path(raw_paths[0]).is_dir():
        return str(Path(raw_paths[0]))
    return str(Path(files[0]).resolve().parent)


def _warn_unknown_ignore_operators(source: str) -> None:
    """Warn (stderr) for each malformed ``# gdmutant: ignore[...]`` scope — an unknown operator name
    (a likely typo) or empty brackets — that silently suppresses nothing. Never fails the run."""
    for line, name in unknown_ignore_operators(source):
        if name:
            detail = f"'# gdmutant: ignore[{name}]' on line {line} names an unknown operator"
        else:
            detail = f"'# gdmutant: ignore[]' on line {line} has empty brackets"
        print(
            f"warning: {detail} — it suppresses nothing "
            "(name a real operator, or drop the brackets to ignore the whole line).",
            file=sys.stderr,
        )


def _report_path_problem(path: str | None, flag: str, *, stdout_ok: bool) -> str | None:
    """A human message (naming `flag`) if a report `path` can't be written, else None — checked
    *before* the run so a long pass (minutes of booting Godot per mutant) never ends on an avoidable
    write error. ``None`` (no report) is always fine; only a real file path is validated,
    and only its parent directory (the file itself needn't exist yet). ``-`` means stdout, valid
    only where `stdout_ok` (``--json``); for a file-only flag (``--html``) it's rejected rather than
    written as a file literally named ``-``."""
    if path is None:
        return None
    if path == "-":
        return None if stdout_ok else f"{flag} needs a file path — stdout ('-') isn't supported"
    parent = Path(path).parent
    if not parent.exists():
        return f"{flag} directory does not exist: {parent}"
    if not os.access(parent, os.W_OK):
        return f"{flag} directory is not writable: {parent}"
    return None


def list_mutants(source_path: str, only_lines: set[int] | None = None) -> int:
    """Print every mutant gdmutant would generate for `source_path` **without running any tests** —
    a Godot-free way to see the tool work. With `only_lines` (diff-scoped) only mutants on
    those lines are listed. Returns 0, or 2 if the source can't be read/parsed."""
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    _warn_unknown_ignore_operators(source)
    mutants = generate_mutants(str(Path(source_path)), source)
    if only_lines is not None:
        mutants = [m for m in mutants if m.span.line in only_lines]
    print(f"{len(mutants)} mutants for {source_path}:")
    for m in mutants:
        loc = f"{m.path}:{m.span.line}:{m.span.column}"
        # A suppressed mutant is still listed (it's generated), flagged so the annotation shows.
        suppressed = ""
        if m.ignore_reason is not None:
            suppressed = f"  (ignored: {m.ignore_reason})" if m.ignore_reason else "  (ignored)"
        print(f"  {loc}  {m.operator_id}  {m.describe_change()}{suppressed}")
    return 0


def run_mutation(
    source_path: str,
    project_dir: str,
    runner: Runner,
    *,
    timeout: float | None = None,
    json_path: str | None = None,
    html_path: str | None = None,
    require_clean: bool = False,
    changed: dict[str, set[int]] | None = None,
    jobs: int = 1,
    step_summary: bool = False,
) -> int:
    """Mutate `source_path`, run via `runner`, print the summary, optionally write a report file.

    Returns 0 on a completed pass — **survivors are report output, not a failure** (FG-6.2) — 1 if
    the unmutated baseline suite fails, and 2 if the source can't be read, isn't valid GDScript, the
    project directory doesn't exist, `require_clean` is set and the source has uncommitted changes,
    the test-runner executable isn't found, or a report (JSON/HTML) can't be written.
    """
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    _warn_unknown_ignore_operators(source)
    # Validate project_dir up front: the runner shells out with `cwd=project_dir`, so a missing dir
    # raises FileNotFoundError too — indistinguishable by type from a missing `godot`. Catching it
    # here keeps _missing_executable's FileNotFoundError unambiguously about the executable, and
    # gives a bad --project its own clear message.
    if not Path(project_dir).is_dir():
        print(f"error: project directory not found: {project_dir}", file=sys.stderr)
        return 2
    # Preflight the report paths up front: a run boots Godot per mutant for minutes, so an
    # unwritable --json/--html target must fail now, not after the whole pass completes.
    for problem in (
        _report_path_problem(json_path, "--json", stdout_ok=True),
        _report_path_problem(html_path, "--html", stdout_ok=False),
    ):
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
    # In-place-mutation safety: the run edits the source file per mutant. Warn (or, with
    # require_clean, refuse) on a dirty tree so a hard interrupt can't lose uncommitted work.
    # Ordered after the read/parse validation above so a genuine read error is reported first,
    # not preceded by a "Continuing ..." warning.
    if _has_uncommitted_changes(source_path):
        if require_clean:
            print(
                f"error: {source_path} has uncommitted changes and --require-clean was given. "
                "Commit or stash first.",
                file=sys.stderr,
            )
            return 2
        print(
            f"warning: {source_path} has uncommitted changes — gdmutant mutates it in place "
            "(restoring it when done), so a hard kill could leave it modified. Commit or stash "
            "first to be safe. Continuing ...",
            file=sys.stderr,
        )
    path = Path(source_path)
    # Progress goes to stderr unconditionally: a real run boots Godot per mutant, so without it the
    # tool looks hung. stderr keeps stdout clean for --json - (pure JSON) and the human summary.
    adapter = ADAPTER if changed is None else _diff_scoped(ADAPTER, changed)
    try:
        result = run(
            project_dir,
            str(path),
            source,
            runner,
            adapter,
            timeout=timeout,
            progress=lambda line: print(line, file=sys.stderr),
            jobs=jobs,
        )
    except BaselineFailed as error:
        return _report_baseline_failure(error, project_dir, runner)
    # With --json - the report goes to stdout, so keep the human summary on stderr — an agent
    # capturing stdout then gets pure JSON.
    report_to_stdout = json_path == "-"
    print(console_summary(result), file=sys.stderr if report_to_stdout else sys.stdout)
    # A run whose baseline passed yet every mutant survived is usually a test command that never
    # touches the mutated file, not a worthless suite — warn (stderr, score/exit code unchanged).
    warning = all_survived_warning(result)
    if warning is not None:
        print(warning, file=sys.stderr)
    _emit_runner_warning(runner)
    if step_summary:
        _emit_step_summary(result)
    stryker = stryker_report(result, str(path), source, "gdscript")
    return _write_reports(stryker, json_path, html_path)


def _emit_runner_warning(runner: Runner) -> None:
    """Print a runner's optional post-run warning (e.g. `GutRunner`'s non-determinism canary) to
    stderr, on the same surface as `all_survived_warning`. A no-op for a runner that doesn't
    implement `RunWarning` or has nothing to report; it never changes the score or the exit code."""
    if isinstance(runner, RunWarning):
        warning = runner.run_warning()
        if warning is not None:
            print(warning, file=sys.stderr)


def _emit_step_summary(run: MutationRun) -> None:
    """Emit the survivor explanations as Markdown for the ``--report step-summary`` reporter. The
    destination is the file named by ``$GITHUB_STEP_SUMMARY`` (appended, as GitHub Actions expects)
    when that env var is set — so the survivors land right in the CI run summary, where reviewers
    look — and stdout otherwise, so the reporter is useful locally too. Advisory output: a write
    failure warns but never changes the score or the exit code."""
    markdown = job_summary_markdown(run)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        print(markdown)
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as error:
        print(
            f"warning: could not write the job summary to {summary_path}: {error}",
            file=sys.stderr,
        )


def _write_reports(stryker: dict[str, object], json_path: str | None, html_path: str | None) -> int:
    """Write the ``--json`` / ``--html`` reports (both rendered from the same Stryker dict). Returns
    2 on a write error, else 0. ``--json -`` streams JSON to stdout; the caller has already routed
    the human summary appropriately. Shared by the single- and multi-file run paths."""
    if json_path == "-":
        print(json.dumps(stryker, indent=2))
    elif json_path is not None:
        try:
            Path(json_path).write_text(json.dumps(stryker, indent=2), encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write report to {json_path}: {error}", file=sys.stderr)
            return 2
        print(f"\nWrote report to {json_path}")
    if html_path is not None:
        try:
            Path(html_path).write_text(html_report(stryker), encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write HTML report to {html_path}: {error}", file=sys.stderr)
            return 2
        print(f"\nWrote HTML report to {html_path} — open it in a browser.")
    return 0


def run_mutation_paths(
    source_paths: list[str],
    project_dir: str,
    runner: Runner,
    *,
    timeout: float | None = None,
    json_path: str | None = None,
    html_path: str | None = None,
    require_clean: bool = False,
    changed: dict[str, set[int]] | None = None,
    jobs: int = 1,
    step_summary: bool = False,
) -> int:
    """Mutate several `.gd` files against one project in a single pass — the baseline runs **once**
    and the score is aggregated across every file, with one merged report. Same return
    codes as `run_mutation`. Every source is loaded/validated before anything runs (a bad file fails
    fast). `jobs` parallelizes each file's mutants (see `run_mutation`)."""
    sources: dict[str, str] = {}
    for source_path in source_paths:
        source = _load_gdscript(source_path)
        if source is None:
            return 2
        _warn_unknown_ignore_operators(source)
        sources[str(Path(source_path))] = source
    if not Path(project_dir).is_dir():
        print(f"error: project directory not found: {project_dir}", file=sys.stderr)
        return 2
    for problem in (
        _report_path_problem(json_path, "--json", stdout_ok=True),
        _report_path_problem(html_path, "--html", stdout_ok=False),
    ):
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
    for source_path in source_paths:
        if _has_uncommitted_changes(source_path):
            if require_clean:
                print(
                    f"error: {source_path} has uncommitted changes and --require-clean was given. "
                    "Commit or stash first.",
                    file=sys.stderr,
                )
                return 2
            print(
                f"warning: {source_path} has uncommitted changes — gdmutant mutates it in place "
                "(restoring it when done). Commit or stash first to be safe. Continuing ...",
                file=sys.stderr,
            )
    adapter = ADAPTER if changed is None else _diff_scoped(ADAPTER, changed)
    try:
        runs = run_paths(
            project_dir,
            sources,
            runner,
            adapter,
            timeout=timeout,
            progress=lambda line: print(line, file=sys.stderr),
            jobs=jobs,
        )
    except BaselineFailed as error:
        return _report_baseline_failure(error, project_dir, runner)
    report_to_stdout = json_path == "-"
    out = sys.stderr if report_to_stdout else sys.stdout
    print(f"\n{len(runs)} files:", file=out)
    for path, file_run in runs.items():
        score = file_run.mutation_score
        score_str = "n/a" if score is None else f"{score * 100:.1f}%"
        scored = file_run.detected + file_run.survived
        print(f"  {path}: {score_str}  ({file_run.detected} detected / {scored})", file=out)
    print("", file=out)
    # Survivors carry their own path, so one aggregate summary lists them per file with the overall
    # score across every file's mutants.
    aggregate = MutationRun(tuple(o for r in runs.values() for o in r.outcomes))
    print(console_summary(aggregate), file=out)
    # Across every file: baseline passed but nothing was detected — usually the test command never
    # exercised the mutated files, not a suite that catches nothing (stderr, score/exit unchanged).
    warning = all_survived_warning(aggregate)
    if warning is not None:
        print(warning, file=sys.stderr)
    _emit_runner_warning(runner)
    if step_summary:
        _emit_step_summary(aggregate)
    stryker = stryker_report_multi({p: (r, sources[p]) for p, r in runs.items()}, "gdscript")
    return _write_reports(stryker, json_path, html_path)


#: Config-file (`.gdmutant.toml`) key -> argparse dest. Keys mirror the CLI flag names (so a project
#: writes `runner = "command"`, `report-path = "..."`), except `command` maps to the `test_command`
#: dest. `source`, `--json`, and `--dry-run` are deliberately absent — they're per-invocation, not
#: persistent project settings.
_CONFIG_KEY_TO_DEST = {
    "project": "project",
    "runner": "runner",
    "command": "test_command",
    "godot": "godot",
    "tests": "tests",
    "report-path": "report_path",
    "timeout": "timeout",
    "require-clean": "require_clean",
    "exclude": "exclude",
}
_CONFIG_FILENAME = ".gdmutant.toml"


def _load_config(path: Path | None = None) -> dict[str, object] | None:
    """Load per-project defaults from `.gdmutant.toml` (a flat table of flag-named keys) in the
    current directory. Returns a dict keyed by argparse *dest* (so it can seed `set_defaults`), or
    ``{}`` if the file is absent, or ``None`` if it can't be parsed/validated (the caller exits 2).
    CLI flags still win — these become argparse defaults, which an explicit flag overrides. Unknown
    keys warn
    and are skipped (a likely typo shouldn't silently do nothing)."""
    path = path or Path(_CONFIG_FILENAME)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"error: cannot read {path}: {error}", file=sys.stderr)
        return None
    settings: dict[str, object] = {}
    for key, value in raw.items():
        dest = _CONFIG_KEY_TO_DEST.get(key)
        if dest is None:
            print(f"warning: {path}: unknown key '{key}' — ignoring", file=sys.stderr)
            continue
        settings[dest] = value
    # Validate settings types up front — set_defaults bypasses argparse's own type/choices
    # checks, so a bad value would otherwise fail confusingly deep in the run.
    for key in ("project", "godot", "tests", "report-path"):
        dest = _CONFIG_KEY_TO_DEST[key]
        val = settings.get(dest)
        if val is not None and not isinstance(val, str):
            print(f"error: {path}: '{key}' must be a string", file=sys.stderr)
            return None
    val = settings.get("test_command")
    if val is not None and not isinstance(val, str):
        print(f"error: {path}: 'command' must be a string", file=sys.stderr)
        return None

    if not isinstance(settings.get("timeout", 0), (int, float)) or isinstance(
        settings.get("timeout"), bool
    ):
        print(f"error: {path}: 'timeout' must be a number", file=sys.stderr)
        return None
    if not isinstance(settings.get("require_clean", False), bool):
        print(f"error: {path}: 'require-clean' must be true or false", file=sys.stderr)
        return None
    if settings.get("runner") not in (None, "gdunit4", "gut", "command"):
        print(f"error: {path}: 'runner' must be 'gdunit4', 'gut', or 'command'", file=sys.stderr)
        return None
    exclude = settings.get("exclude")
    if exclude is not None and (
        not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude)
    ):
        print(f"error: {path}: 'exclude' must be a list of glob strings", file=sys.stderr)
        return None
    return settings


def build_parser(config: dict[str, object] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdmutant",
        description="Mutation testing for GDScript (and, in time, other languages).",
    )
    parser.add_argument("--version", action="version", version=f"gdmutant {__version__}")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="mutate GDScript files and report survivors")
    run_parser.add_argument(
        "source",
        nargs="+",
        metavar="path",
        help="one or more .gd files or directories to mutate (a directory mutates every .gd under "
        "it, recursively, excluding addons/ and dot-dirs)",
    )
    run_parser.add_argument("--project", help="the Godot project dir (default: the source's dir)")
    run_parser.add_argument(
        "--runner",
        choices=("gdunit4", "gut", "command"),
        default="gdunit4",
        help="test runner: gdunit4 or gut (both JUnit XML) or command (any harness, by exit code) "
        "(default: gdunit4)",
    )
    run_parser.add_argument(
        "--command",
        dest="test_command",  # not "command": that dest holds the subcommand name ("run")
        help="test command for --runner command (exit 0 = pass), e.g. "
        "'godot --headless --script res://tests/run_tests.gd'",
    )
    run_parser.add_argument(
        "--godot", default="godot", help="the Godot executable (default: godot)"
    )
    run_parser.add_argument(
        "--tests",
        default="res://test",
        help="the test directory (gdunit4's -a / gut's -gdir) (default: res://test)",
    )
    run_parser.add_argument(
        "--report-path",
        default=None,
        help="JUnit-XML report path, relative to the project dir (default: per runner — "
        f"gdunit4 {DEFAULT_REPORT_PATH}, gut {DEFAULT_GUT_REPORT_PATH})",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-mutant test-run timeout, in seconds (default: derived from the baseline run — "
        "10x its wall-clock, so a hanging mutant is caught in seconds, not minutes)",
    )
    run_parser.add_argument(
        "--json", dest="json_path", help="write the Stryker JSON report here (use - for stdout)"
    )
    run_parser.add_argument(
        "--html",
        dest="html_path",
        help="write a ready-to-open HTML report here — one self-contained file (no network "
        "needed) showing each survivor on its own source line, with the gap explained",
    )
    run_parser.add_argument(
        "--require-clean",
        action="store_true",
        help="refuse to run if the source file has uncommitted git changes (default: warn only)",
    )
    run_parser.add_argument(
        "--no-require-clean",
        dest="require_clean",
        action="store_false",
        help="allow running even if the source file has uncommitted git changes",
    )
    run_parser.add_argument(
        "--exclude",
        action="append",
        metavar="glob",
        help="glob of files to skip when expanding a directory (repeatable; matched against each "
        "path and its filename). An explicitly named file is never excluded. Combines with any "
        "'exclude' list in .gdmutant.toml.",
    )
    run_parser.add_argument(
        "--since",
        metavar="ref",
        help="only mutate lines changed since this git ref (e.g. main, HEAD~1) — the per-PR "
        "diff-scoped mode; a much faster, gate-able signal than a whole-file run",
    )
    run_parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        metavar="N",
        default=1,
        help="evaluate N mutants in parallel, each on its own copy of the project (default: 1 = "
        "serial), for a faster run with the same verdicts — process isolation, and the per-mutant "
        "timeout is scaled by N so contention can't cause a false timeout. Bounded by your "
        "cores/RAM; a plain per-worker copy is made per job.",
    )
    run_parser.add_argument(
        "--report",
        action="append",
        choices=("step-summary",),
        metavar="KIND",
        help="emit an extra report (repeatable). 'step-summary' renders the surviving mutants and "
        "their explanations as Markdown to the GitHub Actions job summary ($GITHUB_STEP_SUMMARY) "
        "when it's set, and to stdout otherwise.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the mutants without running any tests (no Godot needed)",
    )
    # Config values seed the run subparser's defaults, so an explicit CLI flag still overrides them
    # (argparse precedence: passed value > set_defaults > add_argument default). `exclude`
    # is the exception: it's resolved in main() as config + CLI (additive, like every mutation
    # tool's exclude list), so it must NOT seed the append-action default here.
    if config:
        run_parser.set_defaults(**{k: v for k, v in config.items() if k != "exclude"})
    return parser


def _force_utf8(stream: object) -> None:
    """Reconfigure a text stream to UTF-8 so the CLI's Unicode output prints on Windows, whose
    console defaults to cp1252 and raises ``UnicodeEncodeError`` mid-print. Found running the
    gdUnit4 dogfood on Windows: every mutant generated fine, but the run died on *output*.
    ``errors="replace"`` degrades a stray glyph to ``?`` instead of crashing. Reached via
    ``getattr`` because ``reconfigure`` is on ``TextIOWrapper``, not the ``TextIO`` type of
    ``sys.stdout`` (a direct call fails mypy); a stream without it, or that errors, is skipped."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(ValueError, OSError):
        reconfigure(encoding="utf-8", errors="replace")


_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])/(.*)$")


def _split_command(command: str) -> list[str]:
    """Split a ``--command`` string into an argv list, honouring the host OS's path syntax.

    Default ``shlex.split`` runs in POSIX mode, where a backslash is an *escape* — so on Windows an
    unquoted native path like ``C:\\Godot\\godot.exe`` loses its separators (``C:Godotgodot.exe``)
    and the runner is "not found" (a known path parsing bug). On Windows we lex in non-POSIX
    mode instead, which keeps backslashes literal — matching what a user naturally types and how
    `CreateProcess` reads an argv. Unbalanced quotes still raise ``ValueError`` in either mode, so
    the caller's clean exit-2 for that case is unaffected.

    Two Windows fix-ups follow, because the host runs the command via `CreateProcess` (never a
    shell), so only Windows-resolvable tokens work:
    - Non-POSIX mode leaves a token's surrounding quotes in place (POSIX mode strips them), so a
      quoted path with spaces (``"C:\\Program Files\\Godot\\godot.exe"``) would keep literal quotes
      and fail to resolve — strip one matched outer pair.
    - An MSYS/Git-Bash drive path (``/c/Godot/godot.exe``, common when invoking from Git Bash) is
      not resolvable by `CreateProcess`; rewrite it to Windows form (``C:\\Godot\\godot.exe``).

    A dedicated Windows lexer (``mslex``/``oslex``) is deliberately *not* used: it solves only the
    lexing half, not the MSYS path rewrite, so it wouldn't remove this helper — and it would add a
    dependency to a repo heading public. POSIX hosts keep the standard, correct default.
    """
    if os.name != "nt":
        return shlex.split(command)
    return [_normalize_windows_token(token) for token in shlex.split(command, posix=False)]


def _normalize_windows_token(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    drive = _MSYS_DRIVE_RE.match(token)
    if drive:
        token = f"{drive.group(1).upper()}:\\" + drive.group(2).replace("/", "\\")
    return token


def main(argv: Sequence[str] | None = None) -> int:
    # Make the CLI's Unicode output survive a Windows cp1252 console (see `_force_utf8`).
    for _stream in (sys.stdout, sys.stderr):
        _force_utf8(_stream)
    config = _load_config()
    if config is None:
        return 2  # a malformed/invalid .gdmutant.toml is a setup error
    parser = build_parser(config)
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.jobs < 1:
            print("error: --jobs must be a positive integer", file=sys.stderr)
            return 2
        # Excludes are additive: any .gdmutant.toml `exclude` list plus every --exclude on the CLI
        # (both narrow a directory target; neither can drop an explicitly named file).
        config_exclude = config.get("exclude")
        exclude = list(config_exclude) if isinstance(config_exclude, list) else []
        if args.exclude:
            exclude += args.exclude
        files = _expand_sources(args.source, exclude)
        if files is None:
            return 2
        # Resilience: a file discovered by expanding a *directory* that gdtoolkit can't
        # parse is skipped with a warning and the rest are mutated — one odd file (a grammar gap on
        # real GDScript) shouldn't zero out a whole directory run. A file named *explicitly* stays
        # strict, at any count: it's never dropped here, so its parse error exits 2 downstream —
        # a direct request that fails must never be silent (count alone can't tell the two apart).
        explicit = {str(Path(raw).resolve()) for raw in args.source if not Path(raw).is_dir()}
        discovered = [f for f in files if str(Path(f).resolve()) not in explicit]
        _, unparseable = _drop_unparseable(discovered)
        if unparseable:
            dropped = set(unparseable)
            files = [f for f in files if f not in dropped]
            print(
                f"note: skipped {len(unparseable)} directory file(s) gdtoolkit couldn't parse — "
                f"mutating the other {len(files)}:",
                file=sys.stderr,
            )
            for path in unparseable:
                print(f"  {path}", file=sys.stderr)
        if not files:
            print("error: no parseable .gd files in the given path(s)", file=sys.stderr)
            return 2
        # Diff-scoped mode: restrict mutation to lines changed since a base ref. A bad ref
        # is a setup error; no changed lines at all is a clean no-op (exit 0), not a failed run.
        changed: dict[str, set[int]] | None = None
        if args.since:
            changed = _changed_lines(args.since, files)
            if changed is None:
                return 2
            if not any(changed.values()):
                print(
                    f"no lines changed since {args.since} in the given path(s) — nothing to mutate",
                    file=sys.stderr,
                )
                return 0
        if args.dry_run:
            ignored = [
                flag
                for flag, value, default in (
                    ("--project", args.project, None),
                    ("--runner", args.runner, "gdunit4"),
                    ("--command", args.test_command, None),
                    ("--godot", args.godot, "godot"),
                    ("--tests", args.tests, "res://test"),
                    ("--report-path", args.report_path, None),
                    ("--timeout", args.timeout, None),
                    ("--require-clean", args.require_clean, False),
                    ("--json", args.json_path, None),
                    ("--html", args.html_path, None),
                    ("--report", args.report, None),
                )
                if value != default
            ]
            if ignored:
                print(
                    f"note: --dry-run runs no tests, so {', '.join(ignored)} "
                    f"{'is' if len(ignored) == 1 else 'are'} ignored",
                    file=sys.stderr,
                )
            for index, gd_file in enumerate(files):
                if index:
                    print()  # blank line between files
                only = changed.get(str(Path(gd_file).resolve())) if changed is not None else None
                rc = list_mutants(gd_file, only_lines=only)
                if rc != 0:
                    return rc
            return 0
        project_dir = args.project or _default_project_dir(args.source, files)
        # The runner's own timeout is the *baseline* budget (the loop derives per-mutant budgets
        # from the baseline's wall-clock); without an explicit --timeout it falls back to the
        # historical default so a legitimately slow baseline still completes.
        baseline_timeout = DEFAULT_TIMEOUT if args.timeout is None else args.timeout
        runner: Runner
        if args.runner == "command":
            # Split OS-aware (see `_split_command`), then require a non-empty result: this rejects a
            # missing --command AND a whitespace-only one (which would otherwise become `[]` -> a
            # confusing subprocess IndexError deep in the run). Unbalanced quotes make shlex raise
            # ValueError — surface it as a clean exit-2, not a raw traceback, like every other
            # bad-input case here.
            try:
                test_command = _split_command(args.test_command) if args.test_command else []
            except ValueError as error:
                print(f"error: could not parse --command: {error}", file=sys.stderr)
                return 2
            if not test_command:
                print("error: --runner command requires a non-empty --command", file=sys.stderr)
                return 2
            runner = CommandRunner(command=test_command, timeout=baseline_timeout)
            # The JUnit runners warm Godot's import cache themselves (Preparable.prepare, which the
            # engine announces). This mode can't, so an un-imported project gets told up front
            # rather than looking hung for minutes on its very first run.
            notice = _cold_import_notice(project_dir)
            if notice is not None:
                print(notice, file=sys.stderr)
        else:
            if args.test_command:
                # --command only applies to --runner command; flag it rather than silently drop it.
                print("note: --command is ignored unless --runner command is set", file=sys.stderr)
            # gdunit4 and gut are peer JUnit adapters over one contract (docs/decisions/0011); each
            # owns its own default report layout, resolved here when --report-path is omitted.
            if args.runner == "gut":
                runner = GutRunner(
                    test_dir=args.tests,
                    godot=args.godot,
                    report_path=args.report_path or DEFAULT_GUT_REPORT_PATH,
                    timeout=baseline_timeout,
                )
            else:
                runner = GdUnit4Runner(
                    test_path=args.tests,
                    godot=args.godot,
                    report_path=args.report_path or DEFAULT_REPORT_PATH,
                    timeout=baseline_timeout,
                )
        common = {
            "timeout": args.timeout,
            "json_path": args.json_path,
            "html_path": args.html_path,
            "require_clean": args.require_clean,
            "changed": changed,
            "jobs": args.jobs,
            "step_summary": bool(args.report) and "step-summary" in args.report,
        }
        if len(files) == 1:
            return run_mutation(files[0], project_dir, runner, **common)
        return run_mutation_paths(files, project_dir, runner, **common)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
