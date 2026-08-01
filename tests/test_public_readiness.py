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

There are two halves here, and they answer to different rules.

**The shape rules name nothing, so they run everywhere, always.** Each matches a *construction*
that cannot mean the domain term, never the bare word:

- A possessive whose determiner points at a person: a demonstrative or a first-person pronoun in
  front of `operator's`, rather than the definite or indefinite article every mutation-operator
  sentence in this repo uses.
- A home directory rather than a path: a tilde home, a Windows user directory, a `/home/<name>/`.
- An e-mail address outside the reserved test domains, which is how a personal address arrives.
- The name on this clone's own commits, read out of `git config` rather than written down here.

**The vocabulary rule needs a word list, and a file that ships must not hold one.** The private
names are real ones: an internal tool, a codename, a repository nobody outside can look up.
Spelling them out here publishes the very list the rule exists to protect, and it did. An earlier
version held four of them in source and had to exempt itself from its own scan to stay green,
which then had to be argued about, narrowed, and defended by a test of its own. That exemption is
gone. Nothing tracked in this repository names a single private term any more: the list lives
outside the tree, in a file this machine points at through the `GDMUTANT_PRIVATE_TERMS`
environment variable.

That leaves the rule with three states, and it says out loud which one it is in, because a check
that can be mistaken for having run is the hazard this whole module is about:

- **The variable names a readable file.** The rule runs, over every tracked file, like the rest.
- **The variable names a file that is missing, unreadable, or holds no terms.** A hard failure.
  A machine that declared a list and cannot produce it is broken, not unconfigured, and quietly
  narrowing the scan is precisely what must never happen here.
- **The variable is unset.** The rule does not run, and every run of this suite says so: in the
  header pytest prints before the first test, in the skip reason, and in a warning. A fresh clone
  and a CI runner are both this case legitimately. Neither has the file, and neither can be given
  it, because the file is private. What they must not do is *look* checked.

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
- *This file is scanned like every other one.* There is no exemption left, for any path, under
  any spelling. The two places where a shape rule would otherwise match its own example — a home
  directory and an address on a real host — are written here in pieces and assembled by
  `_spelled`, so the example still reaches the rule at run time without the tree carrying the
  text. That is the whole reason the terms had to leave: an exemption is a hole with a name, and
  the way to close it is to stop needing one.

This is a hard failure, not advice. It is also not a proof: judgement calls like a stale status
paragraph, a test count, or a roadmap-progress note are equally unwelcome in a file a stranger
reads, and no regular expression finds those. Those stay a review job. What lives here is the
class that a reader provably cannot catch by reading.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _spelled(*parts: str) -> str:
    """`parts` joined, with nothing added.

    A shape rule matches its own examples: the text that says what a home directory looks like
    *is* a home directory. This file is no longer exempt from its own scan, so the two examples
    that would match are written as pieces and put together here, at run time. The rule still sees
    the whole string; the tree only ever carries the halves. `+` and a call, never two adjacent
    string literals, because a formatter is allowed to fold those back into one.
    """
    return "".join(parts)


@dataclass(frozen=True)
class Rule:
    """One forbidden construction, its compiled pattern, and what to do about a hit."""

    name: str
    pattern: re.Pattern[str]
    fix: str


#: The home directories, assembled rather than written. Spelled out, each of these would be a home
#: path sitting in a tracked file, and the rule below would report this very module. This file is
#: no longer exempt from its own scan, so every example that would match is built at run time out
#: of pieces that do not. See `_spelled`.
_TILDE_HOME = _spelled("~", "/dev/")
_WINDOWS_HOME = _spelled("C:", "\\", "users", "\\")
_MAC_HOME = _spelled("/", "users", "/")
_LINUX_HOME = _spelled("/", "home", "/")

