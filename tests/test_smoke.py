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


def test_cli_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"gdmutant {gdmutant.__version__}"


def test_version_falls_back_when_package_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running from a source tree with no installed metadata must yield the "0.0.0" fallback rather
    # than raising PackageNotFoundError at import.
    import importlib
    import importlib.metadata

    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    try:
        assert importlib.reload(gdmutant).__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(gdmutant)  # restore the real __version__ for the rest of the session
