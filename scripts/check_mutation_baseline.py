#!/usr/bin/env python3
"""Manual-stage hook: run poodle (report mode, advisory) against gdmutant/**.py files changed vs
origin/main. See docs/decisions/0013-windows-local-mutation-testing.md for why this is poodle
and not mutmut (native Windows support) and why it's diff-scoped, not a full-package sweep (a
full sweep is well over an hour; this is a pre-push hook).

A surviving mutant is fine (this is advisory) -- but a baseline that won't run clean unmutated
means the score would be meaningless, so that case fails this hook instead of silently reporting
a green run.

Mutant cap. A diff-scoped run still assumes a "typical" change: a few dozen lines of real logic.
A file that is mostly string literals (long argparse help text, message prose) breaks that
assumption, because poodle's StringMutator turns every literal into its own mutant. PR #186's diff
included gdmutant/engine/survivor_reference.py, almost entirely string literals, and the resulting
run took about fifty minutes and produced no output before the environment killed it.

So before running poodle for real, this script asks poodle itself how many mutants each changed
file would produce -- mutant *generation* is cheap AST parsing, it is the per-mutant *test run*
that is slow -- and if the total is over the cap, it runs the smallest files first and drops
whatever does not fit. Every dropped file is named and its mutant count printed. Silent truncation
would read as full coverage, which is the failure mode this exists to avoid.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

#: Measured directly on a Windows dev machine (this hook's target persona -- see poodle.toml):
#: 82 mutants in gdmutant/engine/spans.py took about 497 seconds end to end, roughly 6 seconds per
#: mutant. That is slower than poodle.toml's older ~2.3s/mutant estimate, which was not measured on
#: Windows -- process-spawn overhead per mutant is well known to be higher here (it is the same
#: reason mutmut has no native Windows support at all, see docs/decisions/0013). 50 mutants is
#: therefore about 5 minutes worst case at the measured rate: inside "single-digit minutes" with
#: real margin, while still covering a normal small change in full.
DEFAULT_MAX_MUTANTS = 50

#: Overrides DEFAULT_MAX_MUTANTS. `--max-mutants` on the command line overrides this in turn.
MAX_MUTANTS_ENV_VAR = "GDMUTANT_MUTATION_BASELINE_MAX_MUTANTS"

# This runs inside `uv run python -c ...`, not in this process. check_mutation_baseline.py itself
# may run under pre-commit's own minimal hook environment (see the gdmutant-mutation hook in
# .pre-commit-config.yaml), which does not have poodle installed. The real poodle invocation below
# already shells out through `uv run` for the same reason. This does the same for the much cheaper
# counting pass, reusing poodle's own mutant-generation code so the count can't drift from what a
# real run would produce.
_COUNT_MUTANTS_SCRIPT = """
import json
import sys
from pathlib import Path

from poodle.config import build_config
from poodle.data_types import PoodleWork
from poodle.mutate import create_mutants_for_file, get_target_files, initialize_mutators

config_file, files = json.loads(sys.stdin.read())
config = build_config(
    cmd_sources=(),
    cmd_config_file=Path(config_file),
    cmd_quiet=3,
    cmd_verbose=0,
    cmd_max_workers=None,
    cmd_excludes=(),
    cmd_only_files=tuple(files),
    cmd_report=(),
    cmd_html=None,
    cmd_json=None,
    cmd_fail_under=None,
)
work = PoodleWork(config)
work.mutators = initialize_mutators(work)

counts = {}
for folder, folder_files in get_target_files(work).items():
    for file in folder_files:
        key = file.as_posix()
        counts[key] = counts.get(key, 0) + len(create_mutants_for_file(work, folder, file))
