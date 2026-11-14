"""Tests for the bounded capture of a test run's output (`engine.runner.run_bounded`).

Most drive it through `CommandRunner`, which adds the least between a command and its result."""

import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import bounded_output

import gdmutant.engine.runner as runner_mod
from gdmutant.engine.runner import (
    CommandRunner,
    SuiteResult,
    SuiteTimeout,
    script_error_excerpt,
)


def _runs(script: str) -> list[str]:
    return [sys.executable, "-c", script]


#: The most traced memory a flood test lets a run reach. Holding a bounded tail peaks far below it
#: (about 0.6 MiB), however big the flood.
_PEAK_BOUND = 4 * 1024 * 1024

#: How much the two script-error flood tests print. They stay fast at this size: one has 1 MiB
#: lines, and the other's lines are not split once its excerpt is complete.
_FLOOD_MB = 64


def _peak_traced_bytes(run: Callable[[], SuiteResult]) -> tuple[SuiteResult, int]:
    # tracemalloc sees every Python allocation, the reader threads' included, which is where
    # holding the whole output would show up.
    tracemalloc.start()
    try:
        result = run()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_command_runner_holds_a_bounded_tail_of_a_flooded_output(tmp_path: Path) -> None:
    # A flooded output costs a fixed amount of memory, and the verdict and the end of the output
    # survive. Each stream prints the whole bound in short lines, so a run that held all of it
    # would peak at about four times the bound. Short lines are slow to read, so the flood is no
    # bigger than that.
    lines = _PEAK_BOUND // len(b"stdout flood line\n")
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'stdout flood line\\n' * {lines})\n"
        f"sys.stderr.buffer.write(b'stderr flood line\\n' * {lines})\n"
        "sys.stderr.write('the real reason\\n')\n"
        "sys.exit(1)\n"
    )
    runner = CommandRunner(_runs(script))
    result, peak = _peak_traced_bytes(lambda: runner.run(str(tmp_path)))
    assert (result.failures, result.errors) == (1, 0)
    assert result.detail.endswith("stderr flood line\nthe real reason")
    assert len(result.detail) == 2000
    assert peak < _PEAK_BOUND, f"peak {peak} bytes for {2 * _PEAK_BOUND} bytes of output"


def test_command_runner_still_finds_a_script_error_after_a_flood(tmp_path: Path) -> None:
    # The marker can come after the flood, and the excerpt is still the marker line and the three
    # after it, exactly as `script_error_excerpt` gives it for the whole output.
    script = (
        "import sys\n"
        f"for _ in range({_FLOOD_MB}):\n"
        "    sys.stdout.buffer.write(b'x' * 1048576 + b'\\n')\n"
        "print('SCRIPT ERROR: Nonexistent function')\n"
        "print('   at: f (res://a.gd:3)')\n"
        "print('one')\n"
        "print('two')\n"
        "print('three')\n"
    )
    runner = CommandRunner(_runs(script))
    result, peak = _peak_traced_bytes(lambda: runner.run(str(tmp_path)))
    assert (result.failures, result.errors) == (0, 1)
    assert (
        result.runtime_error
        == "SCRIPT ERROR: Nonexistent function\n   at: f (res://a.gd:3)\none\ntwo"
    )
    assert peak < _PEAK_BOUND, f"peak {peak} bytes for {_FLOOD_MB} MB of output"


def test_command_runner_keeps_the_first_script_error_through_a_runaway_error_loop(
    tmp_path: Path,
) -> None:
    # An error loop printing SCRIPT ERROR until the timeout: the excerpt is the first one, and the
    # detail still shows how the run ended.
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'SCRIPT ERROR: first\\n   at: a.gd:1\\none\\ntwo\\n')\n"
        f"for _ in range({_FLOOD_MB}):\n"
        "    sys.stdout.buffer.write(b'SCRIPT ERROR: again\\n' * 52429)\n"
        "sys.stdout.buffer.write(b'the end')\n"
    )
    runner = CommandRunner(_runs(script))
    result, peak = _peak_traced_bytes(lambda: runner.run(str(tmp_path)))
    assert (result.failures, result.errors) == (0, 1)
    assert result.runtime_error == "SCRIPT ERROR: first\n   at: a.gd:1\none\ntwo"
    assert result.detail.endswith("SCRIPT ERROR: again\nthe end")
    assert peak < _PEAK_BOUND, f"peak {peak} bytes for {_FLOOD_MB} MB of output"


