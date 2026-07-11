"""Standalone CLI entry point.

Design goal: a developer runs this exactly like Stryker, no AI required. The
engine loop (mutate -> run -> tally -> report) lands in the v0.1 engine milestone,
behind the approved DESIGN.md; for now this only wires the entry point + --version so
the console script resolves and CI has a real surface to exercise.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from gdmutant import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdmutant",
        description="Mutation testing for GDScript (and, in time, other languages).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gdmutant {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # No subcommands yet — the engine loop lands in the v0.1 engine milestone (see ROADMAP.md).
    parser.print_help()
    print("\ngdmutant is scaffolding — the mutate/run/report engine is not built yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