print(json.dumps(counts))
"""


def changed_gdmutant_files(base_ref: str = "origin/main") -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD", "--", "gdmutant"],
        capture_output=True,
        text=True,
        check=True,
    )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [f for f in files if f.endswith(".py") and Path(f).is_file()]


def resolve_max_mutants(cmd_line_value: int | None) -> int:
    """--max-mutants overrides GDMUTANT_MUTATION_BASELINE_MAX_MUTANTS, which overrides
    DEFAULT_MAX_MUTANTS."""
    if cmd_line_value is not None:
        return cmd_line_value
    raw = os.environ.get(MAX_MUTANTS_ENV_VAR)
    if raw is None:
        return DEFAULT_MAX_MUTANTS
    try:
        return int(raw)
    except ValueError:
        print(
            f"check_mutation_baseline: ignoring invalid {MAX_MUTANTS_ENV_VAR}={raw!r} "
            f"(not an int) -- using default {DEFAULT_MAX_MUTANTS}"
        )
        return DEFAULT_MAX_MUTANTS


def count_mutants_per_file(files: list[str], config_path: str = "poodle.toml") -> dict[str, int]:
    """Ask poodle how many mutants it would generate for each file, without running a single
    test. Returns a mapping from posix-style repo-relative path (matching what
    changed_gdmutant_files returns, since git always uses "/") to mutant count."""
    result = subprocess.run(
        ["uv", "run", "python", "-c", _COUNT_MUTANTS_SCRIPT],
        input=json.dumps([config_path, files]),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def select_files_within_cap(
    files: list[str], counts: dict[str, int], max_mutants: int
) -> tuple[list[str], list[tuple[str, int]]]:
    """Choose which changed files fit inside the mutant cap.

    Smallest files first, so the cap covers as many files as possible instead of being spent
    entirely on whichever file the diff happened to list first. A file that alone holds more
    mutants than the whole cap is skipped outright -- this never runs a file partially.

    Returns (included, skipped), where skipped is [(file, mutant_count), ...] in the order the
    files were dropped (largest last, since the loop hands back leftovers in ascending order).
    """
    ordered = sorted(files, key=lambda f: counts.get(f, 0))
    included: list[str] = []
    skipped: list[tuple[str, int]] = []
    total = 0
    for f in ordered:
        n = counts.get(f, 0)
        if total + n <= max_mutants:
            included.append(f)
            total += n
        else:
            skipped.append((f, n))
    return included, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-mutants",
        type=int,
        default=None,
        help=(
            f"Cap on total mutants run before files get dropped (default {DEFAULT_MAX_MUTANTS}, "
            f"or {MAX_MUTANTS_ENV_VAR} if that env var is set)."
        ),
    )
    args = parser.parse_args(argv)
    max_mutants = resolve_max_mutants(args.max_mutants)

    files = changed_gdmutant_files()
    if not files:
        print("no gdmutant/ files changed vs origin/main -- nothing to mutate")
        return 0

    print(f"check_mutation_baseline: mutating {len(files)} changed file(s): {', '.join(files)}")

    try:
        counts = count_mutants_per_file(files)
    except subprocess.CalledProcessError as exc:
        print(
            "check_mutation_baseline: could not count mutants per file -- aborting instead of "
            "risking an uncapped run:"
        )
        print(exc.stdout)
        print(exc.stderr)
        return 1

    total = sum(counts.get(f, 0) for f in files)
    print(
        f"check_mutation_baseline: {total} mutant(s) across {len(files)} file(s), "
        f"cap is {max_mutants}"
    )

    if total == 0:
        # Files changed, but poodle's own mutant generation found nothing to mutate in any of
        # them (e.g. the diff only touched blank lines, comments, or constructs no mutator
        # covers). Running poodle here would report "0 survivors out of 0 mutants" -- a passing
        # score that measured nothing, indistinguishable from a real clean run. That is exactly
        # the "gate that passes without checking anything" shape AGENTS.md calls out, so this
        # fails loud instead of falling through to the poodle run below.
        print(
            "check_mutation_baseline: 0 mutants generated for the changed file(s) -- nothing was "
            "actually checked, not a clean run. Failing instead of reporting a false pass."
        )
        return 1

    if total > max_mutants:
        files, skipped = select_files_within_cap(files, counts, max_mutants)
        skipped_total = sum(n for _, n in skipped)
        print(
            f"check_mutation_baseline: over the cap ({total} > {max_mutants}) -- skipping "
            f"{len(skipped)} file(s), {skipped_total} mutant(s), to stay inside single-digit "
            "minutes. Raise --max-mutants (or "
            f"{MAX_MUTANTS_ENV_VAR}) to cover them too:"
        )
        for f, n in skipped:
            print(f"  SKIPPED {f} ({n} mutants)")
        if not files:
            # Same shape as the `total == 0` guard above: every changed file individually exceeds
            # the cap, poodle never runs, and nothing was actually measured. Returning 0 here would
            # be the same false "clean pass" this PR's other fix exists to close, just reached from
            # the sibling branch instead.
            print(
                "check_mutation_baseline: every changed file alone exceeds the cap -- nothing "
                "was actually checked, not a clean run. Raise --max-mutants (or "
                f"{MAX_MUTANTS_ENV_VAR}) to cover at least one, or split the diff."
            )
            return 1
        print(f"check_mutation_baseline: running poodle on the remaining {len(files)} file(s)")

    only_args: list[str] = []
    for f in files:
        only_args += ["--only", f]

    result = subprocess.run(
        ["uv", "run", "poodle", "-c", "poodle.toml", *only_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        # poodle's own fail_under is unset, so a clean run with surviving mutants still exits 0 --
        # any nonzero here means it didn't produce a meaningful score at all (clean-run failure, no
        # mutants found, a usage error, ...), not just "some mutants survived."
        print(
            f"check_mutation_baseline: poodle exited {result.returncode} -- did not produce a "
            "meaningful score (see output above)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
