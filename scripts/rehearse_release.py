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
  3. The version-bump commit, before the tag exists: the suite (`pytest`), which is the only leg
     of Verify that the release commit can newly fail -- ci.yml already ran the whole `verify` job
     for real on the release pull request.
  4. Runbook step 4: tag it. Then, on the tagged commit, what publish.yml's gate runs: the tag
     guard (scripts/check_release_tag.py), ci.yml's whole `verify` job via
     scripts/verify_local.py, `uv build`, `twine check`, and every image in the built long
     description (scripts/check_readme_images.py).
  5. Runbook step 10: scripts/bump_action_pins.py, committed, then tests/test_action_pin.py must
     pass with no NOTCHECKED warning, because by then every pin can be checked.
  6. ci.yml's `license-check` job, publish.yml's last non-upload gate. It runs last because it
     rebuilds the clone's environment without the dev group.

WHAT THE REHEARSAL RUNS, AGAINST WHAT publish.yml RUNS, SO NEITHER LIST IS HELD IN SOMEONE'S HEAD.
Both read their commands out of ci.yml through scripts/verify_local.py, so the overlap cannot
drift. Walked here: provenance's tag guard, verify (Linux), license-check, build + twine check,
readme-images. NOT walked here, and a green run says nothing about any of them:

  - provenance's ancestry guard, which needs an authenticated fetch of origin/main;
  - verify on Windows, and the two Godot self-tests, which need a runner this does not have
    (ci.yml does run all three on every pull request, just never on a tagged commit);
  - secret-scan's full-history gitleaks pass, which needs gitleaks on PATH;
  - publish-pypi's OIDC upload, and verify-published, which needs a real upload to install from.

`--quick` replaces the two suite legs with the release-shaped tests and no coverage floor, so it
is a weaker run by design. The report names the command each step ran, and `--quick` never
reports a `verify` line it did not produce.

The banner URL in the built long description names the rehearsed tag, which does not exist on
GitHub. The image check fetches the same path at the commit being rehearsed instead, which must
already be on origin/main. When it is not (a local run on an unpushed branch), that step reports
NOTRUN rather than guessing.

Exit codes: 0 every step passed; 1 a step failed; 2 a step could not run, or the rehearsal could
not start. A step that did not run is never reported as a pass. Refusing to start is itself a
reported step, so a tree this cannot rehearse -- not a git checkout, no tags fetched, no `uv` --
prints a NOTRUN line and exits 2 rather than a traceback.

Usage, from the repo root::

    uv run python scripts/rehearse_release.py            # the real gate at the tagged commit
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
import typing
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

#: ci.yml's whole `verify` job, read out of the workflow rather than restated here. This is what
#: publish.yml's `verify-ubuntu` / `verify-windows` run on the tagged commit, and it is six steps,
#: not one: ruff, ruff format, gdlint, mypy, pytest, pip-audit. The last of those is the one a
#: release can newly break without anybody touching Python, because a release regenerates
#: `uv.lock` -- so rehearsing pytest alone would leave the gate's most release-shaped leg unwalked.
VERIFY = ("uv", "run", "--frozen", "python", "scripts/verify_local.py")

#: ci.yml's `license-check` job, which publish.yml also gates a release on. Same script, so it
#: cannot drift from the workflow either.
LICENSE_CHECK = (*VERIFY, "--job", "license-check")

#: The committer the rehearsal's own commits carry, passed per git command rather than written into
#: the clone with `git config`.
#:
#: WHY NOT `git config`. The rehearsal runs this repo's own suite inside the clone, and
#: `tests/test_public_readiness.py::test_no_tracked_file_repeats_the_local_git_identity` scans every
#: tracked file for the clone's LOCAL git identity. The rehearsal's name is in the tree -- this
#: file, and the workflow's `name:` -- so an identity stored in the clone made that test find the
#: rehearsal's own source and turned the suite step red on every single run. Passed per command,
#: nothing is stored, and the clone resolves whatever identity the machine already has: the
#: maintainer's own on a local run, none in CI. That is what a real release sees too.
COMMITTER = ("-c", "user.name=a release dry run", "-c", "user.email=rehearsal@invalid")

