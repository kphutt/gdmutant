"""Every live doc opens with YAML frontmatter — and the two files that must not.

`AGENTS.md` states the convention: a live doc opens with `type` / `status` / `created`, and a build
takes only `status: active`. Nothing enforced it, so `AGENTS.md` itself went without.

The exemptions are the more interesting half. Frontmatter is a GitHub blob-view feature: rendered
there it becomes a small metadata table, but rendered anywhere else it is ordinary Markdown — a
thematic break followed by a setext heading, i.e. a large visible `type: … status: … created: …`
title. Two of this repo's Markdown files are rendered somewhere else:

* `README.md` is `[project].readme`, so PyPI renders it with `readme_renderer`, which has no
  frontmatter support. It would head the package's public page.
* `.github/PULL_REQUEST_TEMPLATE.md` is pasted verbatim into the body of every new pull request, and
  a PR body is rendered by the plain GFM pipeline — again no frontmatter support. It would head
  every PR. (The template's own instructions use HTML comments precisely because those *are*
  invisible once rendered.)

So this test pins both directions, and `AGENTS.md` records the same carve-out in prose.

It also pins that the lists cover every Markdown file in the repo. Each check here runs over a
hand-maintained list, so a doc is watched only if someone remembered to name it, and
`CODE_OF_CONDUCT.md` showed what that costs: it arrived with live frontmatter, reached neither
list, and a bogus `type:` value in it passed this whole module. A file now leaves this module's
sight only by being named in `NOT_A_DOC` on purpose.

`type` and `status` were checked only for presence, not value, so a typo silently produces a new
one-off category instead of an error. That is exactly how one ADR sat with `status: active` in its
frontmatter while its own body said it had been superseded: nothing compared the field against a
known vocabulary. `ALLOWED_TYPES` and `ALLOWED_STATUSES` below are read off the values this repo's
docs actually use today, not invented, so extending either vocabulary is a one-line change made at
the same time as the doc that first needs the new value.

This is a closed-vocabulary check, not a build-inclusion check: it cannot tell a *correct* `status`
from a *wrong but valid-looking* one (a doc marked `active` that should say `superseded` still
passes), only a value outside the known set. That narrower claim is what it actually pins.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Every Markdown file in the repo, which lives in exactly three places: the root, `.github/` and
#: `docs/`. `test_every_markdown_file_is_classified` pins that each one is named in exactly one of
#: the three lists below, so a doc cannot be invisible to this module the way `CODE_OF_CONDUCT.md`
#: was for the commit between it landing and this check arriving.
ALL_MARKDOWN = sorted(
    set(REPO.glob("*.md")) | set(REPO.glob(".github/*.md")) | set(REPO.glob("docs/**/*.md"))
)

#: Docs that must carry frontmatter: everything under `docs/`, plus the root and `.github/` guides.
LIVE_DOCS = sorted(REPO.glob("docs/**/*.md")) + [
    REPO / "AGENTS.md",
    REPO / "CONTRIBUTING.md",
    REPO / "CHANGELOG.md",
    REPO / "CODE_OF_CONDUCT.md",
    REPO / ".github" / "SECURITY.md",
]

#: Docs that must NOT carry frontmatter — see the module docstring for why each is exempt.
RENDERED_ELSEWHERE = [
    REPO / "README.md",
    REPO / ".github" / "PULL_REQUEST_TEMPLATE.md",
]

#: Markdown files that are not documents at all, so the convention simply does not reach them.
#: `CLAUDE.md` is a one-line `@AGENTS.md` import pointer read by a coding agent, never rendered and
#: never read as prose. It is listed rather than ignored so the completeness check below stays
#: exhaustive: a file leaves this module's sight only by someone naming it here on purpose.
NOT_A_DOC = [
    REPO / "CLAUDE.md",
]

#: The `type` values in use across the live docs today: the Diataxis quartet (how-to, reference,
#: explanation) plus the two kinds those don't cover, a changelog-style `record` and an ADR
#: `decision`. Add a value here only once a real doc needs it.
ALLOWED_TYPES = {"how-to", "reference", "explanation", "decision", "record"}

#: The `status` values in use today. `active` builds. `superseded` marks a decision a later one
#: replaced (see AGENTS.md's "build only from `status: active`").
ALLOWED_STATUSES = {"active", "superseded"}


def _frontmatter(path: Path) -> dict[str, str]:
    """The opening `---` block parsed into a dict, or `{}` when the file has none."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    fields = {}
    for line in text[4:end].splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_the_live_docs_list_is_not_silently_empty() -> None:
    # A glob that stops matching would turn the check below into a vacuous pass.
    assert len(LIVE_DOCS) > 15


