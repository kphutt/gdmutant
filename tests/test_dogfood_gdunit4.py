"""Repeatable dogfood harness — run gdmutant against a real, large GDScript codebase (GdUnit4).

gdmutant's own corpus is tiny and hand-built. Real adoption bugs only show up on a real tree: the
GdUnit4 dogfood ([ticket]) immediately surfaced [ticket] — a whole-directory run aborting at exit 2 on
the *one* file gdtoolkit couldn't parse. This harness makes that dogfood **repeatable** so a
regression can't creep back, and is wired to run the same way locally and (later) in CI.

It is **env-gated** on ``GDMUTANT_GDUNIT4_CLONE`` (the path to a GdUnit4 checkout), so a plain
``uv run pytest`` — local dev and the ``verify`` CI job — auto-skips it with zero config. Run it
with, e.g.::

    git clone https://github.com/MikeSchulze/gdUnit4 [dev-path]
    GDMUTANT_GDUNIT4_CLONE=[dev-path] uv run pytest tests/test_dogfood_gdunit4.py

Both checks are **Godot-free** (gdtoolkit parse + the ``--dry-run`` mutant-generation path), so they
are fast (~4s) and cheap enough to gate. The *real*-Godot run of GdUnit4's own suite for survivor
evidence is a separate, much slower step, deliberately not a per-run check.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import is_valid_gdscript

_CLONE = os.environ.get("GDMUTANT_GDUNIT4_CLONE")

pytestmark = pytest.mark.skipif(
    not _CLONE, reason="set GDMUTANT_GDUNIT4_CLONE=<gdUnit4 checkout> to run the dogfood harness"
)


def _src_dir() -> Path:
    """The GdUnit4 addon source tree — the mutation target (its ``test/`` dirs are auto-skipped)."""
    assert _CLONE is not None  # pytestmark guarantees this; narrows the type for mypy
    src = Path(_CLONE).expanduser() / "addons" / "gdUnit4" / "src"
    if not src.is_dir():
        pytest.skip(f"no addons/gdUnit4/src under {_CLONE} — is this a GdUnit4 checkout?")
    return src


# Observed 2026-07-19 on MikeSchulze/gdUnit4: 232/233 .gd parse (99.57%); one file
# (doubler/GdUnitClassDoubler.gd) hits a real gdtoolkit grammar gap. The floor is set well below
# that so gdtoolkit *improving*, or one more odd file appearing, won't flake — but a real parser
# regression that silently drops a swathe of files trips it.
_MIN_PARSE_RATE = 0.99
_MIN_FILE_COUNT = 200  # sanity: we're actually pointed at the full addon, not an empty/partial tree


def test_gdunit4_src_parse_coverage_stays_high() -> None:
    """The gdtoolkit oracle must keep parsing the overwhelming majority of a real codebase. This is
    the canary for a parser regression (a gdtoolkit bump, or a construct that newly fails) that
    would otherwise quietly shrink every directory run's mutation set."""
    files = sorted(_src_dir().rglob("*.gd"))
    assert len(files) >= _MIN_FILE_COUNT, (
        f"expected the full addon tree, found only {len(files)} .gd"
    )
    unparseable = [f for f in files if not is_valid_gdscript(f.read_text(encoding="utf-8"))]
    rate = 1 - len(unparseable) / len(files)
    assert rate >= _MIN_PARSE_RATE, (
        f"parse coverage dropped to {rate:.4f} ({len(unparseable)}/{len(files)} unparseable) — "
        f"below the {_MIN_PARSE_RATE:.2f} floor; likely a gdtoolkit regression. Unparseable:\n  "
        + "\n  ".join(str(f) for f in unparseable)
    )


def test_gdunit4_whole_src_dry_run_completes_over_the_unparseable_file() -> None:
    """The [ticket] regression guard, against the *real* tree that surfaced it: a whole-directory
    ``--dry-run`` must complete (exit 0) and skip the odd file with a note — not abort at exit 2 on
    the first thing gdtoolkit can't parse. Godot-free, so it stays a fast per-run check."""
    src = _src_dir()
    completed = subprocess.run(
        [sys.executable, "-m", "gdmutant.cli", "run", str(src), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    detail = (
        f"\n--- stderr ---\n{completed.stderr[-800:]}"
        f"\n--- stdout tail ---\n{completed.stdout[-400:]}"
    )
    assert completed.returncode == 0, f"whole-dir dry-run aborted ([ticket] regression){detail}"
    # The one known-unparseable file must be reported as skipped, not silently missing or fatal.
    assert "gdtoolkit couldn't parse" in completed.stderr, (
        f"expected a skip note for the odd file{detail}"
    )
    # And the run must have actually produced mutants — not a no-op exit 0. Every mutated file
    # prints a "<N> mutants for <path>:" header, so sum the real counts. A bare "mutants for"
    # substring check would be a no-op: list_mutants prints that header even at N=0, so it couldn't
    # catch a regression that silently zeroes generation across the tree — the very failure class
    # this guards ([internal-tool] P2). Observed 2026-07-19: 10,440 mutants over 227 files; the floor leaves
    # wide headroom (new operators only ever add) while still catching a near-total collapse.
    total_mutants = sum(
        int(line.split(" ", 1)[0])
        for line in completed.stdout.splitlines()
        if " mutants for " in line
    )
    assert total_mutants > 1000, (
        f"whole-src dry-run generated only {total_mutants} mutants — near-total collapse{detail}"
    )
