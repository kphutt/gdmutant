"""Smoke tests — the package imports and the CLI entry point resolves.

The engine's behavior is covered by the dedicated modules (test_spans, test_operators, test_mutants,
test_loop, test_report, test_gdscript_adapter, test_gdunit_runner, test_cli, test_end_to_end).
"""

import pytest

import gdmutant
from gdmutant.cli import build_parser, main


def test_version_is_exposed() -> None:
    assert isinstance(gdmutant.__version__, str)
    assert gdmutant.__version__


def test_cli_runs_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "gdmutant" in out


def test_cli_version_flag_exits_zero() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
