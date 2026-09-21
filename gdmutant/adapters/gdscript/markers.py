"""Marker placement: where a coverage marker goes for each mutant (docs/decisions/0017, step 1).

A marker is one call, ``_GdmMarks.hit(N); ``, inserted in front of a statement in a throwaway copy
of the project. When the test suite runs on that copy, each call records that spot N was reached,
which later tells gdmutant which test files reach a mutant. This module only decides where the
markers go and writes the marked source. Nothing here runs Godot, and mutant runs never see a
marker: the marked source is for the marker run alone.

The rule every placement follows: a mutant's marker runs every time the mutated code runs, at the
same moment or just before it, inside the same function call. Running more often than the mutated
code is fine, because it only credits a test file that could not have killed the mutant, which
costs time and nothing else. Running less often is the dangerous direction, because the one test
that could kill the mutant would not be run. So when a spot cannot take a marker that keeps that
rule, it is flagged `RunEverything` instead, which means what gdmutant does today: the whole suite.

The marker is inserted on the statement's own first line, so every line number stays the same.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from lark import Token, Tree

from gdmutant.adapters.gdscript import _parse, _span_of
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.spans import text_at

#: The autoload the marked copy's ``project.godot`` gains. Every marker is a call on it.
MARKER_AUTOLOAD = "_GdmMarks"


def marker_call(spot: int) -> str:
    """The text inserted in front of a statement to mark spot `spot`, trailing space included."""
    return f"{MARKER_AUTOLOAD}.hit({spot}); "


class RunEverything(Enum):
    """Why a mutant has no marker and must run the whole suite. The value is a short reason."""

    #: A class-level ``var`` initializer (``static``, ``@onready`` and ``@export`` too), an enum
    #: value, a signal, or a property's type or default. It runs at load time or never runs as a
    #: statement at all, so there is no statement to put a marker in front of.
    CLASS_LEVEL = "class-level code"
    #: A mutant inside an annotation's arguments, anywhere in the file.
    ANNOTATION = "annotation argument"
    #: A ``const``, class-level or local. Its value is fixed when the script compiles.
    CONST = "const"
    #: A single-line lambda. Its body is part of an expression, so it cannot be prefixed, and the
    #: statement that creates the lambda is not enough: the lambda can be called later, from
    #: another test.
    SINGLE_LINE_LAMBDA = "single-line lambda"
    #: The code between the marker and the mutated spot contains ``await``. The part after the
    #: pause can run in a later test than the marker did.
    AWAIT = "await"
    #: A default parameter value of a function whose body holds no statement to mark.
    NO_BODY_STATEMENT = "no statement in the function body"


#: A mutant's placement: the id of the spot whose marker covers it, or why it has none.
Placement = int | RunEverything


@dataclass(frozen=True)
class Spot:
    """One marker: its id, and the 1-based line and column of the original source where it is
    inserted. The line is the same in the marked source, since a marker never adds a line."""

    id: int
    line: int
    column: int


@dataclass(frozen=True)
class MarkedSource:
    """The result of `place_markers`.

    `source` is the marked source. `spots` lists its markers in source order. `placements` holds
    one entry per mutant passed in, in the same order: a spot id from `spots`, or a
    `RunEverything` reason.
    """

    source: str
    spots: tuple[Spot, ...]
    placements: tuple[Placement, ...]


#: Statements that can hold a mutant and take a marker in front of them. These are gdtoolkit's
#: statement rules inside a function body (``_func_stmt``). ``elif_branch``, ``else_branch`` and
#: ``match_branch`` are deliberately absent: they are parts of their ``if`` or ``match``
#: statement, never statements that can be prefixed.
_SIMPLE_STATEMENTS = frozenset(
    {
        "pass_stmt",
        "return_stmt",
        "func_var_stmt",
        "break_stmt",
        "breakpoint_stmt",
        "continue_stmt",
        "expr_stmt",
        "const_stmt",
    }
)
_COMPOUND_STATEMENTS = frozenset(
    {"if_stmt", "while_stmt", "for_stmt", "for_stmt_typed", "match_stmt"}
)
_STATEMENTS = _SIMPLE_STATEMENTS | _COMPOUND_STATEMENTS
#: The things whose body is a list of statements that run when it is called.
_SCOPES = frozenset({"func_def", "lambda", "property_custom_getter", "property_custom_setter"})


def _index_positions(tree: Tree[Token]) -> dict[tuple[int, int], tuple[Tree[Token] | Token, ...]]:
    """(line, column) -> the path from the root down to the deepest node or token starting there.

    A token swap mutant starts at its token. A statement deletion starts at its statement, where
    the deepest thing may be the statement itself (a bare ``return`` holds no token at all).
    """
    index: dict[tuple[int, int], tuple[Tree[Token] | Token, ...]] = {}

    def walk(node: Tree[Token], path: tuple[Tree[Token] | Token, ...]) -> None:
        """Record `node` and everything under it, deeper entries overwriting shallower ones."""
        here = (*path, node)
        index[(node.meta.line, node.meta.column)] = here
        for child in node.children:
            if isinstance(child, Tree):
                walk(child, here)
            else:
                start = _span_of(child)
                index[(start.line, start.column)] = (*here, child)

    walk(tree, ())
    return index


def _before(parent: Tree[Token], node: Tree[Token]) -> list[Tree[Token] | Token]:
    """`parent`'s children that come before `node` itself. Found by identity, not ``list.index``,
    which compares lark trees by value, so two identical statements (``a += 1; a += 1``) would
    both be found at the first."""
    position = next(i for i, child in enumerate(parent.children) if child is node)
    return parent.children[:position]


#: A statement to mark, with the node whose children list holds it.
_Target = tuple[Tree[Token], Tree[Token]]


class _Placer:
    """Placement for one source file: its lines, and where each node and token starts."""

    def __init__(self, source: str) -> None:
        self.lines = source.split("\n")
        self.index = _index_positions(_parse(source))

    def starts_its_line(self, node: Tree[Token]) -> bool:
        """True if nothing but indentation comes before `node` on its line."""
        text = self.lines[node.meta.line - 1]
        return len(text) - len(text.lstrip()) == node.meta.column - 1

    def insertion_node(self, statement: Tree[Token], parent: Tree[Token]) -> Tree[Token]:
        """The node `statement`'s marker goes in front of: the statement itself, or the first of
        any annotations right in front of it on the same line, so that in
        ``@warning_ignore("x") var y`` the annotation stays attached to its statement."""
        node = statement
        for before in reversed(_before(parent, statement)):
            if not (
                isinstance(before, Tree)
                and before.data == "annotation"
                and before.meta.line == statement.meta.line
            ):
                break
            node = before
        return node

    def takes_own_marker(self, statement: Tree[Token], parent: Tree[Token]) -> bool:
        """True if a marker can go right in front of `statement`: it starts its line, or another
        statement comes before it on the same line (``a(); b()``). False for a body written on its
        header's own line (``if x: return y``), whose marker belongs to the header's statement."""
        start = self.insertion_node(statement, parent)
        if self.starts_its_line(start):
            return True
        earlier = _before(parent, start)
        return bool(earlier) and isinstance(earlier[-1], Tree) and earlier[-1].data in _STATEMENTS

    def is_single_line_lambda(self, scope: Tree[Token]) -> bool:
        """True if `scope` is a lambda whose body is written on its header's own line."""
        body = [c for c in scope.children[1:] if isinstance(c, Tree)]
        return scope.data == "lambda" and (not body or not self.starts_its_line(body[0]))

    def place(self, mutant: Mutant) -> _Target | RunEverything:
        """The statement whose marker covers `mutant`, or why none can."""
        path = self.index.get((mutant.span.line, mutant.span.column))
        if path is None:
            raise ValueError(f"no token or statement starts at {mutant.span!r}")
        trees = [n for n in path if isinstance(n, Tree)]
        if any(n.data == "annotation" for n in trees):
            return RunEverything.ANNOTATION
        scopes = [i for i, n in enumerate(trees) if n.data in _SCOPES]
        if not scopes:
            if any(n.data == "const_stmt" for n in trees):
                return RunEverything.CONST
            return RunEverything.CLASS_LEVEL
        scope = trees[scopes[-1]]
        if self.is_single_line_lambda(scope):
            return RunEverything.SINGLE_LINE_LAMBDA
        # Each statement between the scope and the mutant, outermost first, with its index.
        statements = [
            (i, n) for i, n in enumerate(trees) if i > scopes[-1] and n.data in _STATEMENTS
        ]
        if not statements:
            # Inside the scope but in no statement: its header, so a default parameter value. It
            # is evaluated on a call, right before the body starts, so the first statement's
            # marker runs on every such call, and nothing can pause in between.
            body = [c for c in scope.children if isinstance(c, Tree) and c.data in _STATEMENTS]
            return (body[0], scope) if body else RunEverything.NO_BODY_STATEMENT
        if statements[-1][1].data == "const_stmt":
            return RunEverything.CONST
        climbed = [statements.pop()]
        # A body written on its header's line is marked by the header's statement (the ADR's
        # rule). An `elif` condition and a `match` pattern need no climbing: they belong to no
        # statement of their own, so their innermost statement already is the `if` or `match`.
        while statements and not self.takes_own_marker(climbed[-1][1], trees[climbed[-1][0] - 1]):
            climbed.append(statements.pop())
        # If the loop ran out of statements, the target is the first statement of a one-line
        # function (``func f(): return x``). Its marker goes right after the colon, the same
        # first-statement marker the default parameter rule uses.
        at, target = climbed[-1]
        if self.awaits_before(target, [n for _, n in climbed]):
            return RunEverything.AWAIT
        return target, trees[at - 1]

    def awaits_before(self, target: Tree[Token], climbed: list[Tree[Token]]) -> bool:
        """True if an ``await`` can pause between `target`'s marker and the mutated code.

        Counted: every ``await`` in `target`, except inside a statement with its own marker (a
        body statement on its own line, which runs after the header's code has finished) and
        inside a lambda (whose ``await`` pauses the lambda, not `target`). A ``while`` on the way
        to the mutant counts in full, body included, because the loop runs its condition again
        after its body, and a pause in the body comes before that.
        """
        whole = any(n.data == "while_stmt" for n in climbed)

        def counted(child: Tree[Token], parent: Tree[Token]) -> bool:
            """True if an ``await`` inside `child`, a node under `parent`, would count."""
            own = child.data in _STATEMENTS and self.takes_own_marker(child, parent)
            return child.data != "lambda" and (whole or not own)

        def walk(node: Tree[Token]) -> bool:
            """True if `node` holds a counted ``await``."""
            return node.data == "await_expr" or any(
                walk(child)
                for child in node.children
                if isinstance(child, Tree) and counted(child, node)
            )

        return walk(target)


