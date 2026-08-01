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

**A rule is only as good as the text it is shown.** A red-team pass drove eight payloads straight
past an earlier version of this module, every one of them an ordinary authoring accident rather
than an attack, so what a rule gets shown is now as deliberate as the rule itself:

- The *file name* is scanned, not only the content. Under `git ls-files` the path is free
  information, and it was the one thing nobody was reading.
- Text is *normalised* first. A zero-width space pasted out of rendered markdown splits a term in
  half while looking identical, and a smart-quotes autocorrect turns `operator's` into a curly
  apostrophe the possessive rule cannot see. Both are silent, both are one paste away.
- Line breaks are *closed up* before matching, in both of the ways a wrap can split a term (to a
  space, and to nothing). The sweep that rewrapped a leaking line and left it intact is the
  documented history of this exact failure, and a line-at-a-time regular expression cannot see it.
- A file that is not UTF-8 is *still read*, through a fallback, rather than dropped. One stray
  byte used to disarm the scan for a whole file without saying so. Only a genuine binary, which
  holds no prose for a reader to leak, comes back unread.
- The scan *refuses to pass when it did not run*. It skips only where it can name the reason
  positively (a mutation tool's copy of the tree, an unpacked source distribution). Git failing
  for any other reason is a failure, not a pass: a guard that reports clean because it read
  nothing is the exact hazard `test_the_scan_reaches_the_whole_tree` exists to catch.

This is a hard failure, not advice. It is also not a proof: judgement calls like a stale status
paragraph, a test count, or a roadmap-progress note are equally unwelcome in a file a stranger
reads, and no regular expression finds those. Those stay a review job. What lives here is the
class that a reader provably cannot catch by reading.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: This module states the very names it forbids, so it cannot pass its own scan. Exempting it is
#: the accepted cost of keeping the list readable: a rule file that spells out what it bans is the
#: same shape as `.gitleaks.toml`, and a list of five words says far less than a sentence using
#: one of them would. Nothing else may be added here without the same argument written beside it.
#:
#: The whole path, not the base name. Matching on the base name exempted every file in the tree
#: that happened to be called `test_public_readiness.py`, and this project ships a corpus of
#: fixture projects, so a second file wearing that name is a rename away rather than far-fetched.
SELF = Path(__file__).resolve()


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
        # Case-insensitive, because Windows paths are: `C:\\users\\...` names exactly the same
        # directory as `C:\\Users\\...` and is exactly as private, and the case-sensitive version
        # of this rule waved it through.
        pattern=re.compile(
            r"~/dev/|[A-Za-z]:\\Users\\|/Users/[A-Za-z0-9._-]+/|/home/(?!runner\b)[A-Za-z0-9._-]+/",
            re.IGNORECASE,
        ),
        fix=(
            "use a repository-relative path, or a placeholder like `<path-to-a-checkout>`. "
            "An absolute home path is both a private detail and wrong on every other machine."
        ),
    ),
    Rule(
        name="a tracker ticket reference",
        pattern=re.compile(r"\bLOD-\d+\b|\blinear\.app\b", re.IGNORECASE),
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

#: Local git identities too generic to search a whole tree for without matching prose, plus the
#: tool identities that legitimately appear all over this tree. A name only leaks privacy when it
#: belongs to a person, so when the commits are authored by a bot or an assistant, that same name
#: showing up in a file is the tool being discussed, not somebody being named. `claude` is here
#: because this repo is developed largely through Claude Code sessions, whose git identity is
#: `Claude`, and `CLAUDE.md` and the docs mention it throughout: without the entry the check
#: fails the whole suite on its own primary contribution path.
_GENERIC_GIT_NAMES = frozenset(
    {
        "root",
        "user",
        "test",
        "admin",
        "build",
        "runner",
        "github-actions",
        "github-actions[bot]",
        "claude",
        "claude code",
        "claude[bot]",
        "dependabot",
        "dependabot[bot]",
    }
)


# --- what the rules are shown ------------------------------------------------------------------

#: Characters a reader cannot see at all. They arrive by pasting out of a browser or a rendered
#: document, and a single one of them inside a term defeats every rule in this file while the
#: line still reads exactly as it did. Dropped before matching, never reported as a difference.
_INVISIBLE = (0x00AD, 0x180E, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)

#: Characters a reader reads as ASCII punctuation. A word processor, a browser paste or a
#: smart-quotes autocorrect substitutes these silently, and the possessive rule, which is the
#: reason this module exists, is switched off by exactly one of them: the curly apostrophe. The
#: dashes are here for the same reason a hyphenated private name is: `ai-toolkit` typed through
#: an autocorrect is not spelled with the hyphen the rule looks for.
_LOOKALIKE = {
    0x2018: "'",
    0x2019: "'",
    0x201B: "'",
    0x02BC: "'",
    0x00B4: "'",
    0x2032: "'",
    0x201C: '"',
    0x201D: '"',
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2212: "-",
}

_NORMALISE: dict[int, str | None] = {**dict.fromkeys(_INVISIBLE), **_LOOKALIKE}

#: What a wrapped line carries over from the line it continues: indentation, and the comment or
#: quote marker that reopens the block. Stripped before the two halves are joined, so a term
#: split across two lines of a `#` comment reads as one term again. A `-` is deliberately absent:
#: two adjacent markdown list items are unrelated sentences, and joining them would invent a hit.
_CONTINUATION_MARKER = re.compile(r"^\s*(?://+|[#*;>]+)?\s*")

#: The two ways a line break can be closed up. A wrap that fell on a space rejoins with a space
#: (`my` / `operator's`), and one that fell inside a word rejoins with nothing (`ai-` /
#: `toolkit`). Which one a rewrap chose is not knowable afterwards, so both are matched.
_JOINERS = (" ", "")

#: A break closes up to nothing only after one of these. A wrap that split a term mid-word broke
#: it after a separator the term itself contains (`ai-` / `toolkit`, `C:\\` / `Users\\`), never
#: after an ordinary letter. Closing up every break instead invents text nobody wrote: run
#: against this repo it welded a line ending in `None` onto the next line's `@pytest.mark...`
#: and reported the result as a leaked e-mail address. A guard that fails on fiction gets
#: switched off, so a break that is not a plausible mid-word split rejoins with a space.
_CLOSES_UP_AFTER = ("-", "/", "\\", "~")

#: How much of a candidate decode must be ordinary ASCII before it counts as text. A UTF-16 file
#: read with the wrong endianness decodes without error into CJK-range noise, so "it decoded" is
#: not evidence on its own. Genuine binaries fall below this too, which is what keeps them out of
#: the scan instead of turning them into meaningless hits.
_ASCII_SHARE_OF_TEXT = 0.5

#: The encodings tried when a file holds NUL bytes. UTF-16 is the one text encoding that puts NUL
#: bytes inside an ordinary English sentence, so a NUL means "wide text or a binary" and nothing
#: else. The bare `utf-16` entry consumes a byte-order mark; the explicit two cover a file written
#: without one.
_WIDE_ENCODINGS = ("utf-16", "utf-16-le", "utf-16-be")


def _normalised(text: str) -> str:
    """`text` as a reader sees it: invisible characters gone, typographic ones folded to ASCII."""
    return text.translate(_NORMALISE)


def _ascii_share(text: str) -> float:
    """How much of `text` is plain ASCII, between 0 and 1."""
    if not text:
        return 0.0
    return sum(character.isascii() for character in text) / len(text)


def _wide_text(raw: bytes) -> str | None:
    """`raw` read as UTF-16, or None when it is a binary rather than wide text.

    Every endianness is tried and the most ASCII-looking result wins, because decoding UTF-16 with
    the wrong byte order raises nothing at all: it returns confident nonsense.
    """
    candidates = []
    for encoding in _WIDE_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
        if "\x00" not in text:
            candidates.append(text)
    if not candidates:
        return None
    best = max(candidates, key=_ascii_share)
    return best if _ascii_share(best) >= _ASCII_SHARE_OF_TEXT else None


def _decoded(raw: bytes) -> str | None:
    """`raw` as scannable text, or None only when it is a genuine binary.

    A single byte that is not valid UTF-8 used to drop the whole file out of the scan, silently,
    which is one accidental paste away from switching this guard off for a file. So a file is
    read whatever it is encoded in: UTF-8 first, then UTF-16 when the NUL bytes say so, and
    otherwise a lossy read that keeps every ASCII character and replaces the rest. Every rule's
    terms are ASCII, so the lossy read still bites, and only the byte it choked on is lost.
    """
    try:
        text: str | None = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\x00" not in text:
        return text
    if b"\x00" in raw:
        return _wide_text(raw)
    return raw.decode("utf-8", errors="replace")


def _text_of(path: Path) -> str | None:
    """The file's scannable text, or None for a genuine binary or a file that is not there."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _decoded(raw)


@dataclass(frozen=True)
class _Projection:
    """A file's text rendered as one string, with the source line each character came from."""

    text: str
    line_of: tuple[int, ...]


def _projection(lines: list[str], joiner: str) -> _Projection:
    """Every line normalised and joined by `joiner`, with the line break closed up.

    Matching a line at a time is what let a rewrap hide a leak in this repo once already: the
    sweep moved half a term onto the next line, the regular expression stopped matching, and the
    line still read the same to a person. Joining the lines back up shows the rules what the
    reader sees. Each character keeps the line it came from, so a hit still names a real line.
    """
    parts: list[str] = []
    line_of: list[int] = []
    for number, raw in enumerate(lines, start=1):
        line = _normalised(raw)
        if parts:
            gap = joiner
            if not gap and not parts[-1].endswith(_CLOSES_UP_AFTER):
                gap = " "
            # `count=1` states the intent and changes nothing: the marker pattern is anchored
            # with `^` and this is one line, so it can match once whatever the count says.
            line = _CONTINUATION_MARKER.sub("", line, count=1)
            parts.append(gap)
            line_of.extend([number] * len(gap))
        parts.append(line)
        line_of.extend([number] * len(line))
    return _Projection("".join(parts), tuple(line_of))


@dataclass(frozen=True)
class Match:
    """One rule hit, written the way somebody fixing it needs to read it."""

    rel: str
    line: int | None
    matched: str
    context: str

    def __str__(self) -> str:
        where = f":{self.line}" if self.line is not None else ""
        return f"  {self.rel}{where}: {self.context}  <- matched {self.matched!r}"


def _matches_in(pattern: re.Pattern[str], path: Path) -> list[Match]:
    """Every place `pattern` hits in one tracked file: its name first, then its text."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    found: list[Match] = []
    # The repository-relative path, never the absolute one, which would name the checkout's own
    # home directory on every file in the tree.
    named = pattern.search(_normalised(rel))
    if named is not None:
        found.append(
            Match(rel=rel, line=None, matched=named.group(0), context="the file name itself")
        )
    text = _text_of(path)
    if text is None:
        return found
    lines = text.splitlines()
    seen: set[tuple[int, str]] = set()
    for joiner in _JOINERS:
        projection = _projection(lines, joiner)
        for match in pattern.finditer(projection.text):
            number = projection.line_of[match.start()]
            key = (number, match.group(0))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Match(
                    rel=rel,
                    line=number,
                    matched=match.group(0),
                    context=lines[number - 1].strip(),
                )
            )
    return found


def _matches(pattern: re.Pattern[str], paths: Iterable[Path]) -> list[Match]:
    """Every hit for `pattern` across `paths`, skipping only this file itself."""
    return [
        match for path in paths if path.resolve() != SELF for match in _matches_in(pattern, path)
    ]


def _hits(pattern: re.Pattern[str], paths: Iterable[Path]) -> list[str]:
    return [str(match) for match in _matches(pattern, paths)]


# --- which files the rules are shown -------------------------------------------------------------


class _GitCannotAnswer(Exception):
    """`git ls-files` could not be asked, or refused to answer. Never a reason to pass."""


#: The directories a mutation-testing tool copies this tree into before running the suite from
#: the copy: mutmut writes `mutants/`, poodle writes `.poodle-temp/run-N/` (and drops `.git`
#: while copying). Git tracks nothing there, correctly, and the suite must not fail there either:
#: a failing check aborts the mutation baseline, which does not lower the dogfood score so much
#: as delete it (see tests/test_mutation_baseline_inputs.py for how that goes wrong).
_MUTATION_COPY_DIRECTORIES = ("mutants", ".poodle-temp")

#: What makes an unpacked source distribution recognisable from the inside. The packaging spec
#: requires `PKG-INFO` at the root of every sdist and hatchling writes it; a git checkout never
#: has one, so it cannot be mistaken for one. The sdist ships `tests/` (see pyproject.toml's
#: sdist allowlist), so somebody running this suite from an unpacked tarball is an ordinary
#: thing to do, and there is genuinely nothing to gate there: that tree is already published.
_SDIST_MARKER = "PKG-INFO"


def _why_this_tree_tracks_nothing() -> str | None:
    """The named reason this directory legitimately has no tracked files, or None.

    "Git said nothing" is deliberately not one of the reasons. It used to be the only one, and it
    made this whole module pass in any environment without a working git: an exported tree, a
    container with no git on PATH, a directory whose `.git` was left behind by a copy. A guard
    that reports clean because it never ran is worse than no guard, because somebody trusts it.
    So the skip has to be able to say which known copy of the tree this is.
    """
    for part in REPO_ROOT.parts:
        for directory in _MUTATION_COPY_DIRECTORIES:
            if part == directory or part.startswith(f"{directory}-"):
                return f"{part!r} is a mutation-testing tool's copy of the tree"
    if (REPO_ROOT / _SDIST_MARKER).is_file():
        return f"this tree is an unpacked source distribution (it has a {_SDIST_MARKER})"
    return None


def _tracked_files() -> list[Path]:
    """Every file git tracks, as absolute paths.

    `git ls-files` rather than a walk: a new directory is in scope the moment it is committed,
    and there is no ignore list here to fall out of date. Git failing raises instead of returning
    nothing, because "no answer" and "no files" are different facts and only one of them is safe.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _GitCannotAnswer(str(exc)) from exc
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _scan_targets() -> list[Path]:
    """The tracked files to read, a skip where nothing is tracked for a named reason, or a
    failure everywhere else."""
    reason = _why_this_tree_tracks_nothing()
    try:
        files = _tracked_files()
    except _GitCannotAnswer as exc:
        if reason is not None:
            pytest.skip(f"{reason}, and git cannot answer here either ({exc})")
        pytest.fail(
            f"git could not list this tree's files ({exc}), so this check read nothing at all. "
            "It fails rather than passing: a scan that reports clean because it never ran is the "
            "one outcome this module must never produce. Run the suite from a git checkout."
        )
    if not files:
        if reason is not None:
            pytest.skip(f"{reason}, so git tracks nothing here and there is nothing to scan")
        pytest.fail(
            "git tracks no files under this directory, so this check read nothing at all and "
            "every rule below would have passed without seeing a byte. It fails rather than "
            "passing. Run the suite from a git checkout."
        )
    return files


# --- the checks ---------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_no_tracked_file_leaks_a_private_detail(rule: Rule) -> None:
    hits = _hits(rule.pattern, _scan_targets())
    assert not hits, (
        f"{len(hits)} tracked line(s) contain {rule.name}, which would be public the moment this "
        f"repo is:\n" + "\n".join(hits) + f"\n\nFix: {rule.fix}"
    )


def _is_a_documentation_address(address: str) -> bool:
    domain = address.rsplit("@", 1)[1].lower()
    return domain in ALLOWED_EMAIL_DOMAINS or domain.endswith(".invalid")


def test_no_tracked_file_carries_a_real_e_mail_address() -> None:
    hits = [
        str(match)
        for match in _matches(_EMAIL, _scan_targets())
        if not _is_a_documentation_address(match.matched)
    ]
    assert not hits, (
        "these tracked lines carry an e-mail address that is not a reserved documentation "
        "domain, so publishing this repo publishes the address:\n" + "\n".join(hits) + "\n\n"
        "Fix: use an `example.com` / `.invalid` address in fixtures and docs. A genuine contact "
        "address belongs in SECURITY.md's reporting link, not in prose."
    )


def _is_too_generic(name: str) -> bool:
    """Whether a git identity is too short or too common to search a whole tree for."""
    return len(name) < 4 or name.lower() in _GENERIC_GIT_NAMES


def _identity_pattern(name: str) -> re.Pattern[str]:
    """The pattern that finds `name` in a tracked file.

    Without regard to case, because the lower-cased spelling is how a name actually reaches a
    file: in a branch name, a URL, a slug or a path, never in the capitalised form `git config`
    hands back. The case-sensitive version of this pattern read `<name>-fixes-the-thing` as an
    innocent branch name and let it through.
    """
    return re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)