#: Environment every command the rehearsal spawns gets, on top of the inherited one.
#:
#: `PYTHONUTF8`/`PYTHONIOENCODING`: a child that writes a non-ASCII character (verify_local.py's own
#: header line carries an em dash) encodes it in the console code page, which on Windows is cp1252,
#: and this script decodes every child as UTF-8. Forcing the child to UTF-8 makes the two agree.
#:
#: `core.hooksPath`: this turns git hooks OFF, so read the next sentence before touching it. Every
#: repository it reaches is a throwaway in a temp directory -- the clone, whose `origin` is a bare
#: repo beside it, and the scratch repositories the suite itself builds and commits to. None of them
#: is the real checkout, none can reach a remote, and the directory is deleted when the rehearsal
#: ends, so no commit this disarms a hook for can ever be pushed anywhere. Without it, a machine
#: with a global hooks path runs the maintainer's own pre-commit gate against each of those scratch
#: repositories: measured in scripts/check_mutation_baseline.py (which neutralises it for this same
#: reason) at rather more than twice the suite's runtime, and able to fail a commit outright.
#:
#: The cost, named rather than hidden: the rehearsal's own two commits run no hooks, so it does not
#: rehearse the local pre-commit gate a maintainer's real step 1-2 commit would fire. The gitleaks
#: half of that is already on the docstring's list of what this cannot reach.
CHILD_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "",
}

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


def gate_args(quick: bool) -> list[str]:
    """What the tagged commit is checked with: publish.yml's own gate, or the quick stand-in.

    `--quick` exists so a maintainer can walk the path in under a minute before cutting a release.
    It is deliberately weaker, and the report says so by naming the command it ran."""
    return [*PYTEST, "--no-cov", *QUICK_TESTS] if quick else list(VERIFY)


def commit_args(message: str, allow_empty: bool = False) -> list[str]:
    """`git commit`, with the committer passed per command. See COMMITTER for why not `git config`.

    `allow_empty` is for the version-bump commit only. When a release is already in progress every
    edit runbook steps 1-2 ask for has been made already, so the commit is genuinely empty and the
    rehearsal must carry on rather than read git's "nothing to commit" as a failure. The step 10
    pin bump never gets it: an empty commit there means `bump_action_pins.py` did nothing, which is
    a finding, not a no-op."""
    empty = ["--allow-empty"] if allow_empty else []
    return ["git", *COMMITTER, "commit", "--quiet", "--all", *empty, "-m", message]


def refuse_a_stored_identity(stored: str) -> None:
    """The clone must carry no local git identity. See COMMITTER for the whole story.

    A belt on top of those braces: this is the one defect that made the gate red on every run, and
    the next person to reach for `git config` in the clone should hit a sentence explaining why
    rather than a failing assertion three steps later in somebody else's test."""
    if stored:
        raise StepFailed(
            f"the clone carries a local git identity ({stored!r}). The suite this rehearsal runs "
            "scans every tracked file for that name, and the rehearsal's own name is in the tree, "
            "so a stored identity turns the suite step red on every run. Pass it per command "
            "instead: see COMMITTER."
        )


def refuse_an_unchanged_version_bump(dirty: bool, last: str | None, tag: str) -> None:
    """Runbook steps 1-2 asked for four edits and the tree did not move. Exactly one state explains
    that innocently: the packaged version is not tagged yet, so a release of it is already in
    progress and its own release pull request made every edit already. Anything else means an edit
    found what it expected and then wrote back what was already there."""
    if dirty or last is None:
        return
    raise StepFailed(
        f"{tag} is the next release after v{last}, so runbook steps 1-2 had a version to bump, a "
        "lockfile to re-resolve, pin comments to move and a changelog heading to date -- and "
        "changed nothing at all. One of those edits no longer edits the file it names."
    )


def rewrite_text(path: Path, edit: typing.Callable[[str], str]) -> None:
    """Rewrite a text file through `edit`, keeping every line ending it already has.

    `Path.read_text` folds CRLF into LF and `Path.write_text` turns each LF into CRLF on Windows,
    so a plain read-edit-write rewrote every line ending of a file the edit did not touch. Git
    then listed the file as modified while `git commit --all` normalised the endings away and found
    nothing to commit. `newline=""` switches both translations off."""
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(edit(text))


def tree_has_changes(work: Path) -> bool:
    """Does the clone differ from HEAD in content? Not "does `git status` list a file".

    `git status --porcelain` lists a file whose line endings were rewritten, which `git commit`
    then calls empty. `git diff --quiet HEAD` compares what a commit would record (exit 1 means a
    difference), so the answer matches what the commit will do. Staged and untracked files do not
    count: the edits only modify tracked files, and `commit --all` only takes those too.

    Three outcomes, not two. Exit 0 is "no changes" and exit 1 is "changes". Anything else is git
    failing to look (129 outside a repository, 128 in one with no HEAD), and reading that as
    "changes" would silence `refuse_an_unchanged_version_bump` by another route. It raises."""
    result = run(["git", "diff", "--quiet", "HEAD"], work, check=False)
    if result.returncode not in (0, 1):
        raise StepFailed(
            f"`git diff --quiet HEAD` exited {result.returncode}, which is neither 'no changes' "
            "(0) nor 'changes' (1), so the clone could not be inspected:\n" + evidence(result)
        )
    return result.returncode == 1


