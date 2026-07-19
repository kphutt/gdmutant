"""The mutation-fixer recipe (docs/mutation-fixer-recipe.md) must stay honest: its hard facts are
pinned against the code they describe, so a drift (e.g. the ignore marker changing) fails CI."""

import re
from pathlib import Path

from gdmutant.adapters.gdscript import _IGNORE_MARKER

_RECIPE = Path(__file__).resolve().parent.parent / "docs" / "mutation-fixer-recipe.md"


def _text() -> str:
    return _RECIPE.read_text(encoding="utf-8")


def test_recipe_exists_and_is_active() -> None:
    text = _text()
    assert text.startswith("---")
    assert "status: active" in text


def test_recipe_uses_the_real_ignore_marker() -> None:
    # The suppression step must quote the adapter's actual marker, or an agent copies a dud.
    assert _IGNORE_MARKER in _text()


def test_recipe_references_the_survived_status() -> None:
    # The loop keys off the exact Stryker status string the reporter emits for a survivor.
    assert '"status": "Survived"' in _text()


def test_recipe_local_links_resolve() -> None:
    # Every relative link must point at a real file (nothing else link-checks docs).
    for target in re.findall(r"\]\(([^)]+)\)", _text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        assert (_RECIPE.parent / target).resolve().exists(), f"broken link in recipe: {target}"


def test_recipe_is_agent_agnostic() -> None:
    # The recipe points at AGENTS.md, the agent-agnostic source. A root `CLAUDE.md` -> `@AGENTS.md`
    # bridge file may exist, but this doc must not route through a tool-specific file.
    assert "CLAUDE.md" not in _text()
