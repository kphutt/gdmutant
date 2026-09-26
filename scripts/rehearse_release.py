#!/usr/bin/env python3
"""Walk the release path end to end on a throwaway copy, so a release-only bug surfaces the day it
lands instead of the day a release is cut.

WHY THIS EXISTS. The tag-shaped guards in docs/releasing.md run on a path nobody walks between
releases: the version bump, the changelog rename, the tagged commit, the built long description,
the post-release pin bump. Bugs there sat quiet until release day, over and over. The release gate
could never pass `tests/test_action_pin.py` on a tagged commit (#286). The runbook drifted from the
tree (#305). A pin bump was a hand edit (#290). This script does, on a copy, what the maintainer
does at release time, and runs every check that path runs.

WHAT IT DOES, IN ORDER. Each step is one step of docs/releasing.md, done on a throwaway clone whose
`origin` is a throwaway bare repo, so nothing here can reach GitHub or PyPI:

  1. Pick the version to rehearse: pyproject.toml's version if it is not tagged yet (a release in
     progress), otherwise the next patch version.
  2. Runbook steps 1-2: set the version, re-lock, bump every `# vX.Y.Z` pin comment, date the
     changelog. Each edit must find exactly what the runbook says it will find, so a runbook that
     has drifted from the tree fails here.
  3. The version-bump commit, before the tag exists: the full suite, as Verify runs it on the
     release PR.
  4. Runbook step 4: tag it. Then, on the tagged commit, what publish.yml's gate runs: the tag
     guard (scripts/check_release_tag.py), the full suite, `uv build`, `twine check`, and every
     image in the built long description (scripts/check_readme_images.py).
  5. Runbook step 10: scripts/bump_action_pins.py, committed, then tests/test_action_pin.py must
     pass with no NOTCHECKED warning, because by then every pin can be checked.

WHAT IT CANNOT REACH, NAMED SO NOBODY READS A GREEN RUN AS MORE THAN IT IS. The ancestry guard's
authenticated fetch, the OIDC upload, the Windows Verify leg and the Godot self-tests (ci.yml runs
those on every pull request), and verify-published, which needs a real upload.

The banner URL in the built long description names the rehearsed tag, which does not exist on
GitHub. The image check fetches the same path at the commit being rehearsed instead, which must
already be on origin/main. When it is not (a local run on an unpushed branch), that step reports
NOTRUN rather than guessing.

Exit codes: 0 every step passed; 1 a step failed; 2 a step could not run, or the rehearsal could
not start. A step that did not run is never reported as a pass.

Usage, from the repo root::

    uv run python scripts/rehearse_release.py            # full suite at both commits
    uv run python scripts/rehearse_release.py --quick    # release-shaped tests only, no coverage
    uv run python scripts/rehearse_release.py --offline  # skip the image fetch; exits 2, not 0
    uv run python scripts/rehearse_release.py --keep     # leave the throwaway copy for inspection
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent

OK = 0
FAILED = 1
NOT_RUN = 2

#: The tests that exercise the release path directly. `--quick` runs only these, without the
#: coverage floor, which a partial run cannot meet.
QUICK_TESTS = (
    "tests/test_action_pin.py",
    "tests/test_check_release_tag.py",
    "tests/test_packaging.py",
)

#: pytest, quiet, with a line per failure, error and warning. The warning lines are how step 10 can
#: tell a strict pass from one that printed NOTCHECKED.
PYTEST = ("uv", "run", "--frozen", "pytest", "-q", "-rfEw", "-p", "no:cacheprovider")

#: `version = "X.Y.Z"` at the start of a line: the `[project]` version, the only such line.
PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")(?P<version>[^"]+)(")', re.MULTILINE)

#: The first `## [...]` heading in CHANGELOG.md.
CHANGELOG_TOP = re.compile(r"^## \[(?P<label>[^\]]+)\](?P<rest>.*)$", re.MULTILINE)

#: The tag-pinned raw URL the build writes into the long description.
TAGGED_RAW = "https://raw.githubusercontent.com/kphutt/gdmutant/v{version}/"
COMMIT_RAW = "https://raw.githubusercontent.com/kphutt/gdmutant/{sha}/"


class StepFailed(Exception):
    """A step ran and found something wrong."""


class StepNotRun(Exception):
    """A step could not run, so nothing is known either way."""


@dataclass
class Step:
    name: str
    state: str = "NOTRUN"
    detail: str = ""


@dataclass
class Rehearsal:
    """What the rehearsal learned, in the order it learned it."""

    steps: list[Step] = field(default_factory=list)

    def run(self, name: str, action: Callable[[], str]) -> bool:
        """Run one step. Returns whether the rehearsal may go on: every step after a failure
        builds on what failed, but a step that could not run leaves the tree as it found it."""
        step = Step(name)
        self.steps.append(step)
        try:
            step.detail = action()
            step.state = "PASS"
        except StepFailed as problem:
            step.state, step.detail = "FAIL", str(problem)
        except StepNotRun as reason:
            step.state, step.detail = "NOTRUN", str(reason)
        except Exception as crash:  # noqa: BLE001 - a crashed step is a failed step, not a traceback
            step.state, step.detail = "FAIL", f"crashed: {type(crash).__name__}: {crash}"
        return step.state != "FAIL"

    def skip_rest(self, names: list[str], because: str) -> None:
        for name in names:
            self.steps.append(Step(name, "NOTRUN", because))

    def exit_code(self) -> int:
        states = {step.state for step in self.steps}
        if "FAIL" in states:
            return FAILED
        if "NOTRUN" in states or not self.steps:
            return NOT_RUN
        return OK

    def report(self) -> str:
        width = max((len(step.name) for step in self.steps), default=0)
        lines = [f"{step.state:<6} {step.name:<{width}}  {step.detail}" for step in self.steps]
        verdict = {OK: "the release path is clear", FAILED: "a step failed", NOT_RUN: ""}
        code = self.exit_code()
        if code == NOT_RUN:
            verdict[NOT_RUN] = "at least one step did not run, so this is not a pass"
        lines.append("")
        lines.append(f"rehearsal: {verdict[code]}")
        return "\n".join(lines)


# --- Pure pieces: every edit the runbook asks for, testable without git or uv ------------------


def next_patch(version: str) -> str:
    """`0.1.3` -> `0.1.4`. Anything that is not three dot-separated integers is refused."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise StepFailed(f"pyproject.toml's version {version!r} is not of the form X.Y.Z")
    major, minor, patch = (int(part) for part in parts)
    return f"{major}.{minor}.{patch + 1}"