def test_command_runner_script_error_detail_shows_the_last_2000_characters(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.write('SCRIPT ERROR: x\\n' + 'y' * 5000 + 'end')"
    detail = CommandRunner(_runs(script)).run(str(tmp_path)).detail
    assert detail.split("Output:\n", 1)[1] == "y" * 1997 + "end"


def test_command_runner_detail_falls_back_to_stdout_when_stderr_is_silent(tmp_path: Path) -> None:
    script = "import sys; print('only on stdout'); sys.exit(1)"
    assert CommandRunner(_runs(script)).run(str(tmp_path)).detail == "only on stdout"


def test_command_runner_keeps_a_character_cut_off_by_the_end_of_the_stream(
    tmp_path: Path,
) -> None:
    # The first two bytes of a three-byte UTF-8 character, and then the stream ends. The decoder
    # holds them back waiting for the third, so they must still be flushed, as a replacement
    # character, when the stream closes: dropped, a loss in the output would leave no trace.
    script = "import sys; sys.stdout.buffer.write(b'abc\\xe2\\x82'); sys.exit(1)"
    assert CommandRunner(_runs(script)).run(str(tmp_path)).detail == "abc�"


def test_command_runner_waits_for_its_readers_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child can exit before its output is read to the end. That is not a timeout: the runner
    # waits for its readers, for whatever is left of the budget, and keeps what they read.
    drain = runner_mod._StreamTail.drain

    def slow_drain(self: runner_mod._StreamTail, pipe: object) -> None:
        time.sleep(0.5)
        drain(self, pipe)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_mod._StreamTail, "drain", slow_drain)
    result = CommandRunner(_runs("import sys; print('late'); sys.exit(1)"), timeout=10).run(
        str(tmp_path)
    )
    assert result.detail == "late"


def test_command_runner_refuses_a_run_whose_output_could_not_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stream lost part way could have held the SCRIPT ERROR that decides the verdict, so the run
    # raises (the engine tallies `error`) rather than being scored off what was read.
    def boom(self: object, text: str) -> None:
        raise OSError("pipe broke")

    monkeypatch.setattr(runner_mod._StreamTail, "_feed", boom)
    with pytest.raises(RuntimeError, match=r"^could not read the command's output: pipe broke$"):
        CommandRunner(_runs("print('x')")).run(str(tmp_path))


@pytest.mark.parametrize("offset", range(-12, 2))
def test_command_runner_finds_a_marker_cut_by_a_long_line_piece(
    tmp_path: Path, offset: int
) -> None:
    # A line longer than the runner keeps is looked at in pieces; a marker straddling a cut must
    # still be found, wherever the cut falls in it.
    start = runner_mod._LINE_CHARS + offset
    script = f"import sys; sys.stdout.write('x' * {start} + 'SCRIPT ERROR' + 'y' * 9000)"
    result = CommandRunner(_runs(script)).run(str(tmp_path))
    assert result.errors == 1


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("a\nSCRIPT ERROR: x\n", "e1\ne2\ne3\n"),
        ("a\nSCRIPT ERROR: x", "e1\ne2\ne3\n"),
        ("SCRIPT ERROR: x\r", "e1\ne2\n"),
        ("SCRIPT ERROR: x\r\n", "e1\ne2\n"),
        ("SCRIPT ERROR: x\r\n\r\nlast\n", ""),
        ("plain\n", "w\nSCRIPT ERROR: y\r\nz\n"),
        ("", "SCRIPT ERROR: only\n\n\nafter\nmore\n"),
        # A newline straight after a break only `splitlines` knows ends an empty line of its own.
        ("SCRIPT ERROR: boom\x0b\n   at: res://a.gd:1\none\n", ""),
        ("SCRIPT ERROR: boom\n\x0c\n   at: res://a.gd:1\n", ""),
        ("", "SCRIPT ERROR: boom\u2028\n\u2029\n   at: res://a.gd:1\n"),
    ],
)
def test_command_runner_excerpt_matches_the_whole_output_one(
    tmp_path: Path, stdout: str, stderr: str
) -> None:
    # The excerpt rebuilt from both tails must equal `script_error_excerpt` of the joined output,
    # with newlines translated, including where stdout's excerpt carries on into stderr.
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write({stdout.encode()!r})\n"
        f"sys.stderr.buffer.write({stderr.encode()!r})\n"
    )
    result = CommandRunner(_runs(script)).run(str(tmp_path))
    out, err = (s.replace("\r\n", "\n").replace("\r", "\n") for s in (stdout, stderr))
    assert result.runtime_error == script_error_excerpt(f"{out}\n{err}")


def test_command_runner_trailing_whitespace_does_not_push_out_the_detail(tmp_path: Path) -> None:
    script = "import sys; sys.stderr.write('X' * 3000 + ' ' * 3500); sys.exit(1)"
    assert CommandRunner(_runs(script)).run(str(tmp_path)).detail == "X" * 2000