def say(text: str, stream: typing.TextIO | None = None) -> None:
    """Print `text` on a console that may not be able to encode it.

    The report quotes command output, and gdmutant already shipped a Windows bug where console
    output crashed under the legacy cp1252 code page. A rehearsal that dies printing its own
    verdict is worse than one that prints `?` where a character should be."""
    out = sys.stdout if stream is None else stream
    encoding = out.encoding or "utf-8"
    out.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")


# --- The impure half: a throwaway clone, git, and uv -------------------------------------------


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    # The clone gets its own `.venv`. An inherited VIRTUAL_ENV (this script is usually launched with
    # `uv run`) would point uv at the source checkout's environment instead.
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    # `errors="replace"`: a byte the child wrote in some other encoding must not raise out of the
    # decode. The rehearsal's job is to report what a step did, and a step whose evidence cannot be
    # read is still a step with a verdict. check_published_package.py reads its output the same way.
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**env, **CHILD_ENV},
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
    # Stripped before matching: verify_local.py indents its own `  FAILED: <step name>` lines, and
    # those name WHICH of ci.yml's six verify steps went red. Matched unindented, the report said
    # only "FAILED (1/6)" and left the reader to go and find out which one.
    named = [ln for ln in lines if ln.strip().startswith(("FAILED", "ERROR"))]
    return "\n".join([*(ln.strip() for ln in named[:10]), lines[-1]] if named else lines[-15:])


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


def step_summary(result: subprocess.CompletedProcess[str]) -> str:
    """The line that says what a step actually ran.

    Neither tool's last line is the answer: verify_local.py ends on advice about what CI already
    covers, and pytest ends on a coverage total. verify_local.py's `All N steps of jobs.X passed`
    line is, because it names how many steps ran -- the number that would have gone quiet if a
    future `verify` job grew a step this rehearsal never replayed."""
    for line in result.stdout.splitlines():
        if line.startswith("All ") and " passed on " in line:
            return line.strip()
    return pytest_summary(result)


