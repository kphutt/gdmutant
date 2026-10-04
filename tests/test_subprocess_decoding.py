"""Every captured child-process stream is decoded explicitly, and a bad byte costs nothing.

``subprocess.run(..., capture_output=True, text=True)`` decodes the child's bytes with the
*machine's* code page and ``errors="strict"``. Godot emits UTF-8. On Windows the code page is
usually cp1252, where five byte values (0x81, 0x8D, 0x8F, 0x90, 0x9D) have no character at all, and
a UTF-8 Linux locale is equally strict about any byte sequence that is not valid UTF-8. One such
byte raises ``UnicodeDecodeError`` inside subprocess's own reader thread, and **that exception does
not propagate**: ``subprocess.run`` returns with a zero exit code and ``stdout`` set to ``None``.
The stream is gone, and nothing says so.

Which is a wrong-verdict bug, not a cosmetic one. gdmutant reads ``SCRIPT ERROR`` out of that
captured output to catch a mutant whose GDScript runtime error never reaches an exit code
(`engine.runner.CommandRunner`, docs/decisions/0015 and 0017). A lost stream takes the marker with
it, so the mutant is scored off its exit code alone — silently, and in the direction that reports a
survivor as clean.

Two halves, because the shape has two ways to come back:

* `test_a_byte_the_code_page_cannot_decode_keeps_the_script_error_marker` drives a real child
  process through the real `CommandRunner.run`, with a real undecodable byte, and checks the marker
  still lands. It is locale-independent: 0x8F is unmapped in cp1252 *and* invalid UTF-8.
* The scan pins every other call site at once. There are forty of them across the package,
  `scripts/` and this suite, several of which only run against a real Godot or a real `gh`, so no
  behavioural test can reach them all. A call that captures output and decodes it has to say how —
  the mistake is a *missing* argument, which is exactly what reading the code can see. It reads each
  module's own imports to find the calls, so an aliased or directly imported `subprocess` function
  cannot slip past it.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from gdmutant.engine.runner import CAPTURE_ENCODING, CAPTURE_ERRORS, CommandRunner

REPO = Path(__file__).resolve().parent.parent

#: A byte with no character in cp1252, and not valid UTF-8 either — so it is undecodable under
#: strict decoding on every machine this suite runs on, Windows or Linux.
UNDECODABLE = b"\x8f"

#: The `subprocess` functions that hand bytes back as text. `call`/`check_call` return only an exit
#: code, so there is nothing for them to decode.
_CAPTURING_FUNCTIONS = ("run", "Popen", "check_output")

#: Arguments that put a `subprocess` call into text mode. Any one of them means the child's bytes
#: get decoded, so the call has to say with what.
_TEXT_MODE_ARGS = ("text", "universal_newlines", "encoding", "errors")

#: Where the package keeps the one decode policy. Inside `gdmutant/` the call sites name these
#: rather than repeating a literal, so the policy and its reasoning have a single home. `scripts/`
#: and the tests are standalone and spell the values out.
_POLICY_NAMES = {"encoding": "CAPTURE_ENCODING", "errors": "CAPTURE_ERRORS"}


def _child_writing(payload: bytes) -> list[str]:
    """A command that writes `payload` to stdout as raw bytes and exits 0.

    Exit 0 on purpose: a `SCRIPT ERROR` in the output is a failure *regardless* of the exit code,
    and a lost stream is exactly what makes a zero exit look like a pass.
    """
    return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"]


def test_a_byte_the_code_page_cannot_decode_keeps_the_script_error_marker(tmp_path: Path) -> None:
    payload = (
        b"running tests\n"
        b"SCRIPT ERROR: Invalid call on a null instance" + UNDECODABLE + b"\n"
        b"   at: Enemy.take_damage (res://enemy.gd:12)\n"
        b"done\n"
    )

    result = CommandRunner(_child_writing(payload)).run(str(tmp_path))

    # The verdict: an error, not the pass the zero exit code would otherwise buy.
    assert (result.tests, result.failures, result.errors) == (1, 0, 1)
    # The marker, and the valid text on both sides of the bad byte, all still here. `replace` keeps
    # the line's length and position, so the excerpt that names the file and line survives too.
    assert "SCRIPT ERROR: Invalid call on a null instance" in result.runtime_error
    assert "res://enemy.gd:12" in result.runtime_error
    # Replaced, not dropped: the reader of a captured stream can see that a byte was lost.
    assert "�" in result.runtime_error


def test_output_with_no_undecodable_byte_is_unchanged(tmp_path: Path) -> None:
    # The decode policy must not rewrite ordinary output. Non-ASCII that *is* valid UTF-8 (Godot
    # prints plenty in its own messages) arrives as itself, with no replacement character.
    message = "SCRIPT ERROR: ünïcode — fine"

    result = CommandRunner(_child_writing(f"{message}\n".encode())).run(str(tmp_path))

    # Compared through `ascii()` so a failure prints ASCII escapes: this suite runs on a Windows
    # console whose code page cannot encode either of those characters (AGENTS.md).
    assert ascii(result.runtime_error) == ascii(message)
    assert "�" not in result.runtime_error


def _modules() -> list[Path]:
    return sorted(
        [*REPO.glob("gdmutant/**/*.py"), *REPO.glob("scripts/**/*.py"), *REPO.glob("tests/**/*.py")]
    )


#: What a mutation tool calls the function bodies it generates. mutmut rewrites each function in a
#: copied tree into `x_name__mutmut_orig` plus one `x_name__mutmut_N` per mutant, all present in the
#: same file, and activates one at a time at runtime. Several of those variants deliberately break
#: the rule this module enforces (`encoding=None`, or the argument removed), so a scan that read
#: them would fail every time the suite runs from that copy -- which aborts mutmut's baseline and
#: makes it evaluate zero mutants, the "check that only works from a real checkout" shape AGENTS.md
#: warns about. The `__mutmut_orig` bodies are faithful copies of the real code, so skipping only
#: the numbered variants leaves every genuine call site in the scan's reach, in the copy as well as
#: in a real checkout.
_GENERATED_VARIANT = re.compile(r"__mutmut_\d+$")


def calls_in(node: ast.AST) -> Iterator[ast.Call]:
    """Every call in `node`, except those inside a mutation tool's generated variant."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and _GENERATED_VARIANT.search(
            child.name
        ):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from calls_in(child)


