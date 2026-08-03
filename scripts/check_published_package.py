#!/usr/bin/env python3
"""Prove the package that reached the index actually installs and runs.

WHAT THIS IS, AND WHAT IT IS NOT. This runs *after* the upload, so it cannot stop a bad release --
by the time it can answer, the version number is spent. It is not a gate in the sense
``publish.yml``'s other guards are. What it converts is the maintainer's post-release habit: instead
of someone remembering to install the thing and try it, the release run goes red on its own if the
published artifact is broken. The difference between a checklist item and a red X is whether it
happens on the day everyone is tired.

WHAT IT ACTUALLY PROVES. ``gdmutant --version`` alone proves an entry point exists. That is a thin
claim. So this goes one step further and mutates a two-line scratch file with ``--dry-run``, which
needs no Godot and no network: reaching a printed mutant list means the console script resolved,
the declared dependencies (``gdtoolkit``, ``lark``) installed and imported, and the GDScript parser
loaded and parsed real source. Those are the failures a wheel actually ships -- a missing
dependency, a package that did not get included, an entry point pointing at a module that is not
there -- and a version string catches none of them.

It also runs ``gdmutant example`` and checks the file it claims to write actually landed. That is a
narrower, separate claim from the one above: a wheel's Python modules and its non-Python package
data (``gdmutant/examples/gdmutant-hello-world.gd``) are included by different packaging rules, so
the smoke test parsing real GDScript has never been proof the bundled example file shipped too.

ISOLATION. Everything happens in a throwaway virtual environment inside a temporary directory, and
every subprocess runs with its working directory set there. That matters: if the checked-out source
tree were the working directory, ``import gdmutant`` would find the repo's own package and the test
would pass without the published artifact contributing anything. The check does not take that on
trust -- it asks the installed interpreter where ``gdmutant`` came from and fails if the answer is
not inside the virtual environment.

NOTHING HERE WRITES ANYWHERE. It installs from an index and runs a CLI against a scratch file it
created. No upload, no tag, no credential, no repository state.

Usage::

    python3 scripts/check_published_package.py --version v0.1.0

Exit codes: 0 the published package installs and runs; 1 it is broken; 2 usage; 3 the index never
served the version within the wait budget (propagation, not a broken package).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Exit codes, named so the workflow log and the tests agree on what each one means.
OK = 0
BROKEN = 1
USAGE = 2
NOT_PROPAGATED = 3

DEFAULT_INDEX = "https://pypi.org/simple"

#: A minimal GDScript file with one comparison and one number in it, so the adapter has something
#: to find. Tabs, because GDScript wants tabs.
SMOKE_SOURCE = "extends Node\n\nfunc is_ready(hp: int) -> bool:\n\treturn hp > 10\n"

#: `gdmutant/cli.py`'s `_EXAMPLE_NAME`, restated rather than imported: this script never imports
#: `gdmutant` (see ISOLATION above -- it only ever runs the *installed console script*, so the
#: package under test is never the repo's own checkout). Keep the two in sync by hand; a mismatch
#: here would make `example_problem` look for a file the real command never writes.
EXAMPLE_NAME = "gdmutant-hello-world.gd"

#: What pip says when the index has heard of the project but not (yet) this version, or not at all.
#: A fresh upload is briefly invisible while the index's caches catch up, and reporting that as
#: "the package is broken" would send whoever reads the log to look for a defect that is not there.
_PROPAGATION_PHRASES = (
    "no matching distribution found",
    "could not find a version that satisfies",
)


@dataclass(frozen=True)
class Result:
    """One command's outcome: its exit code and everything it printed, streams combined."""

    returncode: int
    output: str


#: A command runner: argv plus a working directory, in and a Result out. Injected so every rule
#: below is unit-testable with no network and no real installs.
Runner = Callable[[Sequence[str], Path], Result]