def rehearsal_version(packaged: str, tags: set[str]) -> str:
    """The version a release cut from here would carry.

    Untagged means a release is already in progress: rehearse that exact version. Tagged means the
    next release has not been started: rehearse the next patch."""
    return next_patch(packaged) if f"v{packaged}" in tags else packaged


def set_pyproject_version(text: str, version: str) -> str:
    new, count = PYPROJECT_VERSION.subn(rf"\g<1>{version}\g<3>", text)
    if count != 1:
        raise StepFailed(
            f'expected exactly one `version = "..."` line in pyproject.toml, found {count}. '
            "Runbook step 1 no longer matches the tree."
        )
    return new


def bump_pin_comments(text: str, pin: re.Pattern[str], old: str, new: str, name: str) -> str:
    """Point every `# vX.Y.Z` pin comment at `new`. Runbook step 1.

    Every comment must name `old` (the last release) or `new` (a bump already made). Anything else
    means an earlier release missed this pin, which is the drift this rehearsal exists to find."""
    found = list(pin.finditer(text))
    if not found:
        raise StepFailed(f"{name} carries no `kphutt/gdmutant@<sha> # vX.Y.Z` pin to bump")
    strays = sorted({m.group("version") for m in found} - {old, new})
    if strays:
        raise StepFailed(
            f"{name} has a pin comment naming {', '.join(strays)}, neither the last release "
            f"({old}) nor the one being cut ({new})"
        )
    return pin.sub(lambda m: m.group(0).replace(f"v{m.group('version')}", f"v{new}"), text)