RULES: tuple[Rule, ...] = (
    Rule(
        name="a possessive that can only mean a person",
        # A demonstrative or a first-person pronoun in front of `operator's` never describes a
        # mutation operator: every such sentence in this repo uses the definite or the indefinite
        # article instead. The second half catches the nouns that give the person away whatever
        # the determiner is.
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
        name="a path inside somebody's home directory",
        # Case-insensitive, because Windows paths are: the user directory spelled in lower case
        # names exactly the same place as the capitalised one and is exactly as private, and the
        # case-sensitive version of this rule waved it through.
        #
        # The one exemption is CI's own home directory, and it is held down twice over, because
        # ignoring case widens what a negative lookahead *excuses* just as much as it widens what
        # the rule catches. Ending that lookahead at a word boundary excused every account that
        # merely *starts* with the runner's six letters, since `-` and `.` are word boundaries
        # too, so a person whose account begins that way had a home directory that went
        # unreported. The exemption therefore ends at a `/`, which makes it a whole path segment,
        # and `(?-i:...)` holds it to lower case: a Linux home is case-sensitive, so the
        # capitalised spelling there is not the CI account, it is somebody with that name. All ten
        # spellings are pinned, assembled at run time, in the parametrised test named for this
        # rule at the foot of this module.
        pattern=re.compile(
            _TILDE_HOME
            + r"|[A-Za-z]:\\Users\\"
            + r"|/Users/[A-Za-z0-9._-]+/"
            + r"|/home/(?!(?-i:runner)/)[A-Za-z0-9._-]+/",
            re.IGNORECASE,
        ),
        fix=(
            "use a repository-relative path, or a placeholder like `<path-to-a-checkout>`. "
            "An absolute home path is both a private detail and wrong on every other machine."
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


# --- the private vocabulary, which is not kept here ----------------------------------------------

#: The environment variable a machine uses to declare that it holds a private vocabulary, and
#: where. The variable is the declaration: setting it is a deliberate act, taken by whatever
#: provisions the machine, and it is the only thing that puts the vocabulary rule into service.
#:
#: Not a default path, deliberately. A default would let an *empty* location mean "configured but
#: clean", which is the shape of every bug this module has ever had: silence read as a pass.
VOCABULARY_ENV = "GDMUTANT_PRIVATE_TERMS"

#: How the file is written, quoted in the failure so a reader never has to find the format
#: elsewhere. One term per line; `#` opens a comment; a blank line is nothing.
VOCABULARY_FORMAT = (
    "one term per line. A line starting with '#' is a comment and a blank line is ignored. "
    "A plain line is matched literally, ignoring case, and ignoring which separator joins its "
    "parts, so one entry covers the hyphenated, spaced, underscored and run-together spellings. "
    "A line starting with 're:' is a regular expression instead, for the entries that need a "
    "shape (an id scheme, a collocation, a spelling that must stay case-sensitive)."
)

#: The prefix that turns a line into a regular expression rather than a literal.
_PATTERN_PREFIX = "re:"

#: What may sit between the parts of a literal term. One entry then covers every spelling a
#: hyphenated private name arrives in: hyphenated, spaced, underscored, dotted, run together.
#: Zero-or-more, so a single-word term is unaffected.
_TERM_SEPARATOR = r"[\s\-_.]*"

#: The boundary a literal term is held to. Not `\b`: a term may begin or end with a character
#: `\b` does not treat as a word character, and a hyphen must stay *allowed* on both sides, so
#: that a term still matches inside a longer hyphenated name.
_TERM_EDGE_BEFORE = r"(?<![A-Za-z0-9])"
_TERM_EDGE_AFTER = r"(?![A-Za-z0-9])"


class _VocabularyUnreadable(Exception):
    """This machine declared a vocabulary and could not produce one. Never a reason to narrow."""


@dataclass(frozen=True)
class Vocabulary:
    """The private terms this machine declared, compiled, plus where they came from."""

    source: str
    terms: tuple[str, ...]
    pattern: re.Pattern[str]


def _term_pattern(term: str) -> str:
    """One written term as a regular-expression fragment.

    A `re:` line is handed through untouched, so an entry that needs a shape can have one, and an
    entry that must stay case-sensitive can say so inline with `(?-i:...)`. Everything else is a
    literal, escaped, with its parts allowed to be joined by any of the separators a private name
    gets typed with.
    """
    if term.startswith(_PATTERN_PREFIX):
        return term[len(_PATTERN_PREFIX) :].strip()
    parts = [re.escape(part) for part in re.split(r"[\s\-_.]+", term) if part]
    return _TERM_EDGE_BEFORE + _TERM_SEPARATOR.join(parts) + _TERM_EDGE_AFTER


def _read_vocabulary(path: Path) -> Vocabulary:
    """The vocabulary at `path`, or `_VocabularyUnreadable` naming what went wrong.

    Every way this can fail raises. A file that is missing, unreadable, or holds no terms at all
    is a broken machine, not an unconfigured one, and the difference matters: an unconfigured
    machine says so and stops, whereas a broken one that was allowed to continue would run a scan
    with nothing in it and report the tree clean.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _VocabularyUnreadable(str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise _VocabularyUnreadable(f"it is not UTF-8 text ({exc})") from exc
    terms = tuple(
        stripped
        for line in raw.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )
    if not terms:
        raise _VocabularyUnreadable("it holds no terms at all")
    try:
        pattern = re.compile("|".join(_term_pattern(term) for term in terms), re.IGNORECASE)
    except re.error as exc:
        raise _VocabularyUnreadable(f"one of its terms is not a valid expression ({exc})") from exc
    return Vocabulary(source=str(path), terms=terms, pattern=pattern)


def _declared_vocabulary_path() -> Path | None:
    """The file this machine declares, or None when it declares none.

    A variable set to nothing but whitespace counts as unset. It is what an exported-but-empty
    variable looks like, and reading it as a declaration would turn the loudest state into the
    quietest one: a declared machine that fails, rather than an undeclared one that says so.
    """
    raw = os.environ.get(VOCABULARY_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


#: What the three states are called. The word travels into the header line, the skip reason, the
#: warning and the tests, so that one place decides what a run is allowed to claim.
LOADED, UNREADABLE, UNDECLARED = "loaded", "unreadable", "undeclared"


def _looked_up() -> tuple[str, str, Vocabulary | None]:
    """Which of the three states this machine is in, the sentence that says so, and the terms.

    One lookup feeds the header line `conftest.py` prints, the skip reason, the warning, and the
    scan itself, so the announcement cannot drift away from what actually happened.
    """
    path = _declared_vocabulary_path()
    if path is None:
        return (
            UNDECLARED,
            (
                f"private vocabulary NOT CONFIGURED: {VOCABULARY_ENV} is unset, so the "
                "vocabulary rule DID NOT RUN and this run is not a full public-readiness "
                "check. The shape rules (possessive, home path, e-mail domain, git identity) "
                "did run. A CI runner and a fresh clone are both this case legitimately, "
                "because the word list is private and cannot ship with the repository. To run "
                f"the other half, set {VOCABULARY_ENV} to a file outside this repository "
                "holding " + VOCABULARY_FORMAT
            ),
            None,
        )
    try:
        vocabulary = _read_vocabulary(path)
    except _VocabularyUnreadable as exc:
        return (
            UNREADABLE,
            (
                f"private vocabulary BROKEN: {VOCABULARY_ENV} names {str(path)!r}, and {exc}. "
                "This machine declared a word list and cannot produce it, which is a hard failure "
                "rather than a narrower scan: a declared-but-missing list would leave the "
                "vocabulary rule passing over every tracked file without a term in it. Repair the "
                f"file or unset {VOCABULARY_ENV}. The file holds " + VOCABULARY_FORMAT
            ),
            None,
        )
    return (
        LOADED,
        # Names the variable as well as the file, like the other two states do. Without it the one
        # state a configured machine actually sees was the only one that did not say what to change
        # to get a different answer, and `test_the_run_says_out_loud_which_half_of_the_guard_ran`
        # could not ask the same question of all three.
        f"private vocabulary loaded: {len(vocabulary.terms)} terms from {vocabulary.source!r} "
        f"(named by {VOCABULARY_ENV})",
        vocabulary,
    )


def vocabulary_state() -> tuple[str, str]:
    """Which state this machine is in, and the sentence that says so. Read by `conftest.py`."""
    state, said, _ = _looked_up()
    return state, said


def _vocabulary() -> Vocabulary:
    """The loaded vocabulary, or the right kind of noise instead.

    Never returns an empty vocabulary, and never a narrower one than the machine asked for. The
    two ways out are a failure and an announced skip, and which one a machine gets turns on
    whether it declared a list, never on whether one happened to be there.
    """
    state, said, vocabulary = _looked_up()
    if vocabulary is None:
        if state == UNREADABLE:
            pytest.fail(said)
        warnings.warn(said, stacklevel=2)
        pytest.skip(said)
    return vocabulary


# --- what the rules are shown ------------------------------------------------------------------

#: Characters a reader cannot see at all. They arrive by pasting out of a browser or a rendered
#: document, and a single one of them inside a term defeats every rule in this file while the
#: line still reads exactly as it did. Dropped before matching, never reported as a difference.
_INVISIBLE = (0x00AD, 0x180E, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)

#: Characters a reader reads as ASCII punctuation. A word processor, a browser paste or a
#: smart-quotes autocorrect substitutes these silently, and the possessive rule, which is the
#: reason this module exists, is switched off by exactly one of them: the curly apostrophe. The
#: dashes are here for the same reason a hyphenated private name is: `widget-corp` typed through
#: an autocorrect is not spelled with the hyphen the rule looks for. The example is the made-up
#: name the red team below uses, not a real one, because this comment is itself scanned.
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
#:
#: This is an exclusion, so it is worth saying plainly what it excludes: a *wide file written
#: mostly in a non-Latin script* falls under the floor and is not scanned, even though it is text
#: and could carry a leak. Using "is it English" as the test for "is it a binary" is the wrong
#: proxy, and the honest fix is to sort text from binary by control characters rather than by
#: alphabet. That is left alone here on purpose: this floor is also the endianness tie-break above,
#: and changing both at once is how a scanner starts reporting noise. The limit is not silent,
#: which is what makes it tolerable. Such a file comes back unread, and
#: `test_every_tracked_file_but_the_declared_binaries_is_actually_read` then fails and names it
#: (checked, with a UTF-16 Japanese file holding a Windows home path). So it announces itself the
#: first time it matters instead of quietly narrowing the scan.
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
    """Every hit for `pattern` across `paths`. No path is skipped, this module's own included.

    There used to be an exemption here, because this module spelled out the private names it
    forbade and so could not pass its own scan. It was the one hole in the guard with a name on
    it, and it was reached for real: it compared base names, so every file in the tree called
    what this one is called went unscanned too. Moving the names out of the tree is what made the
    exemption unnecessary, and the exemption is gone rather than merely made exact, because an
    exact hole is still a hole.
    """
    return [match for path in paths for match in _matches_in(pattern, path)]


def _hits(pattern: re.Pattern[str], paths: Iterable[Path]) -> list[str]:
    return [str(match) for match in _matches(pattern, paths)]


# --- which files the rules are shown -------------------------------------------------------------


class _GitCannotAnswer(Exception):
    """`git ls-files` could not be asked, or refused to answer. Never a reason to pass."""


#: Where a mutation-testing tool puts its copy of this tree. Git tracks nothing there, correctly,
#: and the suite must not fail there either: a failing check aborts the mutation baseline, which
#: does not lower the dogfood score so much as delete it (see tests/test_mutation_baseline_inputs
#: .py for how that goes wrong).
#:
#: These name a *layout*, not a word to look for anywhere in the path. mutmut copies into
#: `<repo>/mutants/`, so the copy's own root is the directory called `mutants`. poodle copies into
#: `<repo>/.poodle-temp/run-N/` (`work_folder / ("run-" + run_id)` in its own source), so the
#: copy's root is `run-N` *and* its parent is the temp directory. Both halves are required,
#: because either on its own is a word that an ordinary checkout could sit under. Searching every
#: path component, which is what this did first, excused any checkout living below a folder of
#: that name: the same silent pass S7 was about, reached through a different door.
_MUTMUT_COPY = "mutants"
_POODLE_COPY = ".poodle-temp"
_POODLE_RUN = "run-"

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
    if REPO_ROOT.name == _MUTMUT_COPY:
        return f"this tree is mutmut's copy of the repository, a {_MUTMUT_COPY!r} directory"
    if REPO_ROOT.name.startswith(_POODLE_RUN) and REPO_ROOT.parent.name.startswith(_POODLE_COPY):
        return f"this tree is a poodle run inside {REPO_ROOT.parent.name!r}"
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


def test_no_tracked_file_names_a_private_term() -> None:
    """The half that needs a word list, and therefore needs this machine to have declared one.

    Fails on a machine that declared a list it cannot produce. Skips, loudly, on one that declared
    none. Never narrows: there is no path through here that scans a tree with an empty vocabulary
    and calls the result clean.
    """
    targets = _scan_targets()  # fail or skip on the tree first, before asking about the word list
    vocabulary = _vocabulary()
    hits = _hits(vocabulary.pattern, targets)
    assert not hits, (
        f"{len(hits)} tracked line(s) name something out of the private vocabulary "
        f"({len(vocabulary.terms)} terms, from {vocabulary.source!r}), which would be public the "
        "moment this repo is:\n" + "\n".join(hits) + "\n\n"
        "Fix: name the effect, not the thing. 'a review pass found', 'another project of mine', "
        "'the tracker'. A reader outside cannot look any of these up, so at best they read as "
        "noise, and at worst they hand out the shape of a workspace nobody was shown."
    )


def test_the_private_vocabulary_is_not_kept_inside_this_repository() -> None:
    """A word list that ships is a word list that leaked, so the guard refuses to read one.

    Three things keep the file out of this tree, and this is the one that runs even when the file
    is somewhere silly. The second is `.gitignore`, pinned below, which runs on every machine
    whether or not one is configured. The third is the rule above: a committed word list is a
    tracked file in which every term matches its own line, so committing it turns the guard red
    once per term rather than quietly making it a no-op.
    """
    path = _declared_vocabulary_path()
    if path is None:
        pytest.skip(f"{VOCABULARY_ENV} is unset, so there is no declared file to place")
    assert not path.resolve().is_relative_to(REPO_ROOT), (
        f"{VOCABULARY_ENV} names {str(path)!r}, which is inside this repository at "
        f"{str(REPO_ROOT)!r}. The private vocabulary must live outside the tree: anything in here "
        "is one `git add` away from being published, and publishing the list of words a project "
        "must never publish is the worst single outcome available.\n\n"
        "Fix: keep the file in whatever already syncs private configuration between your "
        f"machines, and point {VOCABULARY_ENV} at it there."
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
    # And this module. It was always in the list and then dropped again on the way to the rules,
    # which is the difference an exemption makes and the reason it is checked here now.
    assert Path(__file__).resolve().relative_to(REPO_ROOT).as_posix() in found


#: A string that exists exactly once in this module: on this line, as this constant's value. It is
#: the probe for "the scan really does read this file". It has to be a constant rather than a
#: literal written inside the test, because the scan reads this module's *source*: a probe spelled
#: out at the point of use appears twice over, once as the constant and once as the search for it,
#: and the test then measures its own text rather than the scan. Both self-scan tests were written
#: that way first and both counted two hits.
_SELF_SCAN_SENTINEL = "public-readiness self-scan sentinel, 4f2a9c, deliberately unique"


def _lines_holding(needle: str) -> list[int]:
    """Which lines of this module hold `needle`, read off the file rather than written down."""
    source = Path(__file__).resolve().read_text(encoding="utf-8").splitlines()
    return [number for number, line in enumerate(source, start=1) if needle in line]


def test_this_module_is_scanned_like_every_other_file() -> None:
    """The exemption is gone, so the rules read this file too, and this proves they reach it.

    The inverse of the check that used to sit here. That one asserted this module was skipped;
    this one asserts it is not, by finding a string that only exists in it. A reintroduced
    exemption, under any spelling, fails here: the scan stops seeing this file and the hit goes.
    """
    here = Path(__file__).resolve()
    expected = _lines_holding(_SELF_SCAN_SENTINEL)
    assert len(expected) == 1, f"the sentinel must appear exactly once, found {expected}"
    found = _matches(re.compile(re.escape(_SELF_SCAN_SENTINEL)), [here])
    assert [match.rel for match in found] == [here.relative_to(REPO_ROOT).as_posix()]
    assert [match.line for match in found] == expected


def test_the_run_says_out_loud_which_half_of_the_guard_ran() -> None:
    """Whatever state the vocabulary is in, the run announces it, and the announcement is true.

    `conftest.py` prints this sentence as a pytest header line before the first test, so it is on
    screen for every run on every machine, CI included. The point is not the sentence: it is that
    a green run on a machine with no word list cannot be read as a fully checked one.
    """
    state, said = vocabulary_state()
    assert state in {LOADED, UNREADABLE, UNDECLARED}
    assert VOCABULARY_ENV in said, said
    # It says which half ran in words a reader does not have to decode, not just a state name.
    expected = {LOADED: "loaded", UNREADABLE: "BROKEN", UNDECLARED: "DID NOT RUN"}[state]
    assert expected in said, said


# --- the vocabulary machinery ---------------------------------------------------------------------
#
# The half that was moved out of source. Everything here is about one property: the rule either
# runs over a real word list, or says out loud that it did not. There is no third way through, and
# in particular no way to scan a tree with an empty vocabulary and call the result clean — that is
# the shape of every defect this module has ever had.


def _declared(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Put this machine into the "declares a word list at `value`" state, or into the unset one."""
    if value is None:
        monkeypatch.delenv(VOCABULARY_ENV, raising=False)
    else:
        monkeypatch.setenv(VOCABULARY_ENV, value)


def _outcome_of_asking_for_the_vocabulary() -> tuple[str, str]:
    """What `_vocabulary()` does here, as a word, plus what it said.

    The same trick `_outcome_of_scanning` uses, and for the same reason: a skip raised inside a
    test is reported as a skip rather than a failure, so asserting on the raise directly would make
    a check go quiet in exactly the case it exists to make noisy.
    """
    try:
        _vocabulary()
    except pytest.fail.Exception as failure:
        return "fail", str(failure)
    except pytest.skip.Exception as skipped:
        return "skip", str(skipped)
    return "loaded", ""


@pytest.mark.parametrize(
    ("value", "declares"),
    [(None, False), ("", False), ("   ", False), ("\t\n", False), ("a-list.txt", True)],
    ids=["unset", "empty", "spaces", "whitespace", "a path"],
)
def test_what_counts_as_declaring_a_word_list(
    monkeypatch: pytest.MonkeyPatch, value: str | None, declares: bool
) -> None:
    # An exported-but-empty variable is what a half-written shell profile leaves behind. Reading it
    # as a declaration would turn the loudest state (a declared machine that fails) into the
    # quietest one, which is backwards.
    _declared(monkeypatch, value)
    assert (_declared_vocabulary_path() is not None) is declares


def test_a_declared_path_is_expanded_from_the_home_shorthand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The word list lives in whatever syncs private configuration between machines, which is
    # naturally written relative to home. An unexpanded tilde would be a path no machine has.
    _declared(monkeypatch, _spelled("~", "/") + "a-list.txt")
    path = _declared_vocabulary_path()
    assert path is not None
    assert "~" not in str(path)
    assert path.is_absolute()


def test_a_machine_that_declares_nothing_skips_loudly_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh clone and a CI runner. Neither has the list, and neither can be given it."""
    _declared(monkeypatch, None)
    with pytest.warns(UserWarning, match="DID NOT RUN"):
        outcome, said = _outcome_of_asking_for_the_vocabulary()
    assert outcome == "skip", f"an undeclared machine came back as {outcome!r}: {said}"
    # The skip has to say the rule did not run, name the variable that would make it run, and be
    # honest that the other half did. A bare "skipped" reads as "nothing to do here".
    assert "DID NOT RUN" in said
    assert VOCABULARY_ENV in said
    assert "shape rules" in said


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        (None, "the file is not there at all"),
        ("", "it holds no terms at all"),
        ("# only a comment\n\n   \n", "it holds no terms at all"),
        ("re:[unclosed\n", "not a valid expression"),
    ],
    ids=["missing", "empty", "comments and blanks only", "a broken expression"],
)
def test_a_machine_that_declares_a_word_list_it_cannot_produce_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, complaint: str
) -> None:
    """The single most important behaviour here, and the one the old design got wrong.

    Every one of these is a machine that *said* it had a word list. Skipping would leave the
    vocabulary rule passing over every tracked file without a term in it, which is a guard
    reporting clean because it read nothing — the exact hazard this module exists to prevent. So
    each is a hard failure that names what went wrong.
    """
    path = tmp_path / "a-list.txt"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    _declared(monkeypatch, str(path))

    outcome, said = _outcome_of_asking_for_the_vocabulary()
    assert outcome == "fail", f"a broken word list came back as {outcome!r} instead of failing"
    assert "BROKEN" in said
    # It names the file, or nobody can go and fix it. The file *name* rather than the whole path:
    # the message quotes the path with `!r`, which on Windows doubles every backslash, so the raw
    # string is not a substring of its own error message.
    assert path.name in said
    assert VOCABULARY_ENV in said
    if complaint != "the file is not there at all":
        assert complaint in said


def test_a_word_list_that_is_not_text_fails_rather_than_being_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A word list saved in the wrong encoding is a broken machine, not an unconfigured one.
    path = tmp_path / "a-list.txt"
    path.write_bytes("widgetcorp\n".encode("utf-16"))
    _declared(monkeypatch, str(path))
    outcome, said = _outcome_of_asking_for_the_vocabulary()
    assert (outcome, "not UTF-8" in said) == ("fail", True), said


def test_a_readable_word_list_loads_and_says_how_many_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a-list.txt"
    path.write_text("# a comment\n\nwidgetcorp\nwidget-corp\n", encoding="utf-8")
    _declared(monkeypatch, str(path))

    state, said = vocabulary_state()
    assert state == LOADED
    assert "2 terms" in said, said
    assert _outcome_of_asking_for_the_vocabulary() == ("loaded", "")


@pytest.mark.parametrize(
    "spelling",
    ["widget-corp", "widget corp", "widget_corp", "widget.corp", "widgetcorp", "WIDGET-CORP"],
    ids=["hyphenated", "spaced", "underscored", "dotted", "run together", "shouted"],
)
def test_one_written_term_covers_every_way_the_name_gets_typed(tree: Path, spelling: str) -> None:
    # A private name arrives hyphenated in one file, spaced in a sentence and run together in an
    # identifier. Writing one entry per spelling is how a list rots, so the entry covers them all.
    pattern = re.compile(_term_pattern("widget-corp"), re.IGNORECASE)
    planted = _plant(tree, "docs/notes.md", f"a note about {spelling} here\n")
    assert [match.line for match in _matches(pattern, [planted])] == [1]


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("widgetcorp", True),
        ("a note about widgetcorp here", True),
        # A term still matches inside a longer hyphenated name: that is how these get buried.
        ("pre-widgetcorp-suffix", True),
        # But not inside an unrelated longer word, which would be a false positive.
        ("awidgetcorp", False),
        ("widgetcorps", False),
        # The separator flexibility comes from the *written* term's own parts. A term written as
        # one word has no parts to rejoin, so it does not match a hyphenated spelling. Whoever
        # writes the list has to write `widget-corp` to cover `widget corp` and `widgetcorp`; this
        # is pinned because it is the one thing about the format that surprises.
        ("the widget-corp handbook", False),
    ],
    ids=[
        "alone",
        "in a sentence",
        "inside a hyphenated name",
        "glued in front",
        "glued behind",
        "a spelling a one-word term cannot reach",
    ],
)
def test_where_a_literal_terms_edges_are(text: str, hits: bool) -> None:
    pattern = re.compile(_term_pattern("widgetcorp"), re.IGNORECASE)
    assert bool(pattern.search(text)) is hits


def test_a_term_written_as_an_expression_is_used_as_one() -> None:
    # The `re:` prefix is what lets an id scheme or a collocation into the list without inventing a
    # second file format. A literal entry would have escaped these into uselessness.
    pattern = re.compile(_term_pattern(r"re:\bWDG-\d+\b"), re.IGNORECASE)
    assert pattern.search("tracked as WDG-411 on the board")
    assert not pattern.search("tracked as WDG- on the board")


def test_an_expression_term_can_insist_on_its_own_capitalisation() -> None:
    # The list is compiled case-insensitively, because that is right for almost every private name.
    # The exception is a name that is also an ordinary lowercase word, where only the capitalised
    # spelling is the private one. `(?-i:...)` is how such an entry says so, inside a list that is
    # otherwise case-blind.
    pattern = re.compile(_term_pattern(r"re:(?-i:\bWidget\b)"), re.IGNORECASE)
    assert pattern.search("filed in Widget last week")
    assert not pattern.search("a sub-widget of the other one")


def test_a_loaded_vocabulary_finds_its_terms_in_a_tracked_file(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end, through the public entry point rather than the helpers: a real file, a real list.
    listing = tmp_path / "a-list.txt"
    listing.write_text("widgetcorp\n", encoding="utf-8")
    _declared(monkeypatch, str(listing))
    planted = _plant(tree, "docs/notes.md", "an innocent line\na note about WidgetCorp\n")
    assert [match.line for match in _matches(_vocabulary().pattern, [planted])] == [2]


def test_a_vocabulary_term_split_by_a_line_wrap_is_still_a_hit(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rewrap failure, applied to the half that moved out of source. A word list is no use if a
    # reflowed paragraph hides its terms.
    listing = tmp_path / "a-list.txt"
    listing.write_text("widget-corp\n", encoding="utf-8")
    _declared(monkeypatch, str(listing))
    planted = _plant(tree, "docs/wrapped.md", "as set out in the widget-\ncorp handbook\n")
    found = _matches(_vocabulary().pattern, [planted])
    # A set, not a list: a separator-flexible term matches in *both* readings of the wrap (as
    # `widget- corp` when the break rejoins with a space, and as `widget-corp` when it closes up),
    # and the two matched texts differ, so the de-duplication by (line, matched text) keeps both.
    # The reported line is what matters, and both readings agree on it.
    assert {match.line for match in found} == {1}
    assert found, "the wrapped term has to be found at all"


def test_the_word_list_is_refused_when_it_sits_inside_this_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard against the worst single outcome available: committing the list of words the repo
    # must never publish. `.gitignore` is the other half, and is checked separately.
    inside = REPO_ROOT / "docs" / "private-terms.txt"
    _declared(monkeypatch, str(inside))
    with pytest.raises(AssertionError, match="must live outside the tree"):
        test_the_private_vocabulary_is_not_kept_inside_this_repository()


def test_the_run_header_announces_the_state_before_the_first_test() -> None:
    """The announcement pytest prints, not the sentence the guard composes.

    This is the difference between "the module can describe its state" and "every run on every
    machine shows it". `conftest.py` is what makes CI logs carry the line, and deleting that hook
    would leave the three-state contract true but invisible, which is the same as absent.
    """
    from tests.conftest import pytest_report_header

    header = pytest_report_header()
    state, said = vocabulary_state()
    assert state in header
    assert said in header


def test_git_ignores_a_private_word_list_dropped_into_this_tree() -> None:
    """The half of "keep it out of the repository" that works with no configuration at all.

    Asked of git rather than read out of `.gitignore`, because a pattern in that file that git
    does not actually apply to the path in question would read as protection and be none.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "private-terms.txt", "docs/my-private-terms.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.stdout.split() == ["private-terms.txt", "docs/my-private-terms.txt"], (
        "git does not ignore a private word list placed in this tree, so one could be committed:\n"
        f"  git check-ignore said: {ignored.stdout!r} {ignored.stderr!r}\n\n"
        "Fix: restore the `*private-terms*` line in .gitignore. The word list must never be a "
        "tracked file: publishing the list of words this repo may not publish is the worst "
        "single outcome available here."
    )


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
#
# The exemption is gone rather than made exact, so both halves of S2 are closed at once: this
# module is scanned, and so is anything else that happens to share its name. These two tests are
# what a reintroduced exemption fails on, whichever of the two spellings it is written in.


def test_a_different_file_wearing_this_modules_name_is_not_exempt(tree: Path) -> None:
    # The exact shape S2 was: the old exemption compared base names, so every file in the tree
    # called what this one is called went unscanned, wherever it sat.
    impostor = _plant(tree, "corpus/probe/" + Path(__file__).name, "WIDGETCORP was here\n")
    assert impostor.name == Path(__file__).name
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
    # It names which copy it thinks this is, so a skip can be argued with rather than trusted.
    assert (outcome, "mutmut" in said) == ("skip", True), said


def test_a_mutation_tools_copy_skips_even_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> list[Path]:
        raise _GitCannotAnswer("git is not installed")

    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path / ".poodle-temp" / "run-1")
    monkeypatch.setattr(_MODULE, "_tracked_files", refuse)
    outcome, said = _outcome_of_scanning()
    assert (outcome, ".poodle-temp" in said) == ("skip", True), said


def test_an_unpacked_source_distribution_skips(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The literal name the packaging spec requires, not the constant, so renaming the constant's
    # value to something an sdist does not contain fails this instead of passing it.
    _plant(tree, "PKG-INFO", "Metadata-Version: 2.4\n")
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert (outcome, "source distribution" in said) == ("skip", True), said


@pytest.mark.parametrize(
    ("where", "excused"),
    [
        ("mutants", True),
        (".poodle-temp/run-1", True),
        (".poodle-temp-manual/run-2", True),
        # A layout, not a word to find somewhere in the path. Each of these is an ordinary
        # checkout that happens to sit under a folder with a familiar name, and excusing it
        # would put back the silent pass this whole module exists to stop.
        ("mutants/gdmutant", False),
        (".poodle-temp/run-1/gdmutant", False),
        ("dev/mutants/checkouts/gdmutant", False),
        (".poodle-temp-manual/gdmutant", False),
        (".poodle-temp/gdmutant", False),
    ],
    ids=[
        "mutmut's copy",
        "a poodle run",
        "a poodle run kept for inspection",
        "a checkout under a folder called mutants",
        "a checkout under a poodle run",
        "a checkout further under one",
        "a checkout parked in a poodle temp folder",
        "the same, without the run- directory",
    ],
)
def test_only_a_real_mutation_copy_is_excused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, excused: bool
) -> None:
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path.joinpath(*where.split("/")))
    assert (_why_this_tree_tracks_nothing() is not None) is excused