def run_command(argv: Sequence[str], cwd: Path) -> Result:
    """The real runner. Streams are merged and decoded leniently.

    ``errors="replace"`` rather than a hard decode: gdmutant already shipped a Windows bug where
    console output crashed under the legacy cp1252 code page, and a check that dies reading a
    stray glyph reports nothing about the package it was asked to check.
    """
    completed = subprocess.run(  # noqa: S603 - argv is built here, never from user input
        list(argv),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return Result(completed.returncode, completed.stdout or "")


def normalize_version(raw: str) -> str:
    """``v0.1.0`` or ``0.1.0`` -> ``0.1.0``.

    The workflow has the release *tag* to hand, and ``scripts/check_release_tag.py`` has already
    forced that tag to equal the packaged version, so accepting either spelling here means the
    workflow can pass what it has instead of reformatting it in YAML.
    """
    return raw[1:] if raw.startswith("v") else raw


def venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    """The interpreter and the console script inside a virtual environment, per platform.

    Built from ``Path`` parts, never a joined string with a hardcoded separator -- Windows is a
    real deployment target here, not only a dev machine.
    """
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "gdmutant.exe"
    return venv_dir / "bin" / "python", venv_dir / "bin" / "gdmutant"


def looks_like_propagation_lag(output: str) -> bool:
    """Whether pip's complaint is "the index does not have it yet" rather than "it is broken"."""
    lowered = output.lower()
    return any(phrase in lowered for phrase in _PROPAGATION_PHRASES)


def install_from_index(
    python: Path,
    spec: str,
    index_url: str,
    cwd: Path,
    run: Runner,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None],
) -> tuple[Result, bool]:
    """Install `spec`, retrying only while the index simply has not caught up.

    Returns the last result and whether every failure was propagation lag. Any other failure stops
    immediately: repeating a real dependency-resolution error for ten minutes teaches nobody
    anything and buries the message that matters.
    """
    result = Result(1, "not attempted")
    lagging = False
    for attempt in range(1, attempts + 1):
        result = run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--index-url",
                index_url,
                spec,
            ],
            cwd,
        )
        if result.returncode == 0:
            return result, False
        lagging = looks_like_propagation_lag(result.output)
        if not lagging:
            return result, False
        if attempt < attempts:
            print(
                f"  {spec} not on the index yet (attempt {attempt}/{attempts}); "
                f"waiting {delay:.0f}s"
            )
            sleep(delay)
    return result, lagging


def version_problem(result: Result, version: str) -> str | None:
    """An error message if ``gdmutant --version`` did not report `version`, else None."""
    if result.returncode != 0:
        return f"`gdmutant --version` exited {result.returncode}:\n{result.output}"
    reported = result.output.strip()
    if reported != f"gdmutant {version}":
        return f"expected 'gdmutant {version}', got {reported!r}"
    return None


def location_problem(result: Result, venv_dir: Path) -> str | None:
    """An error message unless the imported package lives inside the virtual environment.

    This is the anti-shadowing assertion. Without it the whole check could be satisfied by the
    repository checkout sitting on ``sys.path``, and it would keep passing even if the upload had
    shipped an empty wheel.
    """
    if result.returncode != 0:
        return f"could not locate the installed package:\n{result.output}"
    located = Path(result.output.strip())
    if not located.resolve().is_relative_to(venv_dir.resolve()):
        return (
            f"gdmutant was imported from {located}, which is outside {venv_dir}. Something on the "
            "path shadowed the installed package, so this run proved nothing about what was "
            "published."
        )
    return None


def smoke_problem(result: Result) -> str | None:
    """An error message unless the dry-run listed at least one mutant, else None.

    The count is read as a number, not matched as a substring. ``gdmutant run --dry-run`` prints
    ``0 mutants for smoke.gd:`` for an empty list and still exits 0, so "the line appeared" and
    "the adapter found something" are two different questions, and only the second one shows that
    the GDScript parser reached real source. Matching on ``"0 mutants for "`` instead would answer
    neither: it is a substring of ``"10 mutants for smoke.gd:"``, so every count that ends in a
    zero would read as a failure.

    A first word that is not a number means the output no longer has the shape read here. That
    fails loudly rather than raising, because a check whose job is to describe a broken release
    should not itself end in a traceback.
    """
    if result.returncode != 0:
        return f"`gdmutant run smoke.gd --dry-run` exited {result.returncode}:\n{result.output}"
    if " mutants for " not in result.output:
        return f"the dry run listed no mutants:\n{result.output}"
    counted = result.output.split(maxsplit=1)[0]
    try:
        count = int(counted)
    except ValueError:
        return (
            f"the dry run did not open with a mutant count, but with {counted!r}:\n{result.output}"
        )
    if count == 0:
        return f"the dry run listed no mutants:\n{result.output}"
    return None