@pytest.mark.parametrize("brk", ["\\n", "\\x0b"])
def test_command_runner_keeps_the_at_line_after_a_long_script_error_line(
    tmp_path: Path, brk: str
) -> None:
    # The long line arrives over several reads, so it is taken in pieces; they count as one line.
    # "\x0b" is a line break to `splitlines` only, so it ends the line inside its last piece.
    script = (
        "import sys, time\n"
        "sys.stdout.write('SCRIPT ERROR: ' + 'x' * 20000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        f"sys.stdout.write('{brk}   at: res://player.gd:42\\n')\n"
    )
    result = CommandRunner(_runs(script)).run(str(tmp_path))
    assert result.runtime_error is not None
    assert result.runtime_error.splitlines()[1:] == ["   at: res://player.gd:42"]


def test_command_runner_finds_a_marker_late_in_a_long_line(tmp_path: Path) -> None:
    script = (
        "import sys, time\n"
        "sys.stdout.write('x' * 20000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stdout.write('SCRIPT ERROR: late\\n   at: res://a.gd:1\\n')\n"
    )
    result = CommandRunner(_runs(script)).run(str(tmp_path))
    assert result.runtime_error is not None
    assert result.runtime_error.endswith("SCRIPT ERROR: late\n   at: res://a.gd:1")


def test_command_runner_translates_windows_line_endings(tmp_path: Path) -> None:
    # Also a "\r\n" split across two reads, which must not turn into two line breaks.
    script = (
        "import sys, time\n"
        "sys.stderr.buffer.write(b'a\\r\\nb\\rc\\r')\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stderr.buffer.write(b'\\nd')\n"
        "sys.exit(1)\n"
    )
    assert CommandRunner(_runs(script)).run(str(tmp_path)).detail == "a\nb\nc\nd"


def test_command_runner_kills_the_command_when_a_reader_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        started.append(popen(*args, **kwargs))
        return started[-1]

    class SecondFails(threading.Thread):
        count = 0

        def start(self) -> None:
            SecondFails.count += 1
            if SecondFails.count == 2:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(runner_mod.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(runner_mod, "threading", SimpleNamespace(Thread=SecondFails))
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="can't start new thread"):
        CommandRunner(_runs("import time; time.sleep(30)")).run(str(tmp_path))
    assert started[0].poll() is not None
    assert time.monotonic() - began < 15  # killed, not waited out


def test_command_runner_detail_prefers_stderr_even_when_it_is_only_whitespace(
    tmp_path: Path,
) -> None:
    # Stderr is preferred whenever it said anything at all.
    script = "import sys; print('on stdout'); sys.stderr.write('  \\n'); sys.exit(1)"
    assert CommandRunner(_runs(script)).run(str(tmp_path)).detail == ""


@pytest.mark.parametrize(
    ("parts", "found"),
    [
        (["x" * 10 + "No test ca", "ses found", "y" * 100], {"No test cases found"}),
        (["No test", " cases", " found"], {"No test cases found"}),
        (["No test cases foun", "d"], {"No test cases found"}),
        (["No test cases", "\nfound"], set()),
        (["a!", "b"], {"!"}),
        # After the SCRIPT ERROR excerpt is complete `_feed` stops splitting lines; it must not
        # stop watching.
        (["SCRIPT ERROR: x\n1\n2\n3\n", "No test cases found"], {"No test cases found"}),
    ],
)
def test_a_watched_string_is_found_even_when_reads_cut_it(
    parts: list[str], found: set[str]
) -> None:
    tail = runner_mod._StreamTail(["No test cases found", "!"])
    for part in parts:
        tail._feed(part)
    assert tail.found == found


def test_asking_about_an_unwatched_string_is_refused() -> None:
    # It would always answer no, which is a check that passes without checking anything.
    output = bounded_output(0, "No test cases found")
    with pytest.raises(ValueError, match="'No test cases found' was not watched"):
        output.saw("No test cases found")


def test_bounded_output_of_text_reads_as_a_real_run_would() -> None:
    assert bounded_output(1).detail() == ""
    assert bounded_output(1, "a\r\nb\rc").detail() == "a\nb\nc"
    assert bounded_output(1, "out", "a\r\nb\rc").detail() == "a\nb\nc"


def test_command_runner_a_child_holding_its_output_open_times_out(tmp_path: Path) -> None:
    # The command exits at once, but a grandchild it started keeps the pipes open past the
    # budget: that is a timeout.
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n"
    )
    with pytest.raises(SuiteTimeout):
        CommandRunner(_runs(script), timeout=0.5).run(str(tmp_path))


# Every character `str.splitlines` breaks a line on, bar "\n" (the stream is cut on that) and "\r"
# (the decoder turns it into "\n" before `_StreamTail` sees any text, so it cannot get there).
_SPLITLINES_BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