def date_changelog(text: str, version: str, today: str) -> str:
    """Rename the top `## [Unreleased]` heading to `## [X.Y.Z] - YYYY-MM-DD`. Runbook step 2."""
    top = CHANGELOG_TOP.search(text)
    if top is None:
        raise StepFailed("CHANGELOG.md has no `## [...]` heading")
    label = top.group("label")
    if label == version:
        return text  # already dated by a release in progress
    if label != "Unreleased":
        raise StepFailed(
            f"CHANGELOG.md's top heading is `## [{label}]`, not `## [Unreleased]`, so there is "
            "nothing to date for the next release. Add an Unreleased section."
        )
    return text[: top.start()] + f"## [{version}] - {today}" + text[top.end() :]


def rewrite_tag_url(url: str, version: str, sha: str) -> str:
    """The same file at the rehearsed commit, since the rehearsed tag does not exist on GitHub."""
    tagged = TAGGED_RAW.format(version=version)
    return COMMIT_RAW.format(sha=sha) + url[len(tagged) :] if url.startswith(tagged) else url


def pytest_args(quick: bool) -> list[str]:
    return [*PYTEST, "--no-cov", *QUICK_TESTS] if quick else list(PYTEST)


# --- The impure half: a throwaway clone, git, and uv -------------------------------------------


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    # The clone gets its own `.venv`. An inherited VIRTUAL_ENV (this script is usually launched with
    # `uv run`) would point uv at the source checkout's environment instead.
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env
    )
    if check and result.returncode != 0:
        raise StepFailed(f"`{' '.join(args)}` exited {result.returncode}:\n" + evidence(result))
    return result