def test_every_markdown_file_is_classified() -> None:
    # Every check in this module runs over a hand-maintained list, so a new doc is covered only if
    # someone remembers to add it. `CODE_OF_CONDUCT.md` landed with live frontmatter and reached
    # neither list, which meant a bogus `type:` value in it passed the whole module untouched. This
    # closes the class rather than that one instance: an unclassified file fails here and names
    # itself, so the next doc cannot arrive unwatched.
    classified = set(LIVE_DOCS) | set(RENDERED_ELSEWHERE) | set(NOT_A_DOC)
    unclassified = sorted(p.relative_to(REPO).as_posix() for p in set(ALL_MARKDOWN) - classified)
    assert not unclassified, (
        f"{unclassified} is Markdown in this repo that no list in this module names, so no "
        "frontmatter check runs against it. Add it to LIVE_DOCS if it is a live doc, to "
        "RENDERED_ELSEWHERE if it is rendered where frontmatter shows as a heading, or to "
        "NOT_A_DOC if it is not prose at all."
    )


@pytest.mark.parametrize("path", LIVE_DOCS, ids=lambda p: p.name)
def test_every_live_doc_declares_its_type_and_status(path: Path) -> None:
    fields = _frontmatter(path)
    assert fields, f"{path.relative_to(REPO)} has no YAML frontmatter"
    assert fields.get("type"), f"{path.relative_to(REPO)} declares no `type`"
    assert fields.get("status"), f"{path.relative_to(REPO)} declares no `status`"


@pytest.mark.parametrize("path", LIVE_DOCS, ids=lambda p: p.name)
def test_every_live_doc_uses_a_known_type_and_status(path: Path) -> None:
    fields = _frontmatter(path)
    doc_type, status = fields.get("type"), fields.get("status")
    assert doc_type in ALLOWED_TYPES, (
        f"{path.relative_to(REPO)} declares `type: {doc_type}`, outside the known vocabulary "
        f"{sorted(ALLOWED_TYPES)}. A typo here silently invents a new one-off category instead "
        "of failing."
    )
    assert status in ALLOWED_STATUSES, (
        f"{path.relative_to(REPO)} declares `status: {status}`, outside the known vocabulary "
        f"{sorted(ALLOWED_STATUSES)}. A typo here would silently drop the doc from the build with "
        "no error."
    )


def test_the_vocabularies_are_not_silently_empty() -> None:
    # A vocabulary that collapsed to the empty set would turn the check above into "no value is
    # ever allowed": loud, but for the wrong reason. Pin both floors explicitly.
    assert ALLOWED_TYPES
    assert ALLOWED_STATUSES


@pytest.mark.parametrize("path", RENDERED_ELSEWHERE, ids=lambda p: p.name)
def test_the_docs_rendered_outside_github_carry_no_frontmatter(path: Path) -> None:
    # Adding frontmatter here does not produce a metadata table; it produces a visible heading on
    # the PyPI page / in every PR body. The module docstring has the detail.
    assert not path.read_text(encoding="utf-8").startswith("---"), (
        f"{path.relative_to(REPO)} is rendered outside GitHub's blob view, where frontmatter shows "
        "as a literal heading — see AGENTS.md's docs section"
    )
