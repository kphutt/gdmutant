"""Fail the build when a tracked file names something only a private workspace should know.

gdmutant is written in a private repo and is meant to be read by strangers one day. The two
audiences want different text, and the gap between them is invisible while you are writing:
a sentence that reads fine to its author names a machine, a colleague, or an internal tool that
no reader outside will recognise, and it survives every later edit because nobody is looking
for it.

Three hand sweeps of this repo each missed at least one instance. One of them rewrapped the exact
line that was leaking and left the leak intact, because the giveaway word here is `operator` and in
this codebase `operator` almost always means a *mutation operator*: the thing that turns `>` into
`>=`. A search for it returns dozens of legitimate hits in the engine, the design doc and the
survivor reference, and the handful of real ones sit inside that pile. A reader cannot win that,
so this module does it mechanically instead.

The rules below are deliberately narrow. Each one matches a *construction* that cannot mean the
domain term, never the bare word:

- `this operator's` (a demonstrative pointing at a person) rather than `the operator's` (which is
  how every mutation-operator sentence in this repo is written).
- `Litmus`, `Catalyst`, `ai-toolkit` and friends: private tool names, matched case-sensitively
  where an ordinary English word shares the spelling.
- A home directory (`~/dev/`, `C:\\Users\\...`) rather than a path.
- An e-mail address outside the reserved test domains, which is how a personal address arrives.
- A tracker ticket id, which belongs to the tracker and not to a file a stranger will read.

The scan reads `git ls-files`, not a directory walk, so a file added anywhere in the tree is
covered the day it is added and there is no directory left over for a leak to hide in.

This is a hard failure, not advice. It is also not a proof: judgement calls like a stale status
paragraph, a test count, or a roadmap-progress note are equally unwelcome in a file a stranger
reads, and no regular expression finds those. Those stay a review job. What lives here is the
class that a reader provably cannot catch by reading.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: This module states the very names it forbids, so it cannot pass its own scan. Exempting it is
#: the accepted cost of keeping the list readable: a rule file that spells out what it bans is the
#: same shape as `.gitleaks.toml`, and a list of five words says far less than a sentence using
#: one of them would. Nothing else may be added here without the same argument written beside it.
SELF = Path(__file__).name


@dataclass(frozen=True)
class Rule:
    """One forbidden construction, its compiled pattern, and what to do about a hit."""

    name: str
    pattern: re.Pattern[str]
    fix: str


RULES: tuple[Rule, ...] = (
    Rule(
        name="a possessive that can only mean a person",
        # `this/our/my operator's` never describes a mutation operator: every such sentence in
        # this repo says `the operator's`, `an operator's` or `that operator's`. The second half
        # catches the nouns that give the person away whatever the determiner is.
        pattern=re.compile(
            r"\b(?:this|our|my)\s+operator's\b|\boperator's\s+(?:fleet|machine|laptop|inbox)\b",
            re.IGNORECASE,
        ),
        fix=(
            "say what is actually true of this project instead of whose habit it is: "
            "'other private repos set up the same way', 'the maintainer's own machine', or just "
            "drop the ownership and state the practice."
        ),
    ),
    Rule(
        name="a private tool or fleet name",
        # Case-sensitive where an ordinary word shares the spelling, so `a litmus test` and
        # `no precedent for this` stay legal English. `Precedent` is common enough at the start
        # of a sentence that only the tool-shaped collocations are matched.
        pattern=re.compile(
            r"\bLitmus\b|\bCatalyst\b|(?i:\bprecedent\s+(?:review|pass|method|loop|round)\b)"
            r"|(?i:\bai-toolkit\b)|(?i:\blodestar\b)"
        ),
        fix=(
            "name the effect, not the tool: 'a review pass found', 'a design review', "
            "'a second reader'. Nobody outside can look these up, so they read as noise at best."
        ),
    ),
    Rule(
        name="a path inside somebody's home directory",
        pattern=re.compile(
            r"~/dev/|[A-Za-z]:\\Users\\|/Users/[A-Za-z0-9._-]+/|/home/(?!runner\b)[A-Za-z0-9._-]+/"
        ),
        fix=(
            "use a repository-relative path, or a placeholder like `<path-to-a-checkout>`. "
            "An absolute home path is both a private detail and wrong on every other machine."
        ),
    ),
    Rule(
        name="a tracker ticket reference",
        pattern=re.compile(r"\bLOD-\d+\b|\blinear\.app\b"),
        fix=(
            "the tracker is private and its ids mean nothing here. Link a GitHub issue, or "
            "write the requirement out."
        ),
    ),
)

#: Domains an address may use in a tracked file. RFC 2606 reserves the `example.*` names and the
#: `.invalid` TLD precisely so that documentation and fixtures never have to invent a real one.
#: GitHub's noreply host is the account-scoped address the commit metadata already carries.
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
    "users.noreply.github.com",
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: Local git identities too generic to search a whole tree for without matching prose.
_GENERIC_GIT_NAMES = frozenset(
    {"root", "user", "test", "admin", "build", "runner", "github-actions", "github-actions[bot]"}
)


def _tracked_files() -> list[Path]:
    """Every file git tracks, as absolute paths.

    `git ls-files` rather than a walk: a new directory is in scope the moment it is committed,
    and there is no ignore list here to fall out of date. An empty answer is a real answer, and
    `_scan_targets` decides what to do about it.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _scan_targets() -> list[Path]:
    """The tracked files to read, or a skip when git lists none.

    mutmut runs this suite from a copy of the tree under `mutants/`, which is gitignored, so
    `git ls-files` there legitimately returns nothing. Without this, every check below would
    fail inside that copy and abort the mutation baseline, which costs the whole dogfood score
    (see `tests/test_mutation_baseline_inputs.py` for the two ways that happens). The same
    branch covers an unpacked source tarball, which has no git to ask.
    """
    files = _tracked_files()
    if not files:
        pytest.skip("git lists no tracked files here, so there is nothing to scan")
    return files