def test_an_ordinary_checkout_has_no_reason_to_track_nothing(tree: Path) -> None:
    # The whole point: "git said nothing" is not itself a reason, so this must come back empty
    # and turn an unexplained silence into a failure.
    assert _why_this_tree_tracks_nothing() is None


# S8, and the same class in the identity rule: a rule that only matched one capitalisation.


@pytest.mark.parametrize(
    ("tail", "leaks"),
    [
        (_WINDOWS_HOME + "somebody", True),
        (_MAC_HOME + "somebody/code", True),
        (_TILDE_HOME + "a-project", True),
        (_LINUX_HOME + "somebody/code", True),
        (_LINUX_HOME + "runner/work/a-project", False),
        # The exemption is one directory, not a prefix. A person whose account merely begins with
        # those six letters has a home directory like anybody else, and `\b` used to excuse it.
        (_LINUX_HOME + "runner-fixes-the-thing/code", True),
        (_LINUX_HOME + "Runner-fixes-the-thing/code", True),
        (_LINUX_HOME + "runner.smith/code", True),
        (_LINUX_HOME + "runnerbot/code", True),
        # `/home/` is a Linux path, and Linux tells these apart. This one is not CI.
        (_LINUX_HOME + "Runner/work/a-project", True),
    ],
    ids=[
        "windows",
        "posix",
        "a shorthand home",
        "a linux home",
        "a CI runner, which is nobody's",
        "a person whose name starts with the runner's",
        "the same person, capitalised",
        "the same, dotted",
        "the same, with no separator at all",
        "somebody actually called Runner",
    ],
)
def test_a_home_directory_is_a_hit_however_it_is_spelled(
    tree: Path, tail: str, leaks: bool
) -> None:
    # The paths are assembled by `_spelled` rather than written literally, because this module is
    # scanned like every other file: a real home path in this list would make the rule report the
    # test that proves it works. The rule still sees the whole path, because the pieces are joined
    # before it is shown anything.
    rule = _rule_named("a path inside somebody's home directory")
    planted = _plant(tree, "docs/setup.md", "the checkout is at " + tail + "\n")
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