def _insert(source: str, spots: Sequence[Spot]) -> str:
    """`source` with each spot's marker call inserted at its line and column."""
    lines = source.split("\n")
    # Right to left, so an earlier insertion on the same line never shifts a later one's column.
    for spot in sorted(spots, key=lambda s: (s.line, s.column), reverse=True):
        text = lines[spot.line - 1]
        head, tail = text[: spot.column - 1], text[spot.column - 1 :]
        lines[spot.line - 1] = head + marker_call(spot.id) + tail
    return "\n".join(lines)


def place_markers(source: str, mutants: Sequence[Mutant], first_spot: int = 0) -> MarkedSource:
    """Mark `source` for `mutants`, following the placement table in docs/decisions/0017.

    Every mutant gets exactly one placement: the id of a spot, or a `RunEverything` reason.
    Several mutants can share a spot. Spot ids count up from `first_spot` in source order, so a
    caller marking several files keeps ids unique by passing the next free id each time. Only
    statements some mutant needs get a marker, so with no mutants the source comes back as it was.

    Raises:
        ValueError: if a mutant's text does not match `source` at its span, which means it
            belongs to another file or an older version of this one.
    """
    for mutant in mutants:
        if text_at(source, mutant.span) != mutant.original:
            raise ValueError(f"mutant does not match the source at {mutant.span!r}")
    placer = _Placer(source)
    points: list[tuple[int, int] | RunEverything] = []
    for mutant in mutants:
        target = placer.place(mutant)
        if isinstance(target, RunEverything):
            points.append(target)
        else:
            node = placer.insertion_node(*target)
            points.append((node.meta.line, node.meta.column))
    marked = sorted({p for p in points if not isinstance(p, RunEverything)})
    spot_at = {point: first_spot + n for n, point in enumerate(marked)}
    spots = tuple(Spot(spot_at[point], *point) for point in marked)
    placements = tuple(p if isinstance(p, RunEverything) else spot_at[p] for p in points)
    return MarkedSource(_insert(source, spots), spots, placements)
