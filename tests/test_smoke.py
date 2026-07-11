"""Bootstrap smoke tests — prove the package imports and the CLI entry point resolves.

Real engine tests (mutation operators, the GDScript adapter, the fixture corpus) land
in the v0.1 engine milestone. These keep CI meaningful while the tree is still scaffolding.
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