#: An address on a host that is not a reserved documentation domain. Assembled, because written
#: out it would be exactly what `test_no_tracked_file_carries_a_real_e_mail_address` forbids, in a
#: file that rule now reads. See `_spelled`.
_ADDRESS_ON_A_REAL_HOST = _spelled("someone", "@", "a-real-host", ".dev")


@pytest.mark.parametrize(
    ("address", "publishable"),
    [
        ("someone@example.com", True),
        ("SOMEONE@EXAMPLE.COM", True),
        ("someone@a-fixture.invalid", True),
        (_ADDRESS_ON_A_REAL_HOST, False),
    ],
    ids=["a reserved domain", "shouted", "any .invalid host", "anything else"],
)
def test_which_addresses_are_safe_to_publish(address: str, publishable: bool) -> None:
    assert _is_a_documentation_address(address) is publishable


def test_an_address_on_a_real_host_is_a_hit_in_a_tracked_file(tree: Path) -> None:
    # The rule end to end, not just the domain predicate: the pattern has to find the address in
    # a file before the domain test ever gets a say.
    planted = _plant(tree, "docs/contact.md", "write to " + _ADDRESS_ON_A_REAL_HOST + " for help\n")
    found = [m.matched for m in _matches(_EMAIL, [planted])]
    assert found == [_ADDRESS_ON_A_REAL_HOST]
    assert not _is_a_documentation_address(found[0])
