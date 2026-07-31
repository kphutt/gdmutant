"""Every repository file this suite reads must be copied into mutmut's `mutants/` tree.

mutmut runs the suite from a generated copy of the project (`mutants/`), and copies into it only
`[tool.mutmut] also_copy` plus its own defaults. A test that reads a file at the repository root
therefore passes in a normal `pytest` run and raises `FileNotFoundError` under mutmut — which
fails the *baseline* (the unmutated run mutmut takes its timings and test mapping from), so mutmut
aborts and evaluates **zero** mutants. The mutation score does not go down; it stops existing.

That has now happened three times (`.pre-commit-config.yaml`, `poodle.toml`, `action.yml`), each
time from an ordinary, correct-looking new test, and each time the connection between "I added a
test" and "the mutation signal died" was invisible at the moment the test was written. CI does
catch it — `.github/workflows/mutation.yml` fails the job outright when zero mutants were
evaluated — but only *after* a merge, in a job that is not a required check, and never on the
maintainer's Windows machine, where mutmut cannot run at all. This module is the earlier, local
half of that pair: it is an ordinary test, so it runs in `verify` on every platform, before the
merge, and it names the file and the fix instead of a `FileNotFoundError` deep in a CI log.

**How it decides what the suite reads.** Every test here reaches the repository root through one
expression, `Path(__file__).resolve().parent.parent`, so the scan below parses each test module and
collects the first path segment of anything built from it — `REPO / "action.yml"`,
`Path(__file__).resolve().parent.parent / "scripts" / "x.py"`, `REPO.glob("docs/**/*.md")`. It reads
the code rather than running it, so an env-gated or skipped test is covered like any other.

**What it cannot see**, stated plainly so nobody trusts it further than it goes: a path built some
other way — `Path(__file__).parents[1]`, a root passed in as a fixture, a segment held in a
variable rather than written as a literal. Those still reach CI's zero-mutant check, which is the
universal backstop. This is a cheap early warning for the shape the suite actually uses, not a
proof.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from mutmut.configuration import _load_config

REPO = Path(__file__).resolve().parent.parent

_PYPROJECT = REPO / "pyproject.toml"

#: The one expression every test module in this suite uses to reach the repository root.
_REPO_ROOT_EXPR = "Path(__file__).resolve().parent.parent"


def _test_modules() -> list[Path]:
    return sorted(REPO.glob("tests/*.py"))


def _root_aliases(tree: ast.Module) -> set[str]:
    """Names bound at any level to the repo-root expression — `REPO`, `_ROOT`, `REPO_ROOT`, …"""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and ast.unparse(node.value) == _REPO_ROOT_EXPR
    }


def _first_segment(literal: str) -> str:
    """The top-level repository entry a path literal names — `"docs/**/*.md"` -> `"docs"`."""
    return literal.replace("\\", "/").split("/")[0]


def _root_entries_read_by(module: Path) -> set[str]:
    """The top-level repository entries `module` builds a path to."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    aliases = _root_aliases(tree)

    def is_root(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        return ast.unparse(node) == _REPO_ROOT_EXPR

    entries: set[str] = set()
    for node in ast.walk(tree):
        # `<root> / "name"` — the innermost `/` of a chain, so `REPO / "docs" / "x"` gives `docs`.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and is_root(node.left):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                entries.add(_first_segment(node.right.value))
        # `<root>.glob("docs/**/*.md")` — a glob reads every file it matches.
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"glob", "rglob"}
            and is_root(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            entries.add(_first_segment(node.args[0].value))
    return entries


def _reads_by_module() -> dict[str, set[str]]:
    """Every repository-root entry the suite reads, mapped to the modules that read it."""
    reads: dict[str, set[str]] = {}
    for module in _test_modules():
        for entry in _root_entries_read_by(module):
            reads.setdefault(entry, set()).add(module.name)
    return reads


def _entries_present_in_the_mutants_tree(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """What mutmut would put in `mutants/`, asked of mutmut itself rather than restated here.

    `_load_config` is the function that assembles the real list — the repo's `also_copy` plus
    mutmut's own implicit additions (`tests/`, `pyproject.toml`, …) — so reading it cannot drift
    from mutmut's behaviour the way a hand-maintained copy of those defaults would. It resolves
    `pyproject.toml` against the working directory, hence the `chdir`. `source_paths` is copied by
    a different step (`copy_src_dir`) but lands in the same tree, so it counts as present.
    """
    monkeypatch.chdir(REPO)
    config = _load_config()
    return {str(path).rstrip("/\\") for path in config.also_copy + config.source_paths}


def test_every_repo_file_the_suite_reads_is_copied_into_the_mutants_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    present = _entries_present_in_the_mutants_tree(monkeypatch)
    missing = {
        entry: sorted(modules)
        for entry, modules in _reads_by_module().items()
        if entry not in present
    }
    assert not missing, (
        "these repository entries are read by the test suite but are not copied into mutmut's "
        "`mutants/` tree, so the mutation baseline will abort and no mutants will be evaluated. "
        "Add each to `[tool.mutmut] also_copy` in pyproject.toml:\n"
        + "\n".join(
            f"  {entry}  (read by {', '.join(modules)})" for entry, modules in missing.items()
        )
    )


def test_the_scan_sees_both_shapes_it_looks_for() -> None:
    # A scan that quietly stopped matching would turn the check above into a vacuous pass. These
    # two entries are not a sample of the suite — they are the two path shapes *this module* uses:
    # `REPO.glob("tests/*.py")` above (the glob branch) and `REPO / "pyproject.toml"` (the `/`
    # branch). So the assertion holds for as long as this file works at all, and fails the moment
    # either branch stops finding anything.
    assert {"tests", "pyproject.toml"} <= set(_reads_by_module())


def test_no_also_copy_entry_names_a_file_that_is_gone() -> None:
    # `copy_also_copy_files` skips an entry that does not exist, silently and without an error, so
    # a renamed or deleted file leaves an `also_copy` line that reads as protection but copies
    # nothing. The hole only shows up as a baseline failure much later, if some test still reads it.
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["mutmut"]["also_copy"]
    gone = [entry for entry in declared if not (REPO / entry).exists()]
    assert not gone, f"`[tool.mutmut] also_copy` names paths that no longer exist: {gone}"
