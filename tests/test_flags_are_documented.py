"""Every flag `gdmutant run` accepts must be written down somewhere a user can read.

A flag that only exists in `--help` is a flag nobody finds. Two shipped ones — `--report
step-summary` (the reporter the GitHub Action's job summary is built on) and `--no-require-clean`
— reached a release named in no document at all, so this pins the whole set instead of those two:
add an option to the `run` subcommand and this fails until the docs describe it.

The two homes are the README (what the tool is, for a human skimming the project page) and the
AI-agent guide (how to drive it from a script). Either counts; the point is that *some* prose
explains the flag, not which file it lives in.
"""

import argparse
from pathlib import Path

from gdmutant.cli import build_parser

_REPO = Path(__file__).resolve().parent.parent
_DOCS = (_REPO / "README.md", _REPO / "docs" / "using-with-an-ai-agent.md")


def _run_subparser() -> argparse.ArgumentParser:
    """The `run` subcommand's parser. argparse exposes subparsers only through the
    `_SubParsersAction` it registers, so reach through that rather than rebuilding the flag list
    here — a hand-maintained copy would be the very drift this test exists to catch."""
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["run"]
    raise AssertionError("the CLI no longer registers subcommands")  # pragma: no cover


def _long_options() -> list[str]:
    return [
        option
        for action in _run_subparser()._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    ]


def test_every_run_flag_appears_in_the_docs() -> None:
    prose = "\n".join(path.read_text(encoding="utf-8") for path in _DOCS)
    undocumented = [option for option in _long_options() if option not in prose]
    assert not undocumented, (
        f"these `gdmutant run` flags are documented nowhere a user reads: {', '.join(undocumented)}"
    )


def test_the_report_kinds_are_documented() -> None:
    # `--report` takes a value, so naming the flag alone is not enough — each choice needs its own
    # explanation, and `step-summary` is the one the GitHub Action depends on.
    prose = "\n".join(path.read_text(encoding="utf-8") for path in _DOCS)
    for action in _run_subparser()._actions:
        if "--report" in action.option_strings and action.choices:
            for kind in action.choices:
                assert f"--report {kind}" in prose, f"--report {kind} is undocumented"