def capturing_spellings(tree: ast.Module) -> set[str]:
    """How this module spells a call to one of `_CAPTURING_FUNCTIONS`, read off its own imports.

    Matching the text ``subprocess.run`` would miss ``import subprocess as sp`` and
    ``from subprocess import run``, both of which this repository could pick up at any time. So the
    imports decide the names, rather than one assumed spelling.
    """
    spellings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    bound = alias.asname or "subprocess"
                    spellings.update(f"{bound}.{function}" for function in _CAPTURING_FUNCTIONS)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            spellings.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in _CAPTURING_FUNCTIONS
            )
    return spellings


def undecoded_calls(source: str, *, in_the_package: bool) -> list[str]:
    """Every `subprocess` call in `source` that decodes its child's output without saying how.

    Each finding is one line naming the call's line number and what is wrong with it. An empty
    list means every capturing call in `source` is explicit.
    """
    found: list[str] = []
    tree = ast.parse(source)
    spellings = capturing_spellings(tree)
    for node in calls_in(tree):
        if ast.unparse(node.func) not in spellings:
            continue
        given = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        if not any(arg in given for arg in _TEXT_MODE_ARGS):
            continue  # bytes mode: nothing is decoded, so there is nothing to pin.
        for arg in ("encoding", "errors"):
            value = given.get(arg)
            if value is None:
                found.append(f"line {node.lineno}: decodes output without an explicit `{arg}=`")
            elif isinstance(value, ast.Constant) and value.value is None:
                found.append(f"line {node.lineno}: `{arg}=None` is the implicit default again")
            elif in_the_package and ast.unparse(value) != _POLICY_NAMES[arg]:
                found.append(
                    f"line {node.lineno}: `{arg}={ast.unparse(value)}` — inside the package, use "
                    f"`engine.runner.{_POLICY_NAMES[arg]}` so the policy keeps one home"
                )
    return found


def test_every_capturing_subprocess_call_decodes_explicitly() -> None:
    offenders = {
        module.relative_to(REPO).as_posix(): findings
        for module in _modules()
        if (
            findings := undecoded_calls(
                module.read_text(encoding="utf-8"),
                in_the_package=module.relative_to(REPO).parts[0] == "gdmutant",
            )
        )
    }
    assert not offenders, (
        "these `subprocess` calls capture their child's output and let the machine's code page "
        "decode it, which loses the whole stream (`stdout is None`, no exception) on the first "
        "byte the code page has no character for — see this module's docstring:\n"
        + "\n".join(f"  {name}\n    " + "\n    ".join(f) for name, f in offenders.items())
    )


