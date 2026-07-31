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
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Docs that must carry frontmatter: everything under `docs/`, plus the root and `.github/` guides.
LIVE_DOCS = sorted(REPO.glob("docs/**/*.md")) + [
    REPO / "AGENTS.md",
    REPO / "CONTRIBUTING.md",
    REPO / "CHANGELOG.md",
    REPO / ".github" / "SECURITY.md",
]

#: Docs that must NOT carry frontmatter — see the module docstring for why each is exempt.
RENDERED_ELSEWHERE = [
    REPO / "README.md",
    REPO / ".github" / "PULL_REQUEST_TEMPLATE.md",
]


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


@pytest.mark.parametrize("path", LIVE_DOCS, ids=lambda p: p.name)
def test_every_live_doc_declares_its_type_and_status(path: Path) -> None:
    fields = _frontmatter(path)
    assert fields, f"{path.relative_to(REPO)} has no YAML frontmatter"
    assert fields.get("type"), f"{path.relative_to(REPO)} declares no `type`"
    assert fields.get("status"), f"{path.relative_to(REPO)} declares no `status`"


@pytest.mark.parametrize("path", RENDERED_ELSEWHERE, ids=lambda p: p.name)
def test_the_docs_rendered_outside_github_carry_no_frontmatter(path: Path) -> None:
    # Adding frontmatter here does not produce a metadata table; it produces a visible heading on
    # the PyPI page / in every PR body. The module docstring has the detail.
    assert not path.read_text(encoding="utf-8").startswith("---"), (
        f"{path.relative_to(REPO)} is rendered outside GitHub's blob view, where frontmatter shows "
        "as a literal heading — see AGENTS.md's docs section"
    )
