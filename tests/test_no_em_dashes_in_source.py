"""No em dash (U+2014) survives in a `gdmutant/` string literal a user or agent actually reads.

The operator's own writing voice bans em dashes everywhere published under their name, and this
repo's docs and README were already swept for it. The tool's own runtime output (console messages,
argparse help, the HTML report) was the gap: every user-facing string in `gdmutant/` carried the
same dash the docs had already dropped. Fixed once, this test is what stops it drifting back in a
future console message or help string nobody thought to re-check by hand.

Scope is deliberately `gdmutant/` only, and only string *constants* the interpreter actually
builds at runtime:

* Docstrings are excluded. They document the source for a contributor reading the code, not
  something the tool prints, and the operator's own ban is about published/user-facing text.
* `#` comments are excluded for the same reason, and are not AST nodes in the first place, so an
  `ast.walk` never even sees them.
* `tests/` and `scripts/` are out of scope entirely: their prose is contributor-facing, not
  something `gdmutant run` ever prints, so it is not what this guard exists to catch.

A survivor here is reported as `path:lineno`, not just a file name, because a file can hold
several string constants and the whole point of a Check is naming exactly what to fix, not making
someone grep for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_EM_DASH = "—"


def _docstring_ids(tree: ast.Module) -> set[int]:
    """The `id()` of every module/class/function docstring's `Constant` node in `tree`.

    A docstring is only ever the first statement of the module or a `def`/`class` body, and only
    when that statement is a bare string expression — the same shape `ast.get_docstring` itself
    keys on, re-derived here because that helper hands back the *text*, not the node, and matching
    by node identity is what lets everything else in the tree be walked and compared safely.
    """
    ids: set[int] = set()
    scoped_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    scopes: list[ast.AST] = [tree]
    scopes += [n for n in ast.walk(tree) if isinstance(n, scoped_types)]
    for scope in scopes:
        body = getattr(scope, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _em_dash_sites(path: Path) -> list[str]:
    """Every `path:lineno` where a non-docstring string constant in `path` holds an em dash."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    excluded = _docstring_ids(tree)
    rel = path.relative_to(REPO).as_posix()
    return [
        f"{rel}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in excluded
        and _EM_DASH in node.value
    ]


def test_no_em_dash_in_a_gdmutant_source_string() -> None:
    paths = sorted((REPO / "gdmutant").rglob("*.py"))
    sites = [site for path in paths for site in _em_dash_sites(path)]
    assert not sites, (
        "em dash (U+2014) found in a gdmutant/ string literal at: "
        + ", ".join(sites)
        + ". Replace with a comma, colon, period, or parentheses (vary it; a docstring is fine)."
    )
