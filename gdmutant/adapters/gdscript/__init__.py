"""GDScript adapter — the mutation half (no Godot).

Locates mutable tokens with gdtoolkit and turns them into engine `MutationSite`s, generates
`Mutant`s (via `engine.mutants.generate`), and enforces **NF-5** by re-parsing each mutant.

gdtoolkit does not surface tokens inside string literals or comments, and tokenizes compound
operators (`+=`, `->`, `>=`) atomically (verified with the tokenization spike), so keeping only
tokens the operator catalog mutates never edits inside a string/comment or half of a compound
operator. The Godot test runner is a separate concern (Slice 4).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import pairwise

from gdtoolkit.parser import parser as _gdparser
from lark import Token, Tree
from lark.exceptions import LarkError

from gdmutant.engine.adapter import Adapter
from gdmutant.engine.mutants import Mutant, MutationSite, generate
from gdmutant.engine.operators import CATALOG, Operator, all_replacements
from gdmutant.engine.spans import Span, text_at


def _parse(source: str) -> Tree[Token]:
    # gather_metadata attaches spans to Tree *nodes*; the token line/column positions this adapter
    # reads come from lark's lexer regardless. Kept on for any future tree-level use (harmless).
    tree: Tree[Token] = _gdparser.parse(source, gather_metadata=True)
    return tree


def _span_of(tok: Token) -> Span:
    line, col, end_line, end_col = tok.line, tok.column, tok.end_line, tok.end_column
    # lark's lexer always sets token positions; assert non-None only to satisfy the Optional types.
    assert line and col and end_line and end_col  # pragma: no cover
    return Span(line, col, end_line, end_col)


#: The operator id every statement-deletion mutant carries. The generation half lives further down
#: (`_statement_deletions`); the id is declared up here because `unknown_ignore_operators` needs it
#: to know that ``ignore[statement-deletion]`` names a real operator.
STATEMENT_DELETION_ID = "statement-deletion"

# The canonical annotation prefix (the spelling used in docs); the regex below is the lenient parse.
_IGNORE_MARKER = "# gdmutant: ignore"

# ``# gdmutant: ignore`` [optional ``[op1, op2]``] [optional reason]. Bare (no brackets) suppresses
# every operator on the line; ``[ops]`` suppresses only those; trailing text is the reason.
_IGNORE_RE = re.compile(r"#\s*gdmutant:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)$")


@dataclass(frozen=True)
class _IgnoreDirective:
    """A parsed ``# gdmutant: ignore`` annotation. `operators` is ``None`` for a bare marker (all
    operators on the line) or the set of operator ids to suppress; `reason` is the trailing text."""

    operators: frozenset[str] | None
    reason: str


def _ignore_directives(source: str) -> dict[int, _IgnoreDirective]:
    """1-based line -> the ``# gdmutant: ignore`` directive on it (a ``# noqa``-style opt-out for
    equivalent/unkillable mutants). Comments aren't tokens, so this scans raw source; lines split on
    ``\\n`` only, matching the engine's line counting (spans.py), so numbers align with tokens.

    ``# gdmutant: ignore`` → all operators on the line; ``ignore[comparison, numeric]`` → only them;
    text after the marker/brackets is the human reason (surfaced as the report's ``statusReason``).
    """
    directives: dict[int, _IgnoreDirective] = {}
    for i, line in enumerate(source.split("\n"), start=1):
        match = _IGNORE_RE.search(line)
        if match is None:
            continue
        ops_group, reason = match.group(1), match.group(2).strip()
        operators = (
            None
            if ops_group is None
            else frozenset(name.strip() for name in ops_group.split(",") if name.strip())
        )
        directives[i] = _IgnoreDirective(operators, reason)
    return directives


def unknown_ignore_operators(
    source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[tuple[int, str]]:
    """``(line, name)`` for every malformed operator scope in an ignore directive — either a name no
    mutant this adapter generates can carry (a likely typo) or **empty brackets** ``ignore[]``
    (reported with ``name == ""``). Both silently suppress nothing, so the CLI warns; the run is
    never failed.

    The valid names are `catalog`'s ids **plus** `STATEMENT_DELETION_ID`, which is exactly what
    `_mark_ignored` matches a directive against. Statement deletion is structural rather than a
    token swap, so it lives here instead of the token catalog (see `_statement_deletions`) — and
    validating against the catalog alone told anyone writing the documented, *working*
    ``# gdmutant: ignore[statement-deletion]`` that their annotation suppressed nothing.
    """
    valid = {op.id for op in catalog} | {STATEMENT_DELETION_ID}
    warnings: list[tuple[int, str]] = []
    for line, directive in _ignore_directives(source).items():
        if directive.operators is None:
            continue  # a bare marker (no brackets) is well-formed — suppresses the whole line
        if not directive.operators:
            warnings.append((line, ""))  # `ignore[]`: empty brackets, matches no operator
            continue
        warnings.extend((line, name) for name in sorted(directive.operators) if name not in valid)
    return warnings


def _string_format_percents(tree: Tree[Token]) -> set[tuple[int | None, int | None]]:
    """Positions ``(line, column)`` of ``%`` tokens that are the **string-format** operator, which
    the modulo operator must not mutate.

    ``%`` is overloaded in GDScript: arithmetic modulo *and* string formatting (``"fmt" % args``).
    The distinction is the direct **left operand**: a bare string literal means formatting. That has
    to be read from the parse tree, not the flat token stream — ``d["k"] % x`` is genuine modulo,
    but its token immediately before ``%`` is the *index* string ``"k"`` (its left operand is the
    ``d[...]`` ``subscr_expr`` subtree, not a string), so a token-adjacency check would wrongly drop
    it. Here each ``%`` in an ``mdr_expr`` (mul/div/remainder) node is skipped only when the node
    child *directly* to its left is a ``string`` node.

    Also recognised: a **computed string** left operand — a parenthesised ``+``-concatenation with a
    string-literal operand, e.g. ``("Hi " + name) % x``. No type inference: `name`'s runtime type is
    never checked, only the parse-tree shape (a `+`-only ``arith_expr`` with a bare-string operand
    somewhere in it) — a heuristic, not a proof, but this is the *noise* direction (a format ``%``
    wrongly mutated to ``*``/``/`` errors at runtime — an ERROR verdict, never a silently-wrong
    survivor). The risk this function actually guards against is the opposite one: newly suppressing
    a *genuine* modulo site. A `-` anywhere in the parenthesised expression (arithmetic, not
    string-building), or no string literal in it at all, both still rule that out.
    """
    skip: set[tuple[int | None, int | None]] = set()
    for node in tree.iter_subtrees():
        if node.data != "mdr_expr":
            continue
        for prev, cur in pairwise(node.children):
            if (
                isinstance(cur, Token)
                and cur.value == "%"
                and isinstance(prev, Tree)
                and _is_string_format_operand(prev)
            ):
                skip.add((cur.line, cur.column))
    return skip


def _is_string_format_operand(node: Tree[Token]) -> bool:
    """True if `node` (the direct left operand of a ``%``) is a bare string literal, or a
    parenthesised ``+``-only concatenation containing one (see `_string_format_percents`)."""
    if node.data == "string":
        return True
    if node.data == "par_expr" and len(node.children) == 1:
        inner = node.children[0]
        return isinstance(inner, Tree) and _is_string_concatenation(inner)
    return False


def _is_string_concatenation(node: Tree[Token]) -> bool:
    """True if `node` is an ``arith_expr`` joined only by ``+`` (never ``-``, which means genuine
    arithmetic) with at least one operand that is itself a string-format operand — a bare literal or
    a nested parenthesised concatenation, so ``("a" + ("b" + c)) % x`` is also recognised."""
    if node.data != "arith_expr":
        return False
    # `arith_expr` is flat and mixed: PLUS/MINUS operator *tokens* interleave with operands that are
    # themselves either Trees (a nested expression, e.g. a string literal) or bare Tokens (a NAME,
    # NUMBER, ...) — so operators must be picked out by token *type*, not by `isinstance(_, Token)`
    # alone, which every bare-token operand also satisfies.
    if any(isinstance(child, Token) and child.type == "MINUS" for child in node.children):
        return False
    operands = [child for child in node.children if isinstance(child, Tree)]
    return any(_is_string_format_operand(operand) for operand in operands)


def _string_concatenation_pluses(tree: Tree[Token]) -> set[tuple[int | None, int | None]]:
    """Positions ``(line, column)`` of ``+`` tokens that are **string concatenation**, which the
    arithmetic operator must not mutate.

    The catalog's only replacement for ``+`` is ``-`` (`engine.operators.ARITHMETIC`), and
    GDScript's ``String`` defines no ``-``. So the mutant can never measure a test gap: it either
    errors at runtime or sits on a line no test reaches, and in both cases it is reported as a
    survivor that was never a real mutant. Skipping it at generation time is the fix.

    Recognition reuses `_is_string_concatenation` (the same operand typing `_string_format_percents`
    applies to ``%``): an ``arith_expr`` joined only by ``+`` — a ``-`` anywhere means genuine
    arithmetic — with at least one bare-string operand. A string in a *numeric* position is not one,
    because the check reads the parse tree rather than the token stream: ``"5".to_int() + 3`` has a
    ``getattr_call`` operand and ``d["k"] + 1`` a ``subscr_expr``, neither of which is a ``string``
    node, so both stay ordinary arithmetic sites.
    """
    skip: set[tuple[int | None, int | None]] = set()
    for node in tree.iter_subtrees():
        if not _is_string_concatenation(node):
            continue
        skip.update(
            (child.line, child.column)
            for child in node.children
            if isinstance(child, Token) and child.type == "PLUS"
        )
    return skip


def _is_string_valued(node: Tree[Token]) -> bool:
    """True if `node` is an expression the **parse tree alone** shows to be a string: a bare
    literal, or a ``+``-only concatenation containing one (parenthesised or not).

    This is the union of the two operand shapes already recognised in this file — the bare-literal
    / parenthesised-concatenation pair `_is_string_format_operand` applies to ``%``, plus the
    unbracketed `arith_expr` concatenation `_string_concatenation_pluses` applies to ``+``. No type
    inference: a ``String``-typed *variable* is not recognised, because nothing in the tree says
    it is one.
    """
    return _is_string_format_operand(node) or _is_string_concatenation(node)


#: The one compound-assignment token that can appear with a string operand. `COMPOUND_ASSIGN` also
#: swaps ``-=``/``*=``/``/=``, but GDScript's ``String`` defines none of those, so a source line
#: spelling them on a string does not compile in the first place and can never reach this skip.
_STRING_COMPOUND_ASSIGN = "+="


def _string_compound_assigns(tree: Tree[Token]) -> set[tuple[int | None, int | None]]:
    """Positions ``(line, column)`` of ``+=`` tokens that **append to a string**, which the
    compound-assign operator must not mutate.

    The catalog's only replacement for ``+=`` is ``-=`` (`engine.operators.COMPOUND_ASSIGN`), and
    GDScript's ``String`` defines no ``-``. So the mutant is not a changed program whose behavior a
    test could disagree with — it is an invalid one, exactly like the ``+``-concatenation case
    `_string_concatenation_pluses` already skips. Leaving it in reports a survivor that was never a
    real mutant, which is the one thing a survivor must never be.

    This is NF-5's rule reaching a defect NF-5 cannot see. NF-5 drops a mutant whose source no
    longer *parses*, but gdtoolkit's grammar carries no type information — ``s -= "b"`` parses
    perfectly well and is rejected only by Godot, later. Recognising it from operand shape at
    generation time is the same policy applied one level up, not a new one.

    Recognition is `_is_string_valued` on the assignment's right-hand operand. It deliberately stops
    at what the tree proves. A ``String``-typed variable (``s += other``), a ``StringName``
    (``s += &"a"``) and a format expression (``s += "%s" % x``) are all string-valued at runtime and
    are all left as ordinary sites, because suppressing a *genuine* gap is the costlier error and
    only a literal makes the shape certain.
    """
    skip: set[tuple[int | None, int | None]] = set()
    for node in tree.iter_subtrees():
        if node.data != "assnmnt_expr":
            continue
        # `assnmnt_expr` is (target, operator token, value); pair the operator with what follows it.
        for cur, following in pairwise(node.children):
            if (
                isinstance(cur, Token)
                and cur.value == _STRING_COMPOUND_ASSIGN
                and isinstance(following, Tree)
                and _is_string_valued(following)
            ):
                skip.add((cur.line, cur.column))
    return skip


#: gdtoolkit's ``class_var_*`` rules that carry an initializer ``expr``. ``class_var_empty`` and
#: ``class_var_typed`` declare no initial value, so there is nothing to skip in them.
_INITIALIZED_CLASS_VAR_NODES = frozenset(
    {"class_var_assigned", "class_var_typed_assgnd", "class_var_inf"}
)
#: Nodes whose *evaluation* is observable independently of the value they produce, so a dead store
#: does not make a mutation inside them inert: a call can have side effects, a subscript can index
#: out of range, an ``await`` suspends. An initializer containing one keeps all its sites.
_EFFECTFUL_NODES = frozenset({"standalone_call", "getattr_call", "subscr_expr", "await_expr"})


def _inline_properties(node: Tree[Token]) -> Iterator[tuple[Tree[Token], Tree[Token]]]:
    """``(declaration, body)`` for each property declared with inline accessors among `node`'s
    direct children.

    gdtoolkit leaves the declaration's own ``inline_property_body`` node **empty** and hangs the
    accessors off a ``property_body_def`` that is the declaration's *next sibling*, not its child —
    so the two are paired by adjacency. ``static var`` wraps the declaration in an extra
    ``static_class_var_stmt``, which is unwrapped here so a static property is treated the same.
    """
    for current, following in pairwise(node.children):
        if not (isinstance(current, Tree) and isinstance(following, Tree)):
            continue
        if following.data != "property_body_def":
            continue
        # `static var` nests the declaration one level deeper (`static_class_var_stmt`); unwrap it
        # so a static property is read exactly like an ordinary one.
        statement = current.children[0] if current.data == "static_class_var_stmt" else current
        assert isinstance(statement, Tree)  # pragma: no cover — grammar: always a class_var_stmt
        declaration = statement.children[0]
        assert isinstance(declaration, Tree)  # pragma: no cover — grammar: always a class_var_*
        if declaration.data in _INITIALIZED_CLASS_VAR_NODES:
            yield declaration, following  # anything else declares no initial value to skip


def _dead_property_initializers(tree: Tree[Token]) -> set[tuple[int | None, int | None]]:
    """Positions ``(line, column)`` of every token in a property declaration's initializer whose
    stored value can never be read back — so no mutation of it can change observable behavior.

    GDScript runs a property's ``set`` on assignment but **not** on the initializer in the
    declaration itself, so that initializer writes the backing field directly. When the property
    also declares a custom ``get``, every read from outside routes through the getter instead, and
    the backing field is reachable only by naming the property *inside* its own accessors (where the
    name means the field, not the getter). So when neither accessor body mentions the property's own
    name, the initial value is written and never read — dead storage, and every mutant on it is
    inert by language rule rather than by any property of the test suite.

    Shown by the GUT v9.7.1 measurement rather than argued: in ``compare_result.gd`` the numeric
    mutants on the backing field ``var _max_differences = 30`` (line 15) were killed, while the ones
    on ``var max_differences = 30 :`` (line 16) — same literal, the next line — survived.

    Deliberately narrower than "the property has a custom setter", which would suppress genuine test
    gaps in three shapes that keep all their sites here: a **setter-only** property (no getter, so
    reads still return the initial value), a getter that **names the property itself**
    (``get: return health`` reads exactly the field the initializer wrote), and an initializer
    containing a call, subscript or ``await`` (the stored value is dead, but evaluating the
    expression is not — see `_EFFECTFUL_NODES`).
    """
    skip: set[tuple[int | None, int | None]] = set()
    for parent in tree.iter_subtrees():
        for declaration, body in _inline_properties(parent):
            name = declaration.children[0]  # every `class_var_*` rule opens with the NAME token
            assert isinstance(name, Token)  # pragma: no cover — grammar: always a NAME
            (initializer,) = [
                c for c in declaration.children if isinstance(c, Tree) and c.data == "expr"
            ]
            if not any(
                isinstance(c, Tree) and c.data == "property_custom_getter" for c in body.children
            ):
                continue  # no getter: reads still return the backing field the initializer wrote
            if any(tok == name.value for tok in body.scan_values(lambda v: isinstance(v, Token))):
                continue  # an accessor names the property, so it can read the backing field
            if any(sub.data in _EFFECTFUL_NODES for sub in initializer.iter_subtrees()):
                continue  # evaluating the initializer is observable even though its store is dead
            skip.update(
                (tok.line, tok.column)
                for tok in initializer.scan_values(lambda v: isinstance(v, Token))
            )
    return skip


def find_sites(source: str, catalog: tuple[Operator, ...] = CATALOG) -> list[MutationSite]:
    """Every token in `source` that `catalog` can mutate, located via gdtoolkit.

    Filtering by "does the catalog mutate this value" is sufficient: gdtoolkit never surfaces
    tokens from inside string literals or comments, so this never edits within one. `catalog` is
    threaded through so site selection matches generation (a custom catalog finds its own sites).

    Four syntactic exclusions drop tokens that cannot yield a meaningful mutant — each one a shape
    the language rules out, never a shape that merely tends to survive:

    * a ``%`` used as the string-format operator (`_string_format_percents`);
    * a ``+`` that is string concatenation (`_string_concatenation_pluses`);
    * a ``+=`` that appends to a string (`_string_compound_assigns`);
    * a property declaration's initializer whose stored value is unreadable
      (`_dead_property_initializers`).

    ``# gdmutant: ignore`` annotations are **not** filtered here: a suppressed mutant is still
    *generated*, then marked ``ignore_reason`` in `generate_mutants` so it surfaces in the report as
    ``Ignored`` (excluded from the score) rather than vanishing (see docs/decisions/0004, 0006).
    """
    tree = _parse(source)
    skipped = (
        _string_format_percents(tree)
        | _string_concatenation_pluses(tree)
        | _string_compound_assigns(tree)
        | _dead_property_initializers(tree)
    )
    return [
        MutationSite(tok.value, _span_of(tok))
        for tok in tree.scan_values(lambda v: isinstance(v, Token))
        if all_replacements(tok.value, catalog) and (tok.line, tok.column) not in skipped
    ]


def _mark_ignored(mutant: Mutant, directives: dict[int, _IgnoreDirective]) -> Mutant:
    """Return `mutant` tagged with an `ignore_reason` if an ignore directive on its line applies to
    its operator (a bare directive applies to every operator; ``[ops]`` only to the named ones)."""
    directive = directives.get(mutant.span.line)
    if directive is None:
        return mutant
    if directive.operators is not None and mutant.operator_id not in directive.operators:
        return mutant
    return replace(mutant, ignore_reason=directive.reason)


# Statement-deletion (FG-2.1) — replace a statement with ``pass``. Structural, not a token swap, so
# it's a separate path from the token catalog (docs/decisions/0007). Deletes expression statements
# (calls, assignments, ``+=``) and ``return``s. Declarations (``func_var_stmt``) are deferred:
# deleting one either breaks a later reference or is equivalent (unused) — both noise.
_DELETABLE_STMT_NODES = frozenset({"expr_stmt", "return_stmt"})
_FUNCTION_SCOPE_NODES = frozenset({"func_def", "lambda"})
_SCOPE_HEADER_NODES = frozenset({"func_header", "lambda_header"})
_STATEMENT_REPLACEMENT = "pass"  # STATEMENT_DELETION_ID is declared at the top of the module


def _scope_requires_a_return_value(scope: Tree[Token]) -> bool:
    """True if `scope` (a ``func_def`` or ``lambda``) declares a **non-void** return type — so Godot
    requires every path to return a value, and deleting a ``return`` may make it not compile.

    A ``lambda_header`` carries the same optional ``TYPE_HINT`` token as a ``func_header``, so a
    *typed lambda* (``func() -> int: return 9``) is guarded exactly like a typed function: deleting
    its return is the same "not all code paths return a value" Godot error (verified via
    ``--check-only``). An untyped lambda or ``-> void`` scope has no such requirement.
    """
    header = next(
        (c for c in scope.children if isinstance(c, Tree) and c.data in _SCOPE_HEADER_NODES), None
    )
    return header is not None and any(
        isinstance(c, Token) and c.type == "TYPE_HINT" and c.value != "void"
        for c in header.children
    )


def _scope_deletable_statements(scope: Tree[Token]) -> list[Tree[Token]]:
    """Deletable statement nodes inside `scope`, descending through control-flow blocks but NOT into
    nested function scopes (a lambda's statements belong to the lambda, its own scope)."""
    found: list[Tree[Token]] = []

    def walk(node: Tree[Token]) -> None:
        """Recursively add `node`'s deletable statements to `found`, stopping at a nested function
        scope (its statements belong to that scope, not this one)."""
        for child in node.children:
            if not isinstance(child, Tree) or child.data in _FUNCTION_SCOPE_NODES:
                continue
            if child.data in _DELETABLE_STMT_NODES:
                found.append(child)
            else:
                walk(child)

    walk(scope)
    return found


def _statement_deletions(path: str, source: str) -> list[Mutant]:
    """One ``pass``-replacement mutant per deletable single-line statement (FG-2.1).

    A ``return`` is emitted only when deleting it can't break compilation (docs/decisions/0007): the
    enclosing function is untyped/``void``, **or** the function body's last top-level statement is a
    *different* ``return`` (a guaranteed final return backstops the deletion, so every path still
    returns a value). gdtoolkit has no return-path analysis, so this generation-time guard — not
    NF-5's re-parse — is what keeps deletion mutants loadable in Godot (same pattern as
    `_string_format_percents`). Multi-line statements are skipped (`spans.py` single-line). Mutants
    are returned in document order.
    """
    mutants: list[Mutant] = []
    for scope in _parse(source).iter_subtrees():
        if scope.data not in _FUNCTION_SCOPE_NODES:
            continue
        typed = _scope_requires_a_return_value(scope)
        body = [
            c for c in scope.children if isinstance(c, Tree) and c.data not in _SCOPE_HEADER_NODES
        ]
        last = body[-1] if body else None
        last_is_return = last is not None and last.data == "return_stmt"
        for stmt in _scope_deletable_statements(scope):
            if stmt.data == "return_stmt" and typed and not (last_is_return and stmt is not last):
                continue  # deleting it would leave a typed function with no guaranteed return value
            meta = stmt.meta
            if meta.empty or meta.line != meta.end_line:
                continue  # no span, or a multi-line statement (spans.py edits a single line only)
            span = Span(meta.line, meta.column, meta.end_line, meta.end_column)
            original = text_at(source, span)
            mutants.append(
                Mutant(path, span, STATEMENT_DELETION_ID, original, _STATEMENT_REPLACEMENT)
            )
    return sorted(mutants, key=lambda m: (m.span.line, m.span.column))


def generate_mutants(
    path: str, source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[Mutant]:
    """All mutants for `source`, each tagged ``ignore_reason`` if a ``# gdmutant: ignore`` directive
    on its line applies to it; `path` is recorded on each mutant for reporting.

    Token-swap mutants (catalog) come first, then statement-deletion mutants (appended so existing
    mutant ids/order are unchanged — NF-1)."""
    mutants = generate(path, find_sites(source, catalog), catalog) + _statement_deletions(
        path, source
    )
    directives = _ignore_directives(source)
    if not directives:
        return mutants
    return [_mark_ignored(m, directives) for m in mutants]


def is_valid_gdscript(source: str) -> bool:
    """True if `source` parses as GDScript — the NF-5 gate."""
    try:
        _parse(source)
    except LarkError:
        return False
    return True


def apply_mutant(mutant: Mutant, source: str) -> tuple[str, bool]:
    """Apply `mutant` to `source`; return ``(mutated_source, is_valid)``.

    `is_valid` is False when the mutant produces unparseable GDScript, so the engine classifies it
    as invalid and never counts it as "killed" (NF-5).
    """
    mutated = mutant.apply(source)
    return mutated, is_valid_gdscript(mutated)


#: The GDScript `Adapter` the engine injects (NF-3) — the two callables above, bundled.
ADAPTER = Adapter(generate_mutants=generate_mutants, apply_mutant=apply_mutant)