def _stream_tail(whole: str, cuts: tuple[int, ...]) -> runner_mod._StreamTail:
    """A `_StreamTail` after `whole` is fed in, cut into reads at `cuts`."""
    tail = runner_mod._StreamTail()
    start = 0
    for end in (*cuts, len(whole)):
        tail._feed(whole[start:end])
        start = end
    return tail


def _long_line_cases(brk: str) -> list[tuple[str, str]]:
    """Long lines with breaks around the places where they are cut into pieces."""
    size = runner_mod._LINE_CHARS
    cases = [(f"break at {n}", "y" * n + brk + "q" * 5000) for n in range(size - 30, size + 5)]
    # Two breaks in one overlap: the text between them is a line of its own, counted once.
    cases += [
        (f"two breaks at {n}", "y" * n + brk + "w" + brk + "q" * 5000)
        for n in range(size - 16, size + 2)
    ]
    # A marker that starts in the overlap, after a break in it, must still be found.
    cases += [
        (f"marker after break at {n}", "y" * n + brk + "SCRIPT ERROR" + "y" * 6000)
        for n in range(size - 22, size - 8)
    ]
    # A marker and a break both at the edge of the overlap, so one more or one fewer character of
    # overlap changes what is kept. The overlap is `len(marker) - 1` characters wide.
    marker = runner_mod._SCRIPT_ERROR_MARKER
    edge = size - (len(marker) - 1)
    for at_marker in range(edge - 3, edge + 3):
        for at_break in range(edge - 4, edge + 4):
            if at_break + 1 <= at_marker or at_break >= at_marker + len(marker):
                line = ["y"] * (size + 200)
                line[at_marker : at_marker + len(marker)] = marker
                line[at_break] = brk
                cases.append((f"marker at {at_marker}, break at {at_break}", "".join(line)))
    cases.append(("all breaks", brk * 9000))
    return cases


@pytest.mark.parametrize("brk", _SPLITLINES_BREAKS)
def test_stream_tail_excerpt_is_the_same_however_the_output_is_read(brk: str) -> None:
    """A line break `splitlines` knows, anywhere near where a long line is cut into pieces, must not
    change which lines the excerpt holds, wherever the reads happen to end. The cut keeps an
    overlap so a split marker is found, and a break inside it used to be counted twice."""
    size = runner_mod._LINE_CHARS
    at = "   at: res://a.gd:1"
    for name, long_line in _long_line_cases(brk):
        whole = long_line + "\n" + at + "\nB\nC\n"
        if "SCRIPT ERROR" not in long_line:
            whole = "SCRIPT ERROR: boom\n" + whole
        lines = whole.splitlines()
        first = next(i for i, line in enumerate(lines) if "SCRIPT ERROR" in line)
        wanted = lines[first : first + 4]
        for cuts in (
            (),
            (len(whole) // 2,),
            (size - 1, size, size + 1),
            tuple(range(1, len(whole), 997)),
        ):
            tail = _stream_tail(whole, cuts)
            excerpt = tail.excerpt
            where = f"{name}, break {brk!r}, cuts {cuts[:3]}"
            assert excerpt is not None, where  # the marker must be found
            # Same lines as the whole output. A long one may be kept cut short, never changed.
            assert len(excerpt) == len(wanted), where
            for number, (kept, line_of_whole) in enumerate(zip(excerpt, wanted, strict=True)):
                # The marker line may start in the overlap, so it can lose the start of its line.
                assert (kept in line_of_whole) if number == 0 else line_of_whole.startswith(kept), (
                    where
                )
            # The first lines are kept too, and count the same breaks.
            assert len(tail.head) == len(lines[:3]), where
            for kept, line_of_whole in zip(tail.head, lines[:3], strict=True):
                assert line_of_whole.startswith(kept), where


@pytest.mark.parametrize("brk", [None, *_SPLITLINES_BREAKS])
def test_stream_tail_marker_line_starts_where_the_overlap_does(brk: str | None) -> None:
    """A marker found after the first cut of a long line is kept from the start of the overlap (the
    last `len(marker) - 1` characters of the piece before it), or from just after the last line
    break in them. So the overlap's width shows in the kept line: a wider or narrower one fails."""
    size = runner_mod._LINE_CHARS
    marker = runner_mod._SCRIPT_ERROR_MARKER
    overlap_start = size - (len(marker) - 1)
    for pos in range(overlap_start - 3, size + 3):
        line = "a" * pos + (brk or "a") + "a" * (size + 50 - pos - 1) + marker + " boom"
        starts_at = pos + 1 if brk and pos >= overlap_start else overlap_start
        for cuts in ((len(line),), (500, size - 1, size + 1, len(line))):
            excerpt = _stream_tail(line + "\n", cuts).excerpt
            where = f"break {brk!r} at {pos}, cuts {cuts}"
            assert excerpt == [line[starts_at:]], where
