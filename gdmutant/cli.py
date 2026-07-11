"""Standalone CLI: ``gdmutant run <file.gd>`` mutates a GDScript file and reports survivors.

No AI required — a normal command-line tool (the #1 design goal). `run_mutation` is the testable
core (inject any `Runner`); `main` wires the real `GdUnit4Runner`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from gdmutant import __version__
from gdmutant.adapters.gdscript import generate_mutants, is_valid_gdscript
from gdmutant.adapters.gdscript.runner import (
    DEFAULT_REPORT_PATH,
    DEFAULT_TIMEOUT,
    GdUnit4Runner,
)
from gdmutant.engine.loop import BaselineFailed, run
from gdmutant.engine.report import console_summary, stryker_report
from gdmutant.engine.runner import Runner

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
    users are on macOS where Godot is never on PATH ([ticket])."""
    lines = [
        f"error: could not run the test suite — executable {filename!r} not found.",
        "  Install it and put it on your PATH, or pass its full path with --godot.",
    ]
    if sys.platform == "darwin" and "godot" in filename.lower():
        lines.append(_MISSING_GODOT_MACOS_HINT)
    return "\n".join(lines)


def list_mutants(source_path: str) -> int:
    """Print every mutant gdmutant would generate for `source_path` **without running any tests** —
    a Godot-free way to see the tool work. Returns 0, or 2 if the source can't be read/parsed."""
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    mutants = generate_mutants(str(Path(source_path)), source)
    print(f"{len(mutants)} mutants for {source_path}:")
    for m in mutants:
        loc = f"{m.path}:{m.span.line}:{m.span.column}"
        print(f"  {loc}  {m.operator_id}  {m.original} -> {m.replacement}")
    return 0


def run_mutation(
    source_path: str, project_dir: str, runner: Runner, *, json_path: str | None = None
) -> int:
    """Mutate `source_path`, run the suite via `runner`, print the summary, optionally write JSON.

    Returns 0 on a completed pass — **survivors are report output, not a failure** (FG-6.2) — 1 if
    the unmutated baseline suite fails, and 2 if the source can't be read, isn't valid GDScript, the
    project directory doesn't exist, the test-runner executable isn't found, or the JSON report
    can't be written.
    """
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    # Validate project_dir up front: the runner shells out with `cwd=project_dir`, so a missing dir
    # raises FileNotFoundError too — indistinguishable by type from a missing `godot`. Catching it
    # here keeps _missing_executable's FileNotFoundError unambiguously about the executable, and
    # gives a bad --project its own clear message.
    if not Path(project_dir).is_dir():
        print(f"error: project directory not found: {project_dir}", file=sys.stderr)
        return 2
    path = Path(source_path)
    # Progress goes to stderr unconditionally: a real run boots Godot per mutant, so without it the
    # tool looks hung. stderr keeps stdout clean for --json - (pure JSON) and the human summary.
    try:
        result = run(
            project_dir,
            str(path),
            source,
            runner,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except BaselineFailed as error:
        missing = _missing_executable(error)
        if missing is not None:
            print(_missing_executable_hint(missing), file=sys.stderr)
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
        default=DEFAULT_TIMEOUT,
        help=f"per-mutant test-run timeout, in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    run_parser.add_argument(
        "--json", dest="json_path", help="write the Stryker JSON report here (use - for stdout)"
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
                    ("--godot", args.godot, "godot"),
                    ("--tests", args.tests, "res://test"),
                    ("--report-path", args.report_path, DEFAULT_REPORT_PATH),
                    ("--timeout", args.timeout, DEFAULT_TIMEOUT),
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
        runner = GdUnit4Runner(
            test_path=args.tests,
            godot=args.godot,
            report_path=args.report_path,
            timeout=args.timeout,
        )
        return run_mutation(args.source, project_dir, runner, json_path=args.json_path)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