@dataclass(frozen=True)
class Plan:
    """What a release cut from this checkout would be, worked out before anything is copied."""

    source_sha: str
    version: str
    #: The last released version, or None when the packaged version is not tagged yet, which means
    #: a release of it is already in progress.
    last: str | None

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def plan_rehearsal(repo: Path = REPO) -> Plan:
    """Decide what to rehearse, or refuse to start.

    Every refusal here is a StepNotRun, never a quiet fallback: each one is a state in which the
    rehearsal could still run to green while checking something other than the release path, which
    is the one outcome a release gate must never produce."""
    for tool in ("git", "uv"):
        if shutil.which(tool) is None:
            raise StepNotRun(f"`{tool}` is not on PATH")
    # `--show-toplevel`, not `--is-inside-work-tree`: mutmut's `mutants/` and a poodle run's temp
    # directory both sit INSIDE the repository, so "inside a work tree" is true there and would
    # have let a copy through. The root of the checkout being this very directory is the property
    # that tells a checkout from a copy of one. Compared as paths, never as strings: git prints
    # forward slashes on Windows too, and a literal comparison is never true there.
    toplevel = run(["git", "rev-parse", "--show-toplevel"], repo, False)
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != repo.resolve():
        raise StepNotRun(
            f"{repo} is not the root of a git checkout, so there is no commit to rehearse. A copy "
            "of the tree is not one: mutmut's `mutants/`, a poodle run's `.poodle-temp/run-N/`, "
            "an unpacked sdist. Run this from a real checkout."
        )
    tags = set(run(["git", "tag", "--list", "v*"], repo).stdout.split())
    if not tags:
        raise StepNotRun(
            "this checkout has no `v*` tag, so which version a release would cut cannot be worked "
            "out. A tagless fetch looks exactly like a repository before its first release, and "
            "guessing would rehearse a version that already shipped: every edit would be a no-op "
            "and the rehearsal would pass having walked nothing. Fetch the tags "
            "(`git fetch --tags`; in CI, actions/checkout with `fetch-depth: 0`)."
        )
    packaged = str(
        tomllib.loads((repo / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    )
    return Plan(
        source_sha=run(["git", "rev-parse", "HEAD"], repo).stdout.strip(),
        version=rehearsal_version(packaged, tags),
        last=packaged if f"v{packaged}" in tags else None,
    )


def rehearse(quick: bool, keep: bool, offline: bool = False) -> Rehearsal:
    rehearsal = Rehearsal()
    # Deciding what to rehearse is itself a step, so a checkout this cannot start from reports a
    # NOTRUN line and exit 2 rather than a traceback. It used to run outside the step machinery,
    # where a tagless or non-checkout tree exited 1 with no report at all.
    plan: Plan | None = None

    def preconditions() -> str:
        nonlocal plan
        plan = plan_rehearsal()
        cut = "already in progress" if plan.last is None else f"the next after v{plan.last}"
        return f"{plan.source_sha[:12]}, rehearsing {plan.tag} ({cut})"

    rehearsal.run("preconditions", preconditions)
    if plan is None:
        return rehearsal
    source_sha, version, last, tag = plan.source_sha, plan.version, plan.last, plan.tag

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
        "license compliance (ci.yml's license-check job)",
    ]

    def clone() -> str:
        run(["git", "clone", "--quiet", "--bare", "--no-local", str(REPO), str(origin)], scratch)
        run(["git", "update-ref", "refs/heads/main", source_sha], origin)
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], origin)
        run(["git", "clone", "--quiet", str(origin), str(work)], scratch)
        # Deliberately no `git config user.name`: see COMMITTER. Checked rather than assumed.
        refuse_a_stored_identity(
            run(["git", "config", "--local", "--get", "user.name"], work, False).stdout.strip()
        )
        return f"{source_sha[:12]} in {scratch}"

    def edit() -> str:
        pin = load(work / "scripts" / "bump_action_pins.py", "rehearsal_bump_action_pins")
        pyproject = work / "pyproject.toml"
        rewrite_text(pyproject, lambda text: set_pyproject_version(text, version))
        run(["uv", "lock", "--quiet"], work)
        for name in pin.PIN_FILES:
            path = work / name
            rewrite_text(
                path,
                lambda text, name=name: bump_pin_comments(
                    text, pin.PIN, last or version, version, name
                ),
            )
        changelog = work / "CHANGELOG.md"
        today = datetime.date.today().isoformat()
        rewrite_text(changelog, lambda text: date_changelog(text, version, today))
        dirty = tree_has_changes(work)
        refuse_an_unchanged_version_bump(dirty, last, tag)
        run(commit_args(f"release: {tag} (rehearsal)", allow_empty=not dirty), work)
        run(["git", "push", "--quiet", "origin", "HEAD:main"], work)
        if not dirty:
            # The only way here: the packaged version is untagged, so a release of it is already in
            # progress and its release pull request already made every edit. An empty commit keeps
            # the rest of the path walkable instead of reading git's "nothing to commit" as a
            # failure -- which it did, turning the rehearsal red on main for the whole window
            # between a release pull request merging and its tag being pushed.
            return f"nothing left to edit: a release of {tag} is already in progress"
        changed = run(["git", "show", "--stat", "--format=", "HEAD"], work).stdout.strip()
        return f"{len(changed.splitlines()) - 1} files changed, committed and pushed to main"

    def suite() -> str:
        run(["uv", "sync", "--frozen", "--quiet"], work)
        return pytest_summary(run(pytest_args(quick), work))

    def gate() -> str:
        """What publish.yml runs on the tagged commit, which is ci.yml's whole `verify` job.

        Not the same command as `suite` above, on purpose. ci.yml already ran `verify` for real on
        the release pull request, so rehearsing it at the version-bump commit would re-run a check
        that is not release-only. The tagged commit is the one nobody ever walks between releases,
        and publish.yml gates the upload on the whole job there -- pip-audit included, which is the
        leg a regenerated `uv.lock` can newly fail without a line of Python changing."""
        run(["uv", "sync", "--frozen", "--quiet"], work)
        return step_summary(run(gate_args(quick), work))

    def licenses() -> str:
        """publish.yml's `license-check` gate, last because it rebuilds the clone's environment
        without the dev group and nothing after it would want that. It reads `uv.lock`, which has
        not changed since the tag, so running it here says exactly what it would say there."""
        return step_summary(run(list(LICENSE_CHECK), work))

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
        # No --allow-empty, unlike the version-bump commit: if bump_action_pins.py left the tree
        # untouched, git's "nothing to commit" is the finding, not an inconvenience.
        run(commit_args(f"chore: bump action pins to {tag}"), work)
        result = run([*PYTEST, "--no-cov", "tests/test_action_pin.py"], work)
        if "NOTCHECKED" in result.stdout:
            raise StepFailed(
                "test_action_pin.py passed but warned NOTCHECKED after the pin bump, so the strict "
                "SHA comparison never ran. After step 10 every pin must be comparable."
            )
        return pytest_summary(result)

    actions = [clone, edit, suite, tag_guard, gate, build, images, pins, licenses]
    try:
        for index, (name, action) in enumerate(zip(names, actions, strict=True)):
            if not rehearsal.run(name, action):
                rehearsal.skip_rest(names[index + 1 :], f"an earlier step failed ({name})")
                break
    finally:
        if keep:
            say(f"kept the throwaway copy at {scratch}", sys.stderr)
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
    say(rehearsal.report())
    return rehearsal.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
