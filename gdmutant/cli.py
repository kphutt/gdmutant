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
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.loop import BaselineFailed, run
from gdmutant.engine.report import console_summary, stryker_report
from gdmutant.engine.runner import Runner


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
    the unmutated baseline suite fails, and 2 if the source can't be read, isn't valid GDScript, or
    the JSON report can't be written.
    """
    source = _load_gdscript(source_path)
    if source is None:
        return 2
    path = Path(source_path)
    try:
        result = run(project_dir, str(path), source, runner)
    except BaselineFailed as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(console_summary(result))
    if json_path is not None:
        try:
            Path(json_path).write_text(
                json.dumps(stryker_report(result, str(path), source), indent=2), encoding="utf-8"
            )
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
    run_parser.add_argument("--json", dest="json_path", help="write the Stryker JSON report here")
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
        runner = GdUnit4Runner(test_path=args.tests, godot=args.godot)
        return run_mutation(args.source, project_dir, runner, json_path=args.json_path)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
