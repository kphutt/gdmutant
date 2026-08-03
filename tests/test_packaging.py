"""Packaging-hygiene guards.

These assert the distribution *metadata* stays correct without building an artifact: the PEP 561
`py.typed` marker ships, the strict-typing signal is declared, the sdist ships from an explicit
allowlist so no local, untracked, or dot-prefixed state (agent/editor dirs, CI config) can leak
into a release, and the README's relative image paths are rewritten to absolute URLs before they
reach PyPI (GitHub resolves relative paths; PyPI does not, so a miss here is invisible on the repo
front page and only shows up as a broken image after an irreversible upload).
"""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_FANCY_README = _PYPROJECT["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]


def _built_long_description() -> str:
    """README.md with the configured substitutions applied — what PyPI actually receives.

    This mirrors what hatch-fancy-pypi-readme does at build time (assemble the fragments, then run
    each substitution in order, then expand `$HFPR_VERSION`). It is a re-implementation, so it
    proves the *pattern matches the README*, not that the plugin works — the plugin is exercised
    for real by `twine check` on the built artifact in publish.yml.
    """
    text = "".join(
        (_ROOT / fragment["path"]).read_text(encoding="utf-8")
        for fragment in _FANCY_README["fragments"]
    )
    for substitution in _FANCY_README.get("substitutions", []):
        text = re.sub(substitution["pattern"], substitution["replacement"], text)
    return text.replace("$HFPR_VERSION", _PYPROJECT["project"]["version"])


def test_py_typed_marker_ships() -> None:
    # PEP 561: downstream type-checkers only honour our types if this marker is in the package.
    assert (_ROOT / "gdmutant" / "py.typed").is_file()


def test_typed_classifier_declared() -> None:
    # The marker and the `Typing :: Typed` classifier must agree — one without the other misleads.
    assert "Typing :: Typed" in _PYPROJECT["project"]["classifiers"]


def test_sdist_ships_from_an_allowlist_that_omits_local_and_ci_state() -> None:
    # The sdist ships an explicit allowlist, so nothing untracked or dot-prefixed can leak into a
    # release. Assert the load-bearing paths are shipped and that no dot-dir (agent/editor state) or
    # CI/dev tooling directory is in the list — an allowlist naming none of them is the robust,
    # tool-agnostic fix for hatchling otherwise bundling local cruft.
    include = _PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    for shipped in ("/gdmutant", "/tests", "/docs", "/pyproject.toml"):
        assert shipped in include, f"{shipped} must ship in the sdist"
    for entry in include:
        base = entry.lstrip("/")
        assert not base.startswith("."), f"no dot-prefixed state should be shipped: {entry}"
        assert base not in ("scripts", "github"), f"CI/dev tooling must not ship: {entry}"


def test_readme_is_dynamic_so_substitutions_can_run() -> None:
    # A static `readme = "README.md"` would bypass the hook entirely and ship the relative image
    # path straight to PyPI. The two must stay in sync: dynamic readme + the plugin in the build
    # requirements, or neither.
    assert "readme" in _PYPROJECT["project"]["dynamic"]
    assert "readme" not in _PYPROJECT["project"], "a static readme= would silence the hook"
    requires = _PYPROJECT["build-system"]["requires"]
    assert any(r.startswith("hatch-fancy-pypi-readme") for r in requires), requires


def test_no_relative_image_survives_into_the_pypi_long_description() -> None:
    # THE GUARD THAT MATTERS. PyPI's renderer does not resolve relative paths — it hands them to a
    # camo proxy that 404s — so any `src="` left pointing at a repo-relative file renders as a
    # broken image on the project page. GitHub resolves those paths fine, so this failure is
    # invisible until after an irreversible PyPI upload. Assert it at test time instead.
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert 'src=".github/assets/' in readme, (
        "README.md no longer has a relative asset src the substitution was written for — "
        "update the pattern in pyproject.toml's fancy-pypi-readme hook to match reality"
    )
    assert 'src=".github/assets/' not in _built_long_description()


def test_pypi_image_urls_are_absolute_and_tag_pinned() -> None:
    # Every image the long description references must be an absolute https URL, and the repo's own
    # assets must be pinned to the `vX.Y.Z` tag that check_release_tag.py forces to match the
    # packaged version — so a release's page keeps showing the banner that release shipped with.
    built = _built_long_description()
    version = _PYPROJECT["project"]["version"]
    for src in re.findall(r'src="([^"]+)"', built):
        assert src.startswith("https://"), f"non-absolute image src reaches PyPI: {src}"
    assert (
        f"https://raw.githubusercontent.com/kphutt/gdmutant/v{version}/.github/assets/" in built
    ), "the banner URL is not pinned to this version's release tag"


def test_no_relative_markdown_link_survives_into_the_pypi_long_description() -> None:
    # THE GUARD THAT MATTERS, for links: PyPI resolves neither an image src nor a markdown link
    # relative to the repo, so `[survivor reference](docs/survivors/README.md)` renders as a 404 on
    # the project page, invisible until after an irreversible upload. README.md is expected to keep
    # its links relative (so they still work for someone reading a fork) — the substitution is what
    # makes the *published* copy absolute instead.
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    relative_targets = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", readme)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert relative_targets, (
        "README.md has no relative markdown link left for this test to exercise — if that's "
        "deliberate, this test (and the substitution it guards) can be deleted"
    )
    built = _built_long_description()
    for target in relative_targets:
        assert f"]({target})" not in built, f"a relative link reaches PyPI unrewritten: {target}"


def test_pypi_markdown_links_are_absolute_and_repo_pinned() -> None:
    # Every markdown link in the built text must be absolute (or a pure in-page anchor) — no
    # allowlist of "known" external links here, since that would need updating every time README.md
    # gains a new one, exactly the kind of drift this test exists to catch instead of cause.
    # Separately, each relative link README.md actually has must land on the exact absolute GitHub
    # URL expected: the failure mode for a regex mistake here is not an exception, it's a silently
    # broken href on the one page a reader can't ask to be fixed.
    built = _built_long_description()
    for target in re.findall(r"\]\(([^)]+)\)", built):
        assert target.startswith(("http://", "https://", "#", "mailto:")), (
            f"a markdown link in the PyPI long description is still relative: {target}"
        )
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    relative_targets = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", readme)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]
    for target in relative_targets:
        assert f"](https://github.com/kphutt/gdmutant/blob/main/{target})" in built, (
            f"{target} was not rewritten to the expected absolute GitHub URL"
        )