def test_no_tracked_file_repeats_the_local_git_identity() -> None:
    """The name on your commits is the one you are most likely to paste into a file by accident.

    Read off `git config` rather than written down here, for two reasons: this file would
    otherwise contain the very name it is trying to keep out of the tree, and a check derived
    from the local identity protects whoever is working in the clone rather than one person.
    """
    targets = _scan_targets()  # fail or skip early, before asking git for an identity
    name = subprocess.run(
        ["git", "config", "user.name"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if _is_too_generic(name):
        pytest.skip(f"local git identity {name!r} is absent or too generic to search for")

    hits = _hits(_identity_pattern(name), targets)
    assert not hits, (
        f"these tracked lines contain the name on your commits ({name!r}):\n"
        + "\n".join(hits)
        + "\n\nFix: write the role, not the person. 'The maintainer', 'a reviewer', "
        "'whoever cuts the release'."
    )


# --- the checks on the checks --------------------------------------------------------------------


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


#: The tracked files this scan cannot read as text. A binary holds no prose for a reader to leak,
#: so it is legitimately out of scope, but the list is written down rather than inferred: a file
#: that silently stops being readable then shows up as a failure here instead of as one fewer
#: file scanned, which is how a whole file used to fall out of this guard without saying so.
UNREADABLE_BY_DESIGN = (".github/assets/frank.png",)


def test_every_tracked_file_but_the_declared_binaries_is_actually_read() -> None:
    unread = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in _scan_targets()
        if path.is_file() and _text_of(path) is None
    )
    assert unread == sorted(UNREADABLE_BY_DESIGN), (
        "the set of tracked files this scan cannot read has changed. Text the rules never see is "
        "text they cannot fail on, so a new entry here is a hole in the guard, not a detail:\n"
        f"  unread now: {unread}\n  declared:   {sorted(UNREADABLE_BY_DESIGN)}\n\n"
        "Fix: if the new file is a genuine binary, add it to UNREADABLE_BY_DESIGN. If it is text, "
        "find out why `_decoded` gave up on it, because that is a scan the rules are not getting."
    )


# --- the red team ---------------------------------------------------------------------------------
#
# Eight payloads walked past this module in a red-team pass, one per defect below. None of them
# was an attack: every one is something an ordinary authoring accident produces, which is why
# they matter. The terms here are deliberately made up. Which names are forbidden is a separate
# question from whether a scan can see the text at all, and this file is published, so it does
# not grow the real list just to prove that a file name gets read.

#: A stand-in for a private name. Meaningless on purpose.
_FAKE = re.compile(r"\bWIDGETCORP\b")
#: A stand-in for a hyphenated private tool name, the shape a wrap splits mid-word.
_FAKE_HYPHENATED = re.compile(r"\bwidget-corp\b")
#: A stand-in for the possessive rule, the shape a wrap splits on a space.
_FAKE_POSSESSIVE = re.compile(r"\bmy\s+widget's\b")

_MODULE = sys.modules[__name__]

#: Sixteen bytes off the front of a real PNG: a signature, then a chunk length holding NUL bytes.
_PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway directory that the scan treats as the repository root."""
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    return tmp_path


def _plant(tree: Path, rel: str, content: bytes | str) -> Path:
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


# S1: the file name was never scanned at all.


def test_a_private_name_in_the_file_name_is_a_hit(tree: Path) -> None:
    planted = _plant(tree, "docs/WIDGETCORP-rollout.md", "nothing to see in here\n")
    assert _hits(_FAKE, [planted]) == [
        "  docs/WIDGETCORP-rollout.md: the file name itself  <- matched 'WIDGETCORP'"
    ]


def test_a_hit_reads_as_a_place_a_person_can_go_to(tree: Path) -> None:
    # The rendered line is the whole product of this module: the file, the line, what that line
    # says, and what matched in it. Every part of it is load-bearing, so it is pinned exactly.
    planted = _plant(tree, "docs/notes.md", "an innocent first line\na note about WIDGETCORP\n")
    assert _hits(_FAKE, [planted]) == [
        "  docs/notes.md:2: a note about WIDGETCORP  <- matched 'WIDGETCORP'"
    ]


def test_the_file_name_is_read_relative_to_the_repository_not_absolute(tree: Path) -> None:
    # The absolute path of any checkout runs through somebody's home directory, so scanning it
    # would report every file in the tree. Only the repository-relative path is shown to a rule.
    planted = _plant(tree, "docs/innocent.md", "nothing to see in here\n")
    outside_the_repo = re.compile(re.escape(tree.name))
    assert tree.name in str(planted)
    assert _matches(outside_the_repo, [planted]) == []


# S2: the self-exemption matched on the base name, so any file wearing that name was skipped.


def test_this_file_is_still_exempt_from_its_own_scan() -> None:
    spells_out_a_rule = re.compile(re.escape("a private tool or fleet name"))
    assert _matches(spells_out_a_rule, [Path(__file__)]) == []


def test_a_different_file_with_this_files_name_is_not_exempt(tree: Path) -> None:
    impostor = _plant(tree, "corpus/probe/test_public_readiness.py", "WIDGETCORP was here\n")
    assert impostor.name == SELF.name
    assert [match.line for match in _matches(_FAKE, [impostor])] == [1]


# S3: a file that was not UTF-8 was dropped whole, silently.


@pytest.mark.parametrize(
    "encoding",
    ["utf-16", "utf-16-le", "utf-16-be"],
    ids=["with a byte-order mark", "little-endian, no mark", "big-endian, no mark"],
)
def test_wide_text_is_read_whatever_its_byte_order(encoding: str) -> None:
    # Exactly the text, not merely a text containing it: a byte-order mark left on the front is
    # the sign that the decode guessed rather than read.
    assert _decoded("a note about WIDGETCORP\n".encode(encoding)) == "a note about WIDGETCORP\n"


def test_a_single_byte_that_is_not_utf_8_does_not_drop_the_rest_of_the_file() -> None:
    latin_1 = "caf\u00e9, and a note about WIDGETCORP\n".encode("latin-1")
    text = _decoded(latin_1)
    assert text is not None
    assert "WIDGETCORP" in text


def test_a_genuine_binary_is_not_turned_into_noise() -> None:
    assert _decoded(_PNG_HEAD) is None


def test_wide_text_that_is_exactly_half_ascii_is_still_text() -> None:
    # The share of ASCII is a floor, not a strict cut. Half is enough to be text.
    assert _decoded("ab\u00e9\u00e8".encode("utf-16-le")) == "ab\u00e9\u00e8"


def test_wide_bytes_that_are_mostly_not_ascii_are_a_binary() -> None:
    # Two ASCII characters out of five. A decode coming back without an error is not evidence
    # that a file is text: UTF-16 read with the wrong byte order succeeds too, and returns
    # confident nonsense.
    assert _decoded("ab\u00e9\u00e8\u00ea".encode("utf-16-le")) is None


def test_a_byte_order_only_the_last_encoding_can_read_is_still_read() -> None:
    # Big-endian text whose bytes are an unpaired surrogate when read the other way round: the
    # first two encodings tried raise, and the scan has to keep going rather than give up on the
    # file at the first refusal.
    assert _decoded("hello\u00d8world".encode("utf-16-be")) == "hello\u00d8world"


def test_a_wide_decode_that_still_holds_nul_characters_is_a_binary() -> None:
    # UTF-32 read two bytes at a time decodes without complaint and comes back full of NUL
    # characters. That is the sign the guess was wrong, not that the file is text.
    assert _decoded("hi".encode("utf-32-le")) is None


def test_the_ascii_share_of_nothing_is_nothing() -> None:
    # No characters is no evidence of text, not perfect evidence of it.
    assert _ascii_share("") == 0.0


def test_a_tracked_file_that_is_not_on_disk_is_not_read(tree: Path) -> None:
    # git lists a file that has been deleted but not staged. There is no content to scan, which
    # is the one kind of silence here that is legitimate.
    assert _text_of(tree / "deleted.md") is None


def test_a_file_that_cannot_be_opened_at_all_is_not_read(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = _plant(tree, "docs/locked.md", "a note about WIDGETCORP\n")

    def refuse(self: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert _text_of(planted) is None


def test_a_payload_in_a_wide_text_file_is_a_hit(tree: Path) -> None:
    planted = _plant(tree, "docs/exported.md", "a note about WIDGETCORP\n".encode("utf-16"))
    assert [match.line for match in _matches(_FAKE, [planted])] == [1]


# S4: a term split by a line wrap was invisible to a line-at-a-time scan.


def test_a_term_a_wrap_split_on_a_space_is_a_hit(tree: Path) -> None:
    planted = _plant(tree, "docs/wrapped.md", "a sentence that trails off with my\nwidget's own\n")
    assert [match.line for match in _matches(_FAKE_POSSESSIVE, [planted])] == [1]


def test_a_term_a_wrap_split_inside_a_word_is_a_hit(tree: Path) -> None:
    planted = _plant(tree, "docs/wrapped.md", "as documented in the widget-\ncorp handbook\n")
    assert [match.line for match in _matches(_FAKE_HYPHENATED, [planted])] == [1]


@pytest.mark.parametrize("marker", ["#", "//", "*", ";", ">", "   "])
def test_a_wrap_inside_a_marked_up_block_is_a_hit(tree: Path, marker: str) -> None:
    # The marker that reopens the block on the next line is part of the wrap, not part of the
    # sentence: a comment, a doc comment, a quoted reply, or plain indentation.
    body = f"{marker} a line ending with my\n{marker}   widget's name\n"
    planted = _plant(tree, "scripts/thing.py", body)
    assert [match.line for match in _matches(_FAKE_POSSESSIVE, [planted])] == [1]


def test_a_projection_remembers_the_line_every_character_came_from() -> None:
    # The index map is what turns a match back into a line number a person can open. Pinned
    # character by character, including the one the join itself puts in: it belongs to the line
    # it introduces, and losing it slides every later line number by one.
    loose = _projection(["ab", "cd"], " ")
    assert (loose.text, loose.line_of) == ("ab cd", (1, 1, 2, 2, 2))
    tight = _projection(["ab-", "cd"], "")
    assert (tight.text, tight.line_of) == ("ab-cd", (1, 1, 1, 2, 2))


@pytest.mark.parametrize("separator", ["-", "/", "\\", "~"])
def test_a_break_after_a_separator_closes_up_to_nothing(separator: str) -> None:
    # The characters a term itself contains, spelled out here rather than read off the constant,
    # so that dropping one from the constant fails this.
    closed_up = _projection([f"widget{separator}", "corp"], "").text
    assert f"widget{separator}corp" in closed_up


def test_a_break_after_an_ordinary_letter_does_not_weld_two_lines_together(tree: Path) -> None:
    # Closing up every break invents text nobody wrote. Run against this repo it welded a line
    # ending in `None` onto the next line's `@pytest.mark...` and called it a leaked address.
    planted = _plant(tree, "tests/thing.py", "assert thing() is WIDGET\nCORP is elsewhere\n")
    assert _matches(_FAKE, [planted]) == []


def test_a_hit_on_one_line_is_reported_once(tree: Path) -> None:
    # Two projections read every line, so a plain single-line hit is seen twice and must not be
    # reported twice.
    planted = _plant(tree, "docs/plain.md", "a note about WIDGETCORP\n")
    assert len(_matches(_FAKE, [planted])) == 1


def test_a_hit_only_the_closed_up_reading_finds_is_still_reported(tree: Path) -> None:
    # The plain hit on line 1 is found by both readings, so the second reading meets it again as
    # a duplicate. Meeting a duplicate must not end the reading: the wrapped hit comes after it.
    body = "the widget-corp handbook\n\nsee the widget-\ncorp guide\n"
    planted = _plant(tree, "docs/both.md", body)
    assert [match.line for match in _matches(_FAKE_HYPHENATED, [planted])] == [1, 3]


def test_two_different_hits_on_one_line_are_both_reported(tree: Path) -> None:
    # What is de-duplicated is a line and a matched text together, not a line on its own.
    pattern = re.compile(r"\bwidget-corp\b", re.IGNORECASE)
    planted = _plant(tree, "docs/twice.md", "WIDGET-CORP and widget-corp, one line\n")
    matched = [match.matched for match in _matches(pattern, [planted])]
    assert matched == ["WIDGET-CORP", "widget-corp"]


# S5 and S6: a character a reader cannot tell apart switched a rule off.


def test_an_invisible_character_inside_a_term_does_not_hide_it(tree: Path) -> None:
    planted = _plant(tree, "docs/pasted.md", "a note about WIDGET\u200bCORP\n")
    assert [match.line for match in _matches(_FAKE, [planted])] == [1]


def test_a_curly_apostrophe_does_not_switch_off_the_possessive_rule(tree: Path) -> None:
    planted = _plant(tree, "docs/pasted.md", "my widget\u2019s own name\n")
    assert [match.line for match in _matches(_FAKE_POSSESSIVE, [planted])] == [1]


def test_every_character_the_scan_folds_away() -> None:
    # Written out here rather than read off the tables, so dropping an entry from a table fails
    # this test instead of quietly narrowing what the rules can see. Escapes, not the characters
    # themselves: half of them are invisible, which is the whole reason they are in the table.
    assert _normalised("a\u00ad\u180e\u200b\u200c\u200d\u2060\ufeffb") == "ab"
    assert _normalised("\u2018\u2019\u201b\u02bc\u00b4\u2032") == "''''''"
    assert _normalised("\u201c\u201d") == '""'
    assert _normalised("\u2010\u2011\u2012\u2013\u2014\u2212") == "------"
    assert _normalised("plain ASCII is left alone") == "plain ASCII is left alone"


def test_a_typographic_hyphen_does_not_switch_off_a_hyphenated_name(tree: Path) -> None:
    planted = _plant(tree, "docs/pasted.md", "the widget\u2011corp handbook\n")
    assert [match.line for match in _matches(_FAKE_HYPHENATED, [planted])] == [1]


# S7: git failing was read as "nothing to scan", so the whole module passed without running.


def _rule_named(name: str) -> Rule:
    return next(rule for rule in RULES if rule.name == name)


def _outcome_of_scanning() -> tuple[str, str]:
    """What `_scan_targets` does here, as a word, plus what it said about it.

    Asserting on this rather than on a raised skip, because a skip raised inside a test is
    reported as a skip and not as a failure: a check written the obvious way would go quiet in
    exactly the situation it exists to make noisy, which is the defect it is testing for.
    """
    try:
        _scan_targets()
    except pytest.fail.Exception as failure:
        return "fail", str(failure)
    except pytest.skip.Exception as skipped:
        return "skip", str(skipped)
    return "scan", ""


def test_a_tree_git_cannot_answer_for_fails_rather_than_skipping(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> list[Path]:
        raise _GitCannotAnswer("not a git repository")

    monkeypatch.setattr(_MODULE, "_tracked_files", refuse)
    outcome, said = _outcome_of_scanning()
    assert outcome == "fail", f"an unexplained git failure came back as {outcome!r}: {said}"
    assert "never ran" in said
    # And it says what git actually complained about, which is the only clue to the fix.
    assert "not a git repository" in said


def test_git_refusing_is_reported_with_what_git_said(tree: Path) -> None:
    # A throwaway directory is not a checkout, so git refuses here for real rather than through a
    # stand-in. What it said has to travel with the refusal: it is the only clue to the fix, and
    # a failure that says only "git could not list this tree's files ()" sends nobody anywhere.
    with pytest.raises(_GitCannotAnswer) as refusal:
        _tracked_files()
    assert str(refusal.value) not in {"", "None"}


def test_an_ordinary_tree_is_scanned(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = _plant(tree, "docs/plain.md", "nothing to see in here\n")
    monkeypatch.setattr(_MODULE, "_tracked_files", lambda: [planted])
    assert _outcome_of_scanning() == ("scan", "")


def test_a_tree_that_tracks_nothing_fails_rather_than_skipping(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert outcome == "fail", f"an unexplained empty tree came back as {outcome!r}: {said}"
    assert "tracks no files" in said


def test_a_mutation_tools_copy_of_the_tree_still_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure here aborts the mutation baseline, which deletes the dogfood score rather than
    # lowering it, so this branch has to keep working.
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path / "mutants")
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert (outcome, "mutation-testing" in said) == ("skip", True), said


def test_a_mutation_tools_copy_skips_even_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> list[Path]:
        raise _GitCannotAnswer("git is not installed")

    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path / ".poodle-temp" / "run-1")
    monkeypatch.setattr(_MODULE, "_tracked_files", refuse)
    outcome, said = _outcome_of_scanning()
    assert (outcome, "mutation-testing" in said) == ("skip", True), said


def test_an_unpacked_source_distribution_skips(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The literal name the packaging spec requires, not the constant, so renaming the constant's
    # value to something an sdist does not contain fails this instead of passing it.
    _plant(tree, "PKG-INFO", "Metadata-Version: 2.4\n")
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert (outcome, "source distribution" in said) == ("skip", True), said


@pytest.mark.parametrize(
    "directory",
    ["mutants", ".poodle-temp", ".poodle-temp-manual"],
    ids=["mutmut", "poodle", "a poodle run kept for inspection"],
)
def test_the_known_copies_of_the_tree_are_named_positively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path / directory / "run-1")
    assert _why_this_tree_tracks_nothing() is not None


def test_an_ordinary_checkout_has_no_reason_to_track_nothing(tree: Path) -> None:
    # The whole point: "git said nothing" is not itself a reason, so this must come back empty
    # and turn an unexplained silence into a failure.
    assert _why_this_tree_tracks_nothing() is None


# S8, and the same class in the identity rule: a rule that only matched one capitalisation.


@pytest.mark.parametrize(
    ("line", "leaks"),
    [
        (r"the checkout is at C:\users\somebody\code", True),
        ("the checkout is at /users/somebody/code", True),
        ("the checkout is at ~/dev/a-project", True),
        ("the checkout is at /home/somebody/code", True),
        ("the workspace is at /home/runner/work/a-project", False),
    ],
    ids=["windows", "posix", "a shorthand home", "a linux home", "a CI runner, which is nobody's"],
)
def test_a_home_directory_is_a_hit_however_it_is_spelled(
    tree: Path, line: str, leaks: bool
) -> None:
    rule = _rule_named("a path inside somebody's home directory")
    planted = _plant(tree, "docs/setup.md", line + "\n")
    assert bool(_matches(rule.pattern, [planted])) is leaks


def test_a_name_is_a_hit_whatever_its_capitalisation(tree: Path) -> None:
    planted = _plant(tree, "docs/branches.md", "the branch was widgeteer-fixes-the-thing\n")
    assert [match.line for match in _matches(_identity_pattern("Widgeteer"), [planted])] == [1]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("", True), ("bot", True), ("Dependabot[bot]", True), ("Anna", False), ("Widgeteer", False)],
    ids=["absent", "too short", "a bot", "the shortest real name", "a person"],
)
def test_which_git_identities_are_worth_searching_for(name: str, expected: bool) -> None:
    assert _is_too_generic(name) is expected


@pytest.mark.parametrize(
    ("address", "publishable"),
    [
        ("someone@example.com", True),
        ("SOMEONE@EXAMPLE.COM", True),
        ("someone@a-fixture.invalid", True),
        ("someone@a-real-host.dev", False),
    ],
    ids=["a reserved domain", "shouted", "any .invalid host", "anything else"],
)
def test_which_addresses_are_safe_to_publish(address: str, publishable: bool) -> None:
    assert _is_a_documentation_address(address) is publishable
