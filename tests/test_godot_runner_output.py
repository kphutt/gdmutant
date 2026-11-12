"""The GdUnit4 and GUT runners keep a bounded part of a run's output, as the command runner does.

Each test stands a Python script in for Godot: it floods its output and writes the report."""

import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner, GutRunner

#: Far more than a runner may hold: holding all of it shows as a peak many times the bound.
_FLOOD_MB = 16
_PEAK_BOUND = 4 * 1024 * 1024
_XML = '<testsuites><testsuite tests="2" failures="0" errors="0"/></testsuites>'


def _script(report: str | None, *, before: str = "", after: str = "") -> str:
    """Floods both streams with `before` printed first and `after` last, then writes `report`."""
    write = (
        "import os\n"
        f"os.makedirs(os.path.dirname({report!r}), exist_ok=True)\n"
        f"open({report!r}, 'w', encoding='utf-8').write({_XML!r})\n"
        if report
        else ""
    )
    return (
        "import sys\n"
        f"sys.stdout.write({before!r})\n"
        "sys.stdout.flush()\n"  # or the text layer holds `before` back until after the flood
        f"for _ in range({_FLOOD_MB}):\n"
        "    sys.stdout.buffer.write(b'stdout flood line\\n' * 58255)\n"
        "    sys.stderr.buffer.write(b'stderr flood line\\n' * 58255)\n"
        "sys.stdout.flush()\n"
        f"sys.stderr.write({after!r})\n" + write
    )


def _stand_in(monkeypatch: pytest.MonkeyPatch, runner: Any, script: str) -> None:
    monkeypatch.setattr(runner, "_imported", True)  # no import warm-up: there is no Godot
    monkeypatch.setattr(runner, "command", lambda *a, **k: [sys.executable, "-c", script])


def _peak(run: Any) -> tuple[Any, int]:
    tracemalloc.start()
    try:
        return run(), tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


_RUNNERS = [
    pytest.param(GdUnit4Runner, runner_mod.DEFAULT_REPORT_PATH, id="gdunit4"),
    pytest.param(GutRunner, runner_mod.DEFAULT_GUT_REPORT_PATH, id="gut"),
]


@pytest.mark.parametrize(("make", "report"), _RUNNERS)
def test_a_flooded_run_costs_a_bounded_amount_of_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any, report: str
) -> None:
    runner = make()
    _stand_in(monkeypatch, runner, _script(report))
    result, peak = _peak(lambda: runner.run(str(tmp_path)))
    assert (result.tests, result.failures, result.errors) == (2, 0, 0)
    assert peak < _PEAK_BOUND, f"peak {peak} bytes for {2 * _FLOOD_MB} MB of output"


@pytest.mark.parametrize(("make", "report"), _RUNNERS)
def test_a_flooded_marker_run_still_finds_its_script_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any, report: str
) -> None:
    runner = make()
    error = "SCRIPT ERROR: Invalid call\n   at: res://player.gd:42\n"
    _stand_in(monkeypatch, runner, _script(report, after=error))
    result, peak = _peak(lambda: runner.run_markers(str(tmp_path)))
    assert result.runtime_error.startswith("SCRIPT ERROR: Invalid call\n   at: res://player.gd:42")
    assert peak < _PEAK_BOUND, f"peak {peak} bytes for {2 * _FLOOD_MB} MB of output"


@pytest.mark.parametrize(("make", "report"), _RUNNERS)
def test_a_flooded_run_with_no_report_names_the_end_of_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any, report: str
) -> None:
    runner = make()
    _stand_in(monkeypatch, runner, _script(None, after="the real reason\n"))
    with pytest.raises(RuntimeError, match=r"stderr flood line\nthe real reason$"):
        runner.run(str(tmp_path))


def test_gdunit4s_no_tests_line_is_found_anywhere_in_a_flooded_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Printed first, so it is long gone from the tail by the time the run ends.
    runner = GdUnit4Runner()
    _stand_in(monkeypatch, runner, _script(None, before="No test cases found\n"))
    with pytest.raises(RuntimeError, match="GdUnit4 discovered no test suites"):
        runner.run(str(tmp_path))
