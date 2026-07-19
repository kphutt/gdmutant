"""Standalone CLI: ``gdmutant run <file.gd>`` mutates a GDScript file and reports survivors.

No AI required — a normal command-line tool (the #1 design goal). `run_mutation` is the testable
core (inject any `Runner`); `main` wires the real `GdUnit4Runner`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from gdmutant import __version__
from gdmutant.adapters.gdscript import (
    generate_mutants,
    is_valid_gdscript,
    unknown_ignore_operators,
)
from gdmutant.adapters.gdscript.runner import (
    DEFAULT_REPORT_PATH,
    DEFAULT_TIMEOUT,
    GdUnit4Runner,
)
from gdmutant.engine.loop import BaselineFailed, MutationRun, run, run_paths
from gdmutant.engine.report import (
    console_summary,
    html_report,
    stryker_report,
    stryker_report_multi,
)
from gdmutant.engine.runner import CommandRunner, Runner

_MISSING_GODOT_MACOS_HINT = (
    "  On macOS, Godot ships as an app bundle and is never on PATH — pass the binary directly:\n"
    "    --godot /Applications/Godot.app/Contents/MacOS/Godot"
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


def _missing_executable_hint(filename: str) -> str:
    """A friendly, actionable message for a not-found test-runner executable named `filename`.

    Generic by default (install it / pass its full path); adds the macOS Godot app-bundle path when
    the missing binary looks like Godot — the single most common first-run failure, since most Godot
    users are on macOS where Godot is never on PATH (LOD-87)."""
    lines = [
        f"error: could not run the test suite — executable {filename!r} not found.",
        "  Install it and put it on your PATH, or pass its full path with --godot.",
    ]
    if sys.platform == "darwin" and "godot" in filename.lower():
        lines.append(_MISSING_GODOT_MACOS_HINT)
    return "\n".join(lines)


#: The GdUnit4 addon's location inside a Godot project (relative to the project dir).
_GDUNIT_ADDON_REL = Path("addons") / "gdUnit4"


def _gdunit4_addon_hint(error: BaselineFailed, project_dir: str) -> str | None:
    """An actionable message when the gdunit4 baseline failed *because the addon isn't installed*,
    else None. This is the second most common first-run failure after a missing godot binary — but
    unlike a missing binary it surfaces as an opaque ``RuntimeError`` ("GdUnit4 wrote no report"),
    not ``FileNotFoundError``, so without this it fell through to a raw stderr dump with no next
    step (LOD-110). Gated on both the GdUnit4-specific error signature *and* the addon being absent,
    so it never fires for the `command` runner (whose projects have no addon by design)."""
    cause = error.__cause__
    if not (isinstance(cause, RuntimeError) and "wrote no report" in str(cause)):
        return None
    if (Path(project_dir) / _GDUNIT_ADDON_REL).is_dir():
        return None  # addon is present — some other GdUnit4/Godot failure, not a setup problem
    return (
        f"error: the GdUnit4 addon was not found in the project — {_GDUNIT_ADDON_REL}/ is missing "
        f"under {project_dir}.\n"
        "  Install GdUnit4 (Godot Asset Library), or run without the addon via "
        '--runner command --command "<your headless test command>".'
    )


def _report_baseline_failure(error: BaselineFailed, project_dir: str) -> int:
    """Print the most actionable message for a `BaselineFailed` and return the exit code: 2 for a
    *setup* error (missing runner binary, or a GdUnit4 project with no addon), else 1 for a
    genuinely red baseline. Shared by the single- and multi-file run paths."""
    missing = _missing_executable(error)
    if missing is not None:
        print(_missing_executable_hint(missing), file=sys.stderr)
        return 2
    addon_hint = _gdunit4_addon_hint(error, project_dir)
    if addon_hint is not None:
        print(addon_hint, file=sys.stderr)
        return 2
    print(f"error: {error}", file=sys.stderr)
    return 1


#: Directories skipped when expanding a directory target to `.gd` files: third-party addons and the
#: engine/VCS dirs. A dot-prefixed dir (``.godot``, ``.git``) is skipped too (`_gd_files_under`).
_SKIP_DIRS = frozenset({"addons"})


def _gd_files_under(directory: Path) -> list[str]:
    """Every ``.gd`` file under `directory` (recursive), sorted, skipping ``addons/`` (third-party)
    and any dot-directory (``.godot``, ``.git``, …). Point gdmutant at your *source* directory — it
    mutates every `.gd` there, including test files, so a whole-project target adds noise."""
    files: list[str] = []
    for path in directory.rglob("*.gd"):
        parents = path.relative_to(directory).parts[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in parents):
            continue
        files.append(str(path))
    return sorted(files)


def _expand_sources(paths: list[str]) -> list[str] | None:
    """Expand each given path (a ``.gd`` file or a directory) into a de-duplicated, sorted list of
    `.gd` files. Returns None (after printing an error) when nothing resolves to a `.gd` file — the
    "point it at a directory" adoption case (LOD-79). Existence/validity of each file is checked
    later by `_load_gdscript`."""
    collected: list[str] = []
    for raw in paths:
        path = Path(raw)
        collected.extend(_gd_files_under(path) if path.is_dir() else [raw])
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in collected:
        resolved = str(Path(candidate).resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    if not unique:
        print("error: no .gd files found in the given path(s)", file=sys.stderr)
        return None
    return sorted(unique)


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
    write error (LOD-110). ``None`` (no report) is always fine; only a real file path is validated,
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


def list_mutants(source_path: str) -> int:
    """Print every mutant gdmutant would generate for `source_path` **without running any tests** —
    a Godot-free way to see the tool work. Returns 0, or 2 if the source can't be read/parsed."""
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    _warn_unknown_ignore_operators(source)
    mutants = generate_mutants(str(Path(source_path)), source)
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
    # Preflight the report paths up front (LOD-110): a run boots Godot per mutant for minutes, so an
    # unwritable --json/--html target must fail now, not after the whole pass completes.
    for problem in (
        _report_path_problem(json_path, "--json", stdout_ok=True),
        _report_path_problem(html_path, "--html", stdout_ok=False),
    ):
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
    # In-place-mutation safety (LOD-88): the run edits the source file per mutant. Warn (or, with
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
    try:
        result = run(
            project_dir,
            str(path),
            source,
            runner,
            timeout=timeout,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except BaselineFailed as error:
        return _report_baseline_failure(error, project_dir)
    # With --json - the report goes to stdout, so keep the human summary on stderr — an agent
    # capturing stdout then gets pure JSON.
    report_to_stdout = json_path == "-"
    print(console_summary(result), file=sys.stderr if report_to_stdout else sys.stdout)
    stryker = stryker_report(result, str(path), source, "gdscript")
    return _write_reports(stryker, json_path, html_path)


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
) -> int:
    """Mutate several `.gd` files against one project in a single pass — the baseline runs **once**
    and the score is aggregated across every file, with one merged report (LOD-79). Same return
    codes as `run_mutation`. Every source is loaded/validated before anything runs (a bad file fails
    fast)."""
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
    try:
        runs = run_paths(
            project_dir,
            sources,
            runner,
            timeout=timeout,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except BaselineFailed as error:
        return _report_baseline_failure(error, project_dir)
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
    if settings.get("runner") not in (None, "gdunit4", "command"):
        print(f"error: {path}: 'runner' must be 'gdunit4' or 'command'", file=sys.stderr)
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
        choices=("gdunit4", "command"),
        default="gdunit4",
        help="test runner: gdunit4 (JUnit XML) or command (any harness, by exit code) "
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
        "--tests", default="res://test", help="the GdUnit4 test path (default: res://test)"
    )
    run_parser.add_argument(
        "--report-path",
        default=DEFAULT_REPORT_PATH,
        help="GdUnit4 JUnit-XML path, relative to the project dir "
        f"(default: {DEFAULT_REPORT_PATH})",
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
        help="write a ready-to-open HTML report here (the mutation-testing-elements viewer, "
        "report inlined; the viewer loads from a pinned CDN)",
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
        "--dry-run",
        action="store_true",
        help="list the mutants without running any tests (no Godot needed)",
    )
    # Config values seed the run subparser's defaults, so an explicit CLI flag still overrides them
    # (argparse precedence: passed value > set_defaults > add_argument default). LOD-107.
    if config:
        run_parser.set_defaults(**config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    config = _load_config()
    if config is None:
        return 2  # a malformed/invalid .gdmutant.toml is a setup error
    parser = build_parser(config)
    args = parser.parse_args(argv)
    if args.command == "run":
        files = _expand_sources(args.source)
        if files is None:
            return 2
        if args.dry_run:
            ignored = [
                flag
                for flag, value, default in (
                    ("--project", args.project, None),
                    ("--runner", args.runner, "gdunit4"),
                    ("--command", args.test_command, None),
                    ("--godot", args.godot, "godot"),
                    ("--tests", args.tests, "res://test"),
                    ("--report-path", args.report_path, DEFAULT_REPORT_PATH),
                    ("--timeout", args.timeout, None),
                    ("--require-clean", args.require_clean, False),
                    ("--json", args.json_path, None),
                    ("--html", args.html_path, None),
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
                rc = list_mutants(gd_file)
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
            # shlex-split first, then require a non-empty result: this rejects a missing --command
            # AND a whitespace-only one (which would otherwise become `[]` -> a confusing subprocess
            # IndexError deep in the run). Unbalanced quotes make shlex raise ValueError — surface
            # it as a clean exit-2, not a raw traceback, like every other bad-input case here.
            try:
                test_command = shlex.split(args.test_command) if args.test_command else []
            except ValueError as error:
                print(f"error: could not parse --command: {error}", file=sys.stderr)
                return 2
            if not test_command:
                print("error: --runner command requires a non-empty --command", file=sys.stderr)
                return 2
            runner = CommandRunner(command=test_command, timeout=baseline_timeout)
        else:
            if args.test_command:
                # --command only applies to --runner command; flag it rather than silently drop it.
                print("note: --command is ignored unless --runner command is set", file=sys.stderr)
            runner = GdUnit4Runner(
                test_path=args.tests,
                godot=args.godot,
                report_path=args.report_path,
                timeout=baseline_timeout,
            )
        common = {
            "timeout": args.timeout,
            "json_path": args.json_path,
            "html_path": args.html_path,
            "require_clean": args.require_clean,
        }
        if len(files) == 1:
            return run_mutation(files[0], project_dir, runner, **common)
        return run_mutation_paths(files, project_dir, runner, **common)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
