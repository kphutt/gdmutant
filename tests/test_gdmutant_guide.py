"""The gdmutant guide (docs/gdmutant-guide.md) must not rot: its hard facts are pinned against the
code they describe, so changing a status string, the schema version, or the ignore marker without
updating the guide fails CI."""

import re
import subprocess
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import _IGNORE_MARKER
from gdmutant.engine.report import _STATUS, SCHEMA_VERSION

_REPO = Path(__file__).resolve().parent.parent
_GUIDE = _REPO / "docs" / "gdmutant-guide.md"


def _text() -> str:
    return _GUIDE.read_text(encoding="utf-8")


def test_guide_exists_and_is_active() -> None:
    # Built docs come only from `status: active` frontmatter (see AGENTS.md).
    text = _text()
    assert text.startswith("---")
    assert "status: active" in text


def test_guide_documents_the_schema_version() -> None:
    # The JSON example must show the real schemaVersion the reporter emits.
    assert f'"schemaVersion": "{SCHEMA_VERSION}"' in _text()


def test_guide_lists_every_mutant_status() -> None:
    # All four MutantStatus strings the reporter can emit must be documented, so an agent knows
    # every value it might see. A renamed/added status without a guide update fails here.
    text = _text()
    for status in _STATUS.values():
        assert status in text


def test_guide_documents_the_ignore_marker() -> None:
    # The equivalent-mutant suppression marker must match the adapter's real marker exactly.
    assert _IGNORE_MARKER in _text()


def test_guide_states_the_full_exit_code_contract() -> None:
    # The 0/1/2 contract is the load-bearing promise for a scripting agent — spell out all three.
    # Each must head its own entry in the exit-code list: a bare `0` also appears in the worked
    # example, so matching the list marker is what pins the contract rather than a passing mention.
    text = _text()
    for code in ("- `0`:", "- `1`:", "- `2`:"):
        assert code in text


def test_guide_local_links_resolve() -> None:
    # Every relative markdown link in the guide must point at a file that exists — nothing else in
    # CI link-checks docs, so a broken cross-reference (a link to a file that isn't there) would
    # otherwise ship silently.
    for target in re.findall(r"\]\(([^)]+)\)", _text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        target = target.split("#", 1)[0]  # drop any in-page anchor
        if not target:
            continue
        assert (_GUIDE.parent / target).resolve().exists(), (
            f"broken link in gdmutant-guide.md: {target}"
        )


def test_guide_references_agents_md_not_claude_md() -> None:
    # AGENTS.md is the single, agent-agnostic source of conventions, and the guide points there.
    # A bare `CLAUDE.md` -> `@AGENTS.md` bridge file is allowed (it just lets Claude Code, which
    # reads CLAUDE.md, load AGENTS.md) — but the guide itself must not duplicate or route through
    # a tool-specific file, so no `CLAUDE.md` reference belongs in the guide's own text.
    text = _text()
    assert "AGENTS.md" in text
    assert "CLAUDE.md" not in text


def test_no_tracked_agent_tool_config() -> None:
    # Agent-agnostic: no tool-specific config directory should be committed. Enforced in-repo (a CI
    # guard, not a .gitignore entry) so the "stays out" guarantee travels with the repo rather than
    # depending on a machine-global gitignore — and without adding a pro-tooling ignore line. A
    # future `git add -A` that re-tracks such a directory then fails CI.
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--", ".claude/", ".cursor/", ".aider*"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git absent
        pytest.skip("git not available")
    if listed.returncode != 0:  # pragma: no cover - not a git work tree (e.g. an sdist)
        pytest.skip("not a git work tree")
    assert listed.stdout == "", f"tool-specific config must not be tracked:\n{listed.stdout}"