def test_the_scan_reaches_all_three_places_it_is_meant_to_cover() -> None:
    # Anti-vacuity. The check above passes just as happily when the scan has stopped matching
    # anything, so name the files that must be in its reach: one per directory, each a place where a
    # real external binary's output is read. If a rename empties the scan, this fails instead.
    reached = set()
    for module in _modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        spellings = capturing_spellings(tree)
        for node in calls_in(tree):
            if ast.unparse(node.func) in spellings and any(
                keyword.arg in _TEXT_MODE_ARGS for keyword in node.keywords
            ):
                reached.add(module.relative_to(REPO).as_posix())
    assert {
        "gdmutant/engine/runner.py",  # the exit-code runner: reads SCRIPT ERROR out of the output
        "gdmutant/adapters/gdscript/runner.py",  # GdUnit4/GUT: real Godot
        "gdmutant/cli.py",  # git, for --since and the backup check
        "scripts/check_mutation_baseline.py",  # mutmut's own output, read by a gate
        "tests/test_selftest_live.py",  # the live self-test: real Godot, Windows included
    } <= reached


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        (
            "import subprocess\nsubprocess.run(cmd, capture_output=True, text=True)",
            "without an explicit `encoding=`",
        ),
        (
            'import subprocess\nsubprocess.run(cmd, encoding="utf-8")',
            "without an explicit `errors=",
        ),
        (
            'import subprocess\nsubprocess.run(cmd, text=True, encoding="utf-8", errors=None)',
            "the implicit default",
        ),
        (
            "import subprocess\nsubprocess.Popen(cmd, text=True)",
            "without an explicit `encoding=`",
        ),
        # The two spellings a text match would have missed, which is why the imports decide.
        (
            "from subprocess import run\nrun(cmd, capture_output=True, text=True)",
            "without an explicit `encoding=`",
        ),
        (
            "import subprocess as sp\nsp.run(cmd, capture_output=True, text=True)",
            "without an explicit `encoding=`",
        ),
        (
            "from subprocess import check_output as grab\ngrab(cmd, text=True)",
            "without an explicit `encoding=`",
        ),
    ],
)
def test_the_scan_rejects_a_call_that_leaves_the_decode_implicit(
    snippet: str, expected: str
) -> None:
    # The other half of anti-vacuity: a checker nobody has ever seen say no is not a checker.
    findings = undecoded_calls(snippet, in_the_package=False)
    assert findings, f"the scan accepted {snippet!r}"
    assert any(expected in finding for finding in findings), findings


@pytest.mark.parametrize(
    "call",
    [
        'subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")',
        "subprocess.run(cmd, capture_output=True)",  # bytes mode: nothing decodes
        "subprocess.check_call(cmd)",  # no captured output at all
    ],
)
def test_the_scan_accepts_a_call_that_cannot_lose_a_stream(call: str) -> None:
    snippet = f"import subprocess\n{call}"
    # An accept must not be a miss, so check the module is in the scan's reach before believing it.
    assert capturing_spellings(ast.parse(snippet))
    assert undecoded_calls(snippet, in_the_package=False) == []


def test_inside_the_package_the_scan_insists_on_the_shared_policy() -> None:
    # Two paths that should agree, and one checks less, is a recurring bug here (AGENTS.md). A
    # second literal copy of the policy inside the package is how the two drift apart, so the scan
    # refuses one — while `scripts/` and the tests, which are standalone, may spell it out.
    inline = (
        "import subprocess\n"
        'subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")'
    )
    assert undecoded_calls(inline, in_the_package=True)
    assert undecoded_calls(inline, in_the_package=False) == []
    shared = (
        "import subprocess\n"
        "subprocess.run(cmd, capture_output=True, "
        "encoding=CAPTURE_ENCODING, errors=CAPTURE_ERRORS)"
    )
    assert undecoded_calls(shared, in_the_package=True) == []


def test_the_policy_is_the_one_the_tests_assert() -> None:
    # The constants are what the per-call-site assertions elsewhere in this suite compare against
    # (`test_gdunit_runner.py`, `test_gut_runner.py`, `test_marker_run.py`), so pin them here once.
    assert (CAPTURE_ENCODING, CAPTURE_ERRORS) == ("utf-8", "replace")


#: The shape mutmut writes into its copied tree: the real body kept as `__mutmut_orig`, and one
#: numbered variant per mutant, here with the decode policy deliberately broken.
_AS_A_MUTATION_TOOL_REWRITES_IT = """import subprocess


def x_read__mutmut_orig(cmd):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")


def x_read__mutmut_1(cmd):
    return subprocess.run(cmd, capture_output=True, encoding=None, errors="replace")


def x_read__mutmut_2(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)
"""


def test_a_mutation_tools_own_generated_variants_are_not_read_as_offenders() -> None:
    # Recurring bug three (AGENTS.md): the suite also runs from a copy of the tree that is not a
    # checkout. In mutmut's copy every variant of a function sits in the same file, including ones
    # that break this rule on purpose, and a scan that read them would fail there always -- which
    # aborts the baseline and evaluates zero mutants instead of lowering a score.
    assert undecoded_calls(_AS_A_MUTATION_TOOL_REWRITES_IT, in_the_package=False) == []


def test_the_original_body_in_that_copy_is_still_scanned() -> None:
    # The other half, so the filter above cannot quietly become "skip the whole file". The
    # `__mutmut_orig` body is a faithful copy of the real code, so a real offence inside it must
    # still be reported, in the copy exactly as in a checkout.
    broken_original = _AS_A_MUTATION_TOOL_REWRITES_IT.replace(
        'subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")',
        "subprocess.run(cmd, capture_output=True, text=True)",
    )
    findings = undecoded_calls(broken_original, in_the_package=False)
    # Exactly two: one for the missing `encoding`, one for the missing `errors`, and nothing at all
    # from the two numbered variants, which break the rule just as plainly.
    assert len(findings) == 2, findings
