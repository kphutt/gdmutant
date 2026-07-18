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
from gdmutant.engine.loop import BaselineFailed, run
from gdmutant.engine.report import console_summary, stryker_report
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


def _report_path_problem(json_path: str | None) -> str | None:
    """A human message if the ``--json`` report path can't be written, else None — checked *before*
    the run so a long pass (minutes of booting Godot per mutant) never ends on an avoidable write
    error (LOD-110). ``-`` (stdout) and ``None`` (no report) are always fine; only a real file path
    is validated, and only its parent directory (the file itself needn't exist yet)."""
    if json_path is None or json_path == "-":
        return None
    parent = Path(json_path).parent
    if not parent.exists():
        return f"--json directory does not exist: {parent}"
    if not os.access(parent, os.W_OK):
        return f"--json directory is not writable: {parent}"
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
    require_clean: bool = False,
) -> int:
    """Mutate `source_path`, run the suite via `runner`, print the summary, optionally write JSON.

    Returns 0 on a completed pass — **survivors are report output, not a failure** (FG-6.2) — 1 if
    the unmutated baseline suite fails, and 2 if the source can't be read, isn't valid GDScript, the
    project directory doesn't exist, `require_clean` is set and the source has uncommitted changes,
    the test-runner executable isn't found, or the JSON report can't be written.
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
    # Preflight the report path up front (LOD-110): a run boots Godot per mutant for minutes, so an
    # unwritable --json target must fail now, not after the whole pass completes.
    report_problem = _report_path_problem(json_path)
    if report_problem is not None:
        print(f"error: {report_problem}", file=sys.stderr)
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
    # With --json - the report goes to stdout, so keep the human summary on stderr — an agent
    # capturing stdout then gets pure JSON.
    report_to_stdout = json_path == "-"
    print(console_summary(result), file=sys.stderr if report_to_stdout else sys.stdout)
    if json_path is not None:
        report = json.dumps(stryker_report(result, str(path), source, "gdscript"), indent=2)
        if report_to_stdout:
            print(report)
        else:
            try:
                Path(json_path).write_text(report, encoding="utf-8")
            except OSError as error:
                print(f"error: cannot write report to {json_path}: {error}", file=sys.stderr)
                return 2
            print(f"\nWrote report to {json_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdmutant",
        description="Mutation testing for GDScript (and, in time, other languages).",
    )
    parser.add_argument("--version", action="version", version=f"gdmutant {__version__}")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="mutate a GDScript file and report survivors")
    run_parser.add_argument("source", help="the .gd file to mutate")
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
        "--require-clean",
        action="store_true",
        help="refuse to run if the source file has uncommitted git changes (default: warn only)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the mutants without running any tests (no Godot needed)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
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
                )
                if value != default
            ]
            if ignored:
                print(
                    f"note: --dry-run runs no tests, so {', '.join(ignored)} "
                    f"{'is' if len(ignored) == 1 else 'are'} ignored",
                    file=sys.stderr,
                )
            return list_mutants(args.source)
        project_dir = args.project or str(Path(args.source).resolve().parent)
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
        return run_mutation(
            args.source,
            project_dir,
            runner,
            timeout=args.timeout,
            json_path=args.json_path,
            require_clean=args.require_clean,
        )
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