def _readable_text(path: Path) -> str | None:
    """The file's text, or None when it is missing or is not decodable UTF-8 (a PNG asset)."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _hits(rule_pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for path in _scan_targets():
        if path.name == SELF:
            continue
        text = _readable_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            match = rule_pattern.search(line)
            if match:
                rel = path.relative_to(REPO_ROOT).as_posix()
                hits.append(f"  {rel}:{number}: {line.strip()}  <- matched {match.group(0)!r}")
    return hits


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_no_tracked_file_leaks_a_private_detail(rule: Rule) -> None:
    hits = _hits(rule.pattern)
    assert not hits, (
        f"{len(hits)} tracked line(s) contain {rule.name}, which would be public the moment this "
        f"repo is:\n" + "\n".join(hits) + f"\n\nFix: {rule.fix}"
    )


def test_no_tracked_file_carries_a_real_e_mail_address() -> None:
    hits = []
    for path in _scan_targets():
        if path.name == SELF:
            continue
        text = _readable_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for address in _EMAIL.findall(line):
                domain = address.rsplit("@", 1)[1].lower()
                if domain not in ALLOWED_EMAIL_DOMAINS and not domain.endswith(".invalid"):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    hits.append(f"  {rel}:{number}: {address}")
    assert not hits, (
        "these tracked lines carry an e-mail address that is not a reserved documentation "
        "domain, so publishing this repo publishes the address:\n" + "\n".join(hits) + "\n\n"
        "Fix: use an `example.com` / `.invalid` address in fixtures and docs. A genuine contact "
        "address belongs in SECURITY.md's reporting link, not in prose."
    )


def test_no_tracked_file_repeats_the_local_git_identity() -> None:
    """The name on your commits is the one you are most likely to paste into a file by accident.

    Read off `git config` rather than written down here, for two reasons: this file would
    otherwise contain the very name it is trying to keep out of the tree, and a check derived
    from the local identity protects whoever is working in the clone rather than one person.
    """
    _scan_targets()  # skip early where there is no tree, before asking git for an identity
    name = subprocess.run(
        ["git", "config", "user.name"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if len(name) < 4 or name.lower() in _GENERIC_GIT_NAMES:
        pytest.skip(f"local git identity {name!r} is absent or too generic to search for")

    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits = _hits(pattern)
    assert not hits, (
        f"these tracked lines contain the name on your commits ({name!r}):\n"
        + "\n".join(hits)
        + "\n\nFix: write the role, not the person. 'The maintainer', 'a reviewer', "
        "'whoever cuts the release'."
    )


def test_the_scan_reaches_the_whole_tree() -> None:
    """A scan that quietly stopped finding files would turn every check above into a green no-op.

    Pin that it sees a real tree, and specifically that it reaches the two directories the
    leaks have historically come from: prose written for the author rather than for a reader.
    """
    found = {path.relative_to(REPO_ROOT).as_posix() for path in _scan_targets()}
    assert len(found) > 50, (
        f"only {len(found)} tracked files found, so the scan is not seeing the tree"
    )
    assert "README.md" in found
    assert any(name.startswith("docs/decisions/") for name in found)
    assert any(name.startswith("tests/") for name in found)
