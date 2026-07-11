"""The agent guide (docs/agent-guide.md) must not rot: its hard facts are pinned against the code
they describe, so changing a status string, the schema version, or the ignore marker without
updating the guide fails CI."""

from pathlib import Path

from gdmutant.adapters.gdscript import _IGNORE_MARKER
from gdmutant.engine.report import _STATUS, SCHEMA_VERSION

_GUIDE = Path(__file__).resolve().parent.parent / "docs" / "agent-guide.md"


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
    text = _text()
    for code in ("**`0`**", "**`1`**", "**`2`**"):
        assert code in text