def evidence(result: subprocess.CompletedProcess[str]) -> str:
    """The lines that say why a command failed. pytest's own FAILED/ERROR lines when it printed
    any, since its last lines are a coverage table; otherwise the end of the output."""
    # Joined with a newline: stdout rarely ends in one, and gluing the two streams would merge
    # pytest's last line with the first line of stderr.
    lines = f"{result.stdout}\n{result.stderr}".strip().splitlines()
    named = [ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
    return "\n".join([*named[:10], lines[-1]] if named else lines[-15:])


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise StepNotRun(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass looks its own module up in sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pytest_summary(result: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else "pytest printed nothing"


def rehearse(quick: bool, keep: bool, offline: bool = False) -> Rehearsal:
    rehearsal = Rehearsal()
    for tool in ("git", "uv"):
        if shutil.which(tool) is None:
            rehearsal.steps.append(Step("preconditions", "NOTRUN", f"`{tool}` is not on PATH"))
            return rehearsal

    source_sha = run(["git", "rev-parse", "HEAD"], REPO).stdout.strip()
    tags = set(run(["git", "tag", "--list", "v*"], REPO).stdout.split())
    packaged = str(
        tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    )
    version = rehearsal_version(packaged, tags)
    last = packaged if f"v{packaged}" in tags else None
    tag = f"v{version}"

    scratch = Path(tempfile.mkdtemp(prefix="gdmutant-rehearsal-"))
    origin, work = scratch / "origin.git", scratch / "work"
    names = [
        "throwaway origin + clone",
        f"runbook steps 1-2 for {tag}",
        "version-bump commit: test suite",
        f"tag {tag}: check_release_tag.py",
        f"tag {tag}: test suite",
        f"tag {tag}: uv build + twine check",
        f"tag {tag}: long-description images",
        "runbook step 10: bump_action_pins.py + strict pin test",
    ]

    def clone() -> str:
        run(["git", "clone", "--quiet", "--bare", "--no-local", str(REPO), str(origin)], scratch)
        run(["git", "update-ref", "refs/heads/main", source_sha], origin)
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], origin)
        run(["git", "clone", "--quiet", str(origin), str(work)], scratch)
        for key, value in (("user.name", "release rehearsal"), ("user.email", "rehearsal@invalid")):
            run(["git", "config", key, value], work)
        return f"{source_sha[:12]} in {scratch}"

    def edit() -> str:
        pin = load(work / "scripts" / "bump_action_pins.py", "rehearsal_bump_action_pins")
        pyproject = work / "pyproject.toml"
        pyproject.write_text(set_pyproject_version(pyproject.read_text("utf-8"), version), "utf-8")
        run(["uv", "lock", "--quiet"], work)
        for name in pin.PIN_FILES:
            path = work / name
            text = path.read_text("utf-8")
            path.write_text(
                bump_pin_comments(text, pin.PIN, last or version, version, name), "utf-8"
            )
        changelog = work / "CHANGELOG.md"
        today = datetime.date.today().isoformat()
        changelog.write_text(date_changelog(changelog.read_text("utf-8"), version, today), "utf-8")
        run(["git", "commit", "--quiet", "--all", "-m", f"release: {tag} (rehearsal)"], work)
        run(["git", "push", "--quiet", "origin", "HEAD:main"], work)
        changed = run(["git", "show", "--stat", "--format=", "HEAD"], work).stdout.strip()
        return f"{len(changed.splitlines()) - 1} files changed, committed and pushed to main"

    def suite() -> str:
        run(["uv", "sync", "--frozen", "--quiet"], work)
        return pytest_summary(run(pytest_args(quick), work))

    def tag_guard() -> str:
        run(["git", "tag", tag], work)
        run(["git", "push", "--quiet", "origin", tag], work)
        guard = ["uv", "run", "--frozen", "python", "scripts/check_release_tag.py", tag]
        return run(guard, work).stdout.strip()

    def build() -> str:
        dist = work / "dist"
        shutil.rmtree(dist, ignore_errors=True)
        run(["uv", "build", "--quiet"], work)
        # `uv build` also drops a `.gitignore` into dist/, which twine refuses as a distribution.
        built = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
        if len(built) != 2:
            raise StepFailed(f"expected one wheel and one sdist, found {[p.name for p in built]}")
        run(["uv", "run", "--frozen", "--with", "twine", "twine", "check", *map(str, built)], work)
        return ", ".join(p.name for p in built) + ", twine check passed"

    def images() -> str:
        if offline:
            raise StepNotRun("--offline: no image was fetched, so nothing is known about them")
        on_main = run(
            ["git", "merge-base", "--is-ancestor", source_sha, "origin/main"], REPO, False
        )
        if on_main.returncode != 0:
            raise StepNotRun(
                f"{source_sha[:12]} is not on origin/main, so its assets cannot be fetched from "
                "GitHub. Push it, or run this in CI, where it runs on every merge."
            )
        checker = load(work / "scripts" / "check_readme_images.py", "rehearsal_readme_images")
        fetch = checker.urllib_fetcher()

        def at_commit(url: str) -> object:
            response = fetch(rewrite_tag_url(url, version, source_sha))
            if response.final_url == rewrite_tag_url(url, version, source_sha):
                response = dataclasses.replace(response, final_url=url)
            return response

        code = checker.main(["check_readme_images.py", "--dist-dir", str(work / "dist")], at_commit)
        if code == checker.UNVERIFIED:
            raise StepNotRun("the network could not answer for at least one image")
        if code != checker.OK:
            raise StepFailed(f"check_readme_images.py exited {code}; its report is above")
        return "every image resolves (the banner checked at the rehearsed commit)"

    def pins() -> str:
        run(["uv", "run", "--frozen", "python", "scripts/bump_action_pins.py"], work)
        run(["git", "commit", "--quiet", "--all", "-m", f"chore: bump action pins to {tag}"], work)
        result = run([*PYTEST, "--no-cov", "tests/test_action_pin.py"], work)
        if "NOTCHECKED" in result.stdout:
            raise StepFailed(
                "test_action_pin.py passed but warned NOTCHECKED after the pin bump, so the strict "
                "SHA comparison never ran. After step 10 every pin must be comparable."
            )
        return pytest_summary(result)

    actions = [clone, edit, suite, tag_guard, suite, build, images, pins]
    try:
        for index, (name, action) in enumerate(zip(names, actions, strict=True)):
            if not rehearsal.run(name, action):
                rehearsal.skip_rest(names[index + 1 :], f"an earlier step failed ({name})")
                break
    finally:
        if keep:
            print(f"kept the throwaway copy at {scratch}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    return rehearsal


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rehearse_release.py", description=__doc__.split("\n")[0])
    parser.add_argument("--quick", action="store_true", help="run only the release-shaped tests")
    parser.add_argument(
        "--offline", action="store_true", help="skip the one step that needs the network"
    )
    parser.add_argument("--keep", action="store_true", help="keep the throwaway copy afterwards")
    args = parser.parse_args(argv[1:])
    rehearsal = rehearse(args.quick, args.keep, args.offline)
    print(rehearsal.report())
    return rehearsal.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