def example_problem(result: Result, written: Path) -> str | None:
    """An error message unless ``gdmutant example`` exited 0 and `written` now exists, else None.

    `smoke_problem` proves the console script, its dependencies and the parser all arrived --
    everything a wheel needs to run at all. This proves something narrower and separate: that
    package *data* shipped too. `gdmutant/examples/gdmutant-hello-world.gd` ships alongside the
    Python source under the same `packages = ["gdmutant"]` wheel target, but a data file and a
    module are included by different rules, so one arriving has never been proof the other did.
    """
    if result.returncode != 0:
        return f"`gdmutant example` exited {result.returncode}:\n{result.output}"
    if not written.is_file():
        return f"`gdmutant example` exited 0 but did not write {written.name}:\n{result.output}"
    return None


def check(
    version: str,
    workdir: Path,
    index_url: str,
    run: Runner,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None],
) -> tuple[int, str]:
    """Install the published version into `workdir` and exercise it.

    Returns (exit code, message).
    """
    venv_dir = workdir / "venv"
    created = run([sys.executable, "-m", "venv", str(venv_dir)], workdir)
    if created.returncode != 0:
        return BROKEN, f"could not create a virtual environment:\n{created.output}"
    python, console_script = venv_paths(venv_dir)

    spec = f"gdmutant=={version}"
    print(f"installing {spec} from {index_url} into a clean environment")
    installed, lagging = install_from_index(
        python, spec, index_url, workdir, run, attempts, delay, sleep
    )
    if installed.returncode != 0 and lagging:
        return NOT_PROPAGATED, (
            f"the index has not served {spec} after {attempts} attempts. This is index "
            "propagation, not a broken package - the upload itself succeeded. Re-run this job in a "
            f"few minutes.\n{installed.output}"
        )
    if installed.returncode != 0:
        return BROKEN, f"installing {spec} from the index failed:\n{installed.output}"

    problem = version_problem(run([str(console_script), "--version"], workdir), version)
    if problem is not None:
        return BROKEN, problem
    print(f"  gdmutant {version} reports its own version correctly")

    located = run(
        [str(python), "-c", "import gdmutant, sys; sys.stdout.write(gdmutant.__file__)"], workdir
    )
    problem = location_problem(located, venv_dir)
    if problem is not None:
        return BROKEN, problem
    print("  the running gdmutant is the installed one, not a copy on the path")

    (workdir / "smoke.gd").write_text(SMOKE_SOURCE, encoding="utf-8")
    problem = smoke_problem(run([str(console_script), "run", "smoke.gd", "--dry-run"], workdir))
    if problem is not None:
        return BROKEN, problem
    print(
        "  it parsed real GDScript and listed mutants - entry point, dependencies and parser "
        "all arrived"
    )

    problem = example_problem(
        run([str(console_script), "example"], workdir), workdir / EXAMPLE_NAME
    )
    if problem is not None:
        return BROKEN, problem
    print(f"  `gdmutant example` wrote {EXAMPLE_NAME} - the packaged example data arrived too")

    return OK, f"the published gdmutant {version} installs from {index_url} and runs"


def main(
    argv: list[str],
    run: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        prog="check_published_package.py", description=__doc__, allow_abbrev=False
    )
    parser.add_argument("--version", required=True, help="the released version or tag (vX.Y.Z)")
    parser.add_argument(
        "--index-url",
        default=DEFAULT_INDEX,
        help=f"index to install from (default: {DEFAULT_INDEX})",
    )
    parser.add_argument(
        "--attempts", type=int, default=10, help="install attempts while the index catches up"
    )
    parser.add_argument("--delay", type=float, default=30.0, help="seconds between attempts")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where to build the throwaway environment (default: a temporary directory, removed "
        "afterwards). Must not be inside a checkout of gdmutant.",
    )
    args = parser.parse_args(argv[1:])

    version = normalize_version(args.version)
    workdir = args.workdir
    temporary = workdir is None
    if temporary:
        workdir = Path(tempfile.mkdtemp(prefix="gdmutant-published-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        code, message = check(
            version, workdir, args.index_url, run, args.attempts, args.delay, sleep
        )
    finally:
        if temporary:
            shutil.rmtree(workdir, ignore_errors=True)

    if code == OK:
        print(message)
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
