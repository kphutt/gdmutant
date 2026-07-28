"""Tests for the GDScript adapter's mutation half."""

from gdmutant.adapters.gdscript import (
    apply_mutant,
    find_sites,
    generate_mutants,
    is_valid_gdscript,
    unknown_ignore_operators,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import TableOperator


def test_find_sites_locates_operators_and_literals() -> None:
    sites = find_sites("func f(a, b):\n\treturn a > b and b >= 0\n")
    assert {s.token for s in sites} == {">", "and", ">=", "0"}


def test_find_sites_ignores_strings_and_comments() -> None:
    # gdtoolkit doesn't tokenize inside strings/comments, so the operators there are never sites.
    src = 'func f(a, b):\n\tvar s := "a > b and 5"\n\t# c > d or 7\n\treturn a > b\n'
    sites = find_sites(src)
    assert [(s.token, s.span.line) for s in sites] == [(">", 4)]  # only the real one, on line 4


def test_generate_mutants_records_path_and_every_mutant_is_valid() -> None:
    src = "func f(a, b):\n\treturn a > b and b >= 0\n"
    mutants = generate_mutants("f.gd", src)
    assert mutants and all(m.path == "f.gd" for m in mutants)
    for m in mutants:
        _mutated, valid = apply_mutant(m, src)
        assert valid, f"catalog mutant should be valid GDScript: {m}"


def test_find_sites_pins_duplicate_tokens_in_document_order() -> None:
    # Two '>' on one line must yield TWO distinct sites (a dedup-by-value bug would silently drop
    # the second, under-generating mutants — the product's core output). Sites come back in
    # document (left-to-right) order.
    sites = find_sites("func f(a, b, c, d):\n\treturn a > b and c > d\n")
    assert [(s.token, s.span.column) for s in sites] == [(">", 11), ("and", 15), (">", 21)]


def test_bare_ignore_marks_every_operator_on_the_line_with_its_reason() -> None:
    # A bare `# gdmutant: ignore` marks EVERY mutant on the line as ignored — *generated* (not
    # dropped), carrying the trailing text as the reason; other lines are untouched. find_sites no
    # longer filters, so the mutant is still produced and then tagged in generate_mutants.
    src = "func f(a, b):\n\treturn a > b  # gdmutant: ignore boundary equivalent\n\treturn a < b\n"
    reason = {(m.span.line, m.original): m.ignore_reason for m in generate_mutants("f.gd", src)}
    assert reason[(2, ">")] == "boundary equivalent"  # marked, reason captured
    assert reason[(3, "<")] is None  # a different line is untouched
    assert {s.token for s in find_sites(src)} == {">", "<"}  # both still located


def test_operator_scoped_ignore_marks_only_the_named_operator() -> None:
    # The point here: `ignore[comparison]` suppresses only the comparison mutant on the line;
    # the numeric mutants on the SAME line (the `0`) stay active — line-level was too coarse.
    src = "func f(value):\n\tif value < 0:  # gdmutant: ignore[comparison]\n\t\treturn 0\n"
    marked = {
        (m.operator_id, m.replacement): m.ignore_reason
        for m in generate_mutants("f.gd", src)
        if m.span.line == 2
    }
    assert marked[("comparison", "<=")] == ""  # ignored (no reason given)
    assert marked[("numeric", "1")] is None and marked[("numeric", "-1")] is None  # still active


def test_operator_scoped_ignore_accepts_a_comma_list_and_reason() -> None:
    src = (
        "func f(value):\n"
        "\tif value < 0:  # gdmutant: ignore[comparison, numeric] both equivalent\n\t\treturn 0\n"
    )
    reason = {
        (m.operator_id, m.replacement): m.ignore_reason
        for m in generate_mutants("f.gd", src)
        if m.span.line == 2
    }
    assert reason[("comparison", "<=")] == "both equivalent"
    assert reason[("numeric", "1")] == "both equivalent"


def test_bare_ignore_is_line_scoped_not_statement_scoped() -> None:
    # Line-scoped, like # noqa (docs/decisions/0004, 0006): on a condition wrapped across lines, a
    # bare marker on line 4 marks only line 4's mutants; line 3's '>' stays active.
    src = "func f(a, b):\n\tif (\n\t\ta > b\n\t\tand a < b  # gdmutant: ignore\n\t):\n\t\tpass\n"
    marks = {(m.span.line, m.original): m.ignore_reason for m in generate_mutants("f.gd", src)}
    assert marks[(3, ">")] is None  # line 3 not marked
    assert marks[(4, "and")] == "" and marks[(4, "<")] == ""  # line 4 (marked) suppressed


def test_unknown_ignore_operator_is_a_no_op_and_reported() -> None:
    # A typo'd operator name matches nothing (suppresses nothing) but is surfaced by
    # unknown_ignore_operators so the CLI can warn instead of silently doing nothing.
    src = "func f(a, b):\n\treturn a > b  # gdmutant: ignore[comparson]\n"  # typo
    assert all(m.ignore_reason is None for m in generate_mutants("f.gd", src))  # nothing suppressed
    assert unknown_ignore_operators(src) == [(2, "comparson")]


def test_empty_ignore_brackets_are_reported_but_a_bare_marker_is_not() -> None:
    # `ignore[]` (empty brackets, malformed) is surfaced with an empty name so the CLI can warn; a
    # bare `# gdmutant: ignore` (well-formed — suppresses the whole line) is NOT reported.
    src = (
        "func f(a, b):\n\treturn a > b  # gdmutant: ignore[]\n\treturn a < b  # gdmutant: ignore\n"
    )
    assert unknown_ignore_operators(src) == [(2, "")]  # only the empty brackets on line 2
    marks = {m.span.line: m.ignore_reason for m in generate_mutants("f.gd", src)}
    assert marks[2] is None and marks[3] == ""  # empty-brackets suppresses nothing; bare suppresses


def _stmt_deletions(src: str) -> dict[tuple[int, str], object]:
    return {
        (m.span.line, m.original): m
        for m in generate_mutants("f.gd", src)
        if m.operator_id == "statement-deletion"
    }


def test_statement_deletion_replaces_an_expr_statement_with_pass() -> None:
    # A call/assignment statement is deleted (replaced with `pass`) — always safe (still compiles).
    # Applying it keeps valid GDScript with indentation preserved.
    src = "func f(x):\n\tx.foo()\n\treturn x\n"
    d = _stmt_deletions(src)
    assert (2, "x.foo()") in d
    mutated, valid = apply_mutant(d[(2, "x.foo()")], src)  # type: ignore[arg-type]
    assert valid and mutated.split("\n")[1] == "\tpass"


def test_statement_deletion_skips_a_typed_functions_sole_return() -> None:
    # Deleting the only return of a typed function -> `func f() -> bool: pass` -> Godot "not all
    # paths return a value", which gdtoolkit misses. The generation-time guard must never emit it.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    assert not _stmt_deletions(src)


def test_statement_deletion_emits_returns_in_untyped_and_void_functions() -> None:
    # Untyped and `-> void` functions never require a return value, so their returns are deletable.
    assert (2, "return a") in _stmt_deletions("func f(a):\n\treturn a\n")
    assert (2, "return") in _stmt_deletions("func f() -> void:\n\treturn\n")


def test_statement_deletion_emits_an_early_return_backstopped_by_a_final_return() -> None:
    # A typed function ending in a top-level `return` still returns a value if an EARLIER return is
    # deleted, so the early one is emitted; the guaranteed final one is not.
    d = _stmt_deletions("func f(a) -> int:\n\tif a < 0:\n\t\treturn 0\n\treturn a\n")
    assert (3, "return 0") in d  # early return, backstopped -> emitted
    assert (4, "return a") not in d  # final return -> skipped


def test_statement_deletion_skips_all_returns_when_no_final_return_backstops() -> None:
    # A typed function whose last top-level statement is an if/else (not a return) has no guaranteed
    # fall-through return, so deleting either branch's return would break compilation -> skip both.
    src = "func f(a) -> int:\n\tif a > 0:\n\t\treturn 1\n\telse:\n\t\treturn 2\n"
    assert not _stmt_deletions(src)


def test_statement_deletion_skips_a_multi_line_statement() -> None:
    # spans.py edits a single line, so a statement wrapped across lines is skipped (single-line
    # neighbours still covered). The multi-line print(...) is skipped; the untyped return isn't.
    src = "func f(a, b):\n\tprint(\n\t\ta,\n\t\tb,\n\t)\n\treturn a\n"
    d = _stmt_deletions(src)
    assert not any(orig.startswith("print") for _line, orig in d)  # multi-line call skipped
    assert (6, "return a") in d  # the single-line return IS emitted


def test_statement_deletion_treats_a_lambda_as_its_own_scope() -> None:
    # A lambda's return belongs to the lambda (untyped -> safe to delete), not the enclosing typed
    # function; the enclosing function's own final return stays skipped.
    d = _stmt_deletions("func f() -> int:\n\tvar g := func(): return 9\n\treturn g.call()\n")
    assert (2, "return 9") in d  # the lambda's return -> emitted
    assert (3, "return g.call()") not in d  # the typed function's final return -> skipped


def test_statement_deletion_skips_a_typed_lambdas_sole_return() -> None:
    # A lambda_header carries the same `-> TYPE_HINT` as a function, so a TYPED lambda's return is
    # a return-value requirement too: deleting `func() -> int: return 9` -> `pass` is the same Godot
    # "not all code paths return a value" error (verified live via --check-only). The guard must
    # treat the typed lambda exactly like a typed function and never emit its sole return.
    d = _stmt_deletions("func outer() -> void:\n\tvar g := func() -> int:\n\t\treturn 9\n")
    assert (3, "return 9") not in d  # typed lambda's sole return -> skipped
    # The enclosing `-> void` function requires no return value, so its own body is unaffected here.


def test_statement_deletion_skips_a_typed_lambda_return_with_no_backstop() -> None:
    # A typed lambda whose body ends in an if/else (no guaranteed fall-through return) must skip
    # both branch returns, mirroring the typed-function no-backstop case.
    src = (
        "func outer() -> void:\n"
        "\tvar g := func(a) -> int:\n"
        "\t\tif a > 0:\n"
        "\t\t\treturn 1\n"
        "\t\telse:\n"
        "\t\t\treturn 2\n"
    )
    d = _stmt_deletions(src)
    assert (4, "return 1") not in d and (6, "return 2") not in d


def test_find_sites_and_generate_thread_a_custom_catalog_through() -> None:
    # A custom operator that mutates a token the DEFAULT catalog ignores (the identifier `b`).
    # Both find_sites and generate_mutants must use THIS catalog — a mutant that falls back to the
    # default would never surface `b`, so it finds nothing here.
    custom = (TableOperator("ident", {"b": ("c",)}),)
    src = "func f(a, b) -> bool:\n\treturn b > a\n"
    assert {s.token for s in find_sites(src, catalog=custom)} == {"b"}
    mutants = generate_mutants("f.gd", src, catalog=custom)
    assert mutants and {(m.original, m.replacement) for m in mutants} == {("b", "c")}


def test_negative_literal_is_located_and_mutated() -> None:
    # gdtoolkit tokenizes -5 as a single NUMBER token; the numeric operator must still bump it.
    src = "func f():\n\treturn -5\n"
    assert any(s.token == "-5" for s in find_sites(src))
    mutants = generate_mutants("f.gd", src)
    assert any(m.operator_id == "numeric" and m.original == "-5" for m in mutants)


def test_is_valid_gdscript() -> None:
    assert is_valid_gdscript("func f():\n\treturn 1\n") is True
    assert is_valid_gdscript("func f(:\n") is False


def test_apply_mutant_flags_invalid_mutant_nf5() -> None:
    # Real catalog swaps stay valid, so hand-build a mutant whose replacement breaks syntax to
    # exercise the NF-5 gate directly (an invalid mutant must report is_valid=False, never killed).
    src = "func f(a, b):\n\treturn a > b\n"
    site = next(s for s in find_sites(src) if s.token == ">")
    broken = Mutant("f.gd", site.span, "comparison", ">", ")")
    mutated, valid = apply_mutant(broken, src)
    assert "a ) b" in mutated
    assert valid is False


def test_new_operators_generate_valid_mutants() -> None:
    # Compound assignment, modulo, and unary-not removal must all be found as sites and produce
    # parseable GDScript. The `not` case is the subtle one: it's a swap to "" (a token deletion),
    # so verify it doesn't leave broken syntax behind.
    src = (
        "func f(e, s, x, alive):\n\te += s\n\tvar r = x % 2\n"
        "\tif not alive:\n\t\treturn 0\n\treturn e\n"
    )
    by_op: dict[str, set[tuple[str, str]]] = {}
    for m in generate_mutants("f.gd", src):
        _, valid = apply_mutant(m, src)
        assert valid, f"{m.operator_id} {m.original!r}->{m.replacement!r} produced invalid GDScript"
        by_op.setdefault(m.operator_id, set()).add((m.original, m.replacement))
    assert by_op["compound-assign"] == {("+=", "-=")}
    assert by_op["modulo"] == {("%", "*"), ("%", "/")}
    assert by_op["logical-not"] == {("not", "")}  # deletion, and the result still parses


def test_arithmetic_modulo_is_a_mutation_site() -> None:
    # A real arithmetic `%` must be found and produce modulo mutants.
    src = "func f(a, b):\n\treturn a % b\n"
    assert any(s.token == "%" for s in find_sites(src))
    assert any(m.operator_id == "modulo" for m in generate_mutants("f.gd", src))


def test_string_format_percent_is_not_a_mutation_site() -> None:
    # GDScript overloads `%` for string formatting; a `%` whose left operand is a string literal is
    # formatting, not modulo, so it must NOT be mutated (double/single/triple-quoted forms).
    for src in (
        'func f(x):\n\treturn "Hi %s" % x\n',
        "func f(x):\n\treturn 'Hi %s' % x\n",
        'func f(x):\n\treturn """Hi %s""" % x\n',
    ):
        assert not any(s.token == "%" for s in find_sites(src)), src
        assert not any(m.operator_id == "modulo" for m in generate_mutants("f.gd", src)), src


def test_modulo_after_a_non_string_operand_is_still_a_site() -> None:
    # The skip is specific to a *string* left operand — `x % 2` (name on the left) stays a site,
    # so genuine modulo isn't over-suppressed just because a string appears elsewhere on the line.
    src = 'func f(x):\n\treturn x % 2 + len("s")\n'
    assert any(s.token == "%" for s in find_sites(src))


def test_modulo_on_a_string_keyed_index_is_still_a_site() -> None:
    # `d["k"] % x` is genuine modulo on a dict/array value — a common real pattern (`state["frame"]
    # % 2`). The token immediately before `%` is the index string "k", but its *left operand* is the
    # `d[...]` subscript, so the tree-based check must keep it (a token-adjacency check dropped it).
    src = 'func f(d, x):\n\treturn d["k"] % x\n'
    assert any(s.token == "%" for s in find_sites(src)), "genuine modulo site was skipped"
    assert any(m.operator_id == "modulo" for m in generate_mutants("f.gd", src))


def test_modulo_on_a_parenthesised_expression_ending_in_a_string_is_still_a_site() -> None:
    # Same class as the subscript case: the left operand is a ternary, not a bare string literal,
    # even though its last token is a string — must stay a modulo site.
    src = 'func f(c, x):\n\treturn (x if c else "b") % 2\n'
    assert any(s.token == "%" for s in find_sites(src))


def test_computed_string_before_percent_is_a_known_scope_limitation() -> None:
    # A computed string (parenthesised `+`-concatenation with a string-literal operand) is now
    # recognised as string-format, closing the scope limitation this test used to pin: its `%` is
    # skipped rather than mutated as modulo.
    src = 'func f(name, x):\n\treturn ("Hi " + name) % x\n'
    assert not any(s.token == "%" for s in find_sites(src))


def test_computed_string_concatenation_can_have_the_literal_on_either_side() -> None:
    # The string-literal operand that signals concatenation may be first, last, or in the middle —
    # the check isn't positional.
    for src in (
        'func f(name, x):\n\treturn (name + "!") % x\n',
        'func f(a, b, x):\n\treturn (a + "-" + b) % x\n',
    ):
        assert not any(s.token == "%" for s in find_sites(src)), src


def test_arithmetic_concatenation_with_a_minus_is_still_a_genuine_site() -> None:
    # A `-` anywhere in the parenthesised expression means arithmetic, not string-building, even
    # though a `+` is also present — must NOT be newly suppressed.
    src = "func f(a, b, c, x):\n\treturn (a + b - c) % x\n"
    assert any(s.token == "%" for s in find_sites(src)), "genuine modulo site was skipped"


def test_numeric_concatenation_without_any_string_literal_is_still_a_genuine_site() -> None:
    # A `+`-only parenthesised expression with no string literal anywhere is ordinary numeric
    # addition — must NOT be newly suppressed just because it's shaped like concatenation.
    src = "func f(a, b, x):\n\treturn (a + b) % x\n"
    assert any(s.token == "%" for s in find_sites(src)), "genuine modulo site was skipped"


def test_single_element_array_with_a_concatenation_is_still_a_genuine_site() -> None:
    # `[a + "b"] % x` has a string-concatenation *inside* it, but the node directly left of `%` is
    # the array literal `[...]`, not a parenthesised expression — recognition is specific to
    # parentheses (a real ``%`` string-format), so this must stay a genuine site.
    src = 'func f(a, x):\n\treturn [a + "b"] % x\n'
    assert any(s.token == "%" for s in find_sites(src)), "genuine modulo site was skipped"


def test_string_concatenation_plus_is_not_a_mutation_site() -> None:
    # GDScript's String defines no `-`, so `+`->`-` on concatenation can only error or sit unrun —
    # never measure a test gap. It must be skipped at generation time rather than reported.
    src = 'func greet(name: String) -> String:\n\treturn "Hello, " + name\n'
    assert not any(s.token == "+" for s in find_sites(src))
    assert not any(m.operator_id == "arithmetic" for m in generate_mutants("f.gd", src))


def test_numeric_addition_is_still_a_mutation_site() -> None:
    # The positive direction: genuine numeric `+` must keep producing its arithmetic mutant.
    src = "func total(a: int, b: int) -> int:\n\treturn a + b\n"
    assert any(s.token == "+" for s in find_sites(src))
    assert any(m.operator_id == "arithmetic" for m in generate_mutants("f.gd", src))


def test_every_plus_in_a_multi_operand_concatenation_is_skipped() -> None:
    # `a + "\n" + b` is one flat arith_expr with TWO `+` tokens; both are concatenation (this is
    # GUT's version_numbers.gd:123, which produced two survivors that were never real mutants).
    src = 'func f(a, b):\n\treturn a + "\\n" + b\n'
    assert not any(s.token == "+" for s in find_sites(src))


def test_a_minus_in_the_expression_keeps_the_pluses_as_genuine_sites() -> None:
    # A `-` anywhere means arithmetic, not string-building, so nothing in the expression is skipped
    # even though a string literal appears — the "never suppress a real gap" direction.
    src = 'func f(a, b, c):\n\treturn a + b - c + len("s")\n'
    assert any(s.token == "+" for s in find_sites(src))


def test_a_string_in_a_numeric_position_keeps_the_plus_as_a_genuine_site() -> None:
    # A string literal that has been *converted* or *indexed* is a numeric operand, and the parse
    # tree says so: `"5".to_int()` is a getattr_call, `d["k"]` a subscr_expr, `len("s")` a
    # standalone_call — none is a bare `string` node, so all three stay arithmetic sites.
    for src in (
        'func f():\n\treturn "5".to_int() + 3\n',
        'func f(d):\n\treturn d["k"] + 1\n',
        'func f(s):\n\treturn len("ab") + 1\n',
    ):
        assert any(site.token == "+" for site in find_sites(src)), src


def test_concatenation_skip_does_not_reach_the_string_format_percent_case() -> None:
    # The two skips are independent: `("Hi " + name) % x` has its `%` skipped as string-format AND
    # its `+` skipped as concatenation, and neither exclusion is what suppresses the other.
    src = 'func f(name, x):\n\treturn ("Hi " + name) % x\n'
    assert {s.token for s in find_sites(src)} == set()


def test_property_declaration_initializer_is_not_a_mutation_site() -> None:
    # GDScript skips the setter at initialization, so this initializer writes the backing field
    # directly — and the custom getter routes every read elsewhere, leaving the value unreadable.
    # The backing field one line up is ordinary code and must still be mutated (GUT's
    # compare_result.gd:15-16, where exactly that split showed up as killed vs survived).
    src = (
        "extends Node\n"
        "var _health: int = 100\n"
        "var health: int = 100:\n"
        "\tset(value):\n"
        "\t\t_health = value\n"
        "\tget:\n"
        "\t\treturn _health\n"
    )
    lines = {s.span.line for s in find_sites(src)}
    assert 2 in lines  # the backing field's initializer is still mutated
    assert 3 not in lines  # the property declaration's is not


def test_a_plain_variable_declaration_is_still_a_mutation_site() -> None:
    # No inline accessors at all: nothing about the store is dead, so every form keeps its site.
    for src in (
        "var health: int = 100\n",
        "var health = 100\n",
        "var health := 100\n",
    ):
        assert any(site.token == "100" for site in find_sites(src)), src


def test_a_setter_only_property_keeps_its_initializer_site() -> None:
    # The boundary the naive rule gets wrong: with no custom `get`, reads return the backing field
    # the initializer wrote, so mutating it IS observable — a real gap that must not be suppressed.
    src = "var health: int = 100:\n\tset(value):\n\t\t_other = value\n"
    assert any(s.token == "100" for s in find_sites(src))


def test_a_getter_naming_the_property_keeps_its_initializer_site() -> None:
    # Inside its own accessors the property name means the backing field, so `get: return health`
    # reads exactly what the initializer wrote — observable, and a site.
    src = (
        "var health: int = 100:\n\tset(value):\n\t\thealth = value * 2\n\tget:\n\t\treturn health\n"
    )
    assert any(s.token == "100" for s in find_sites(src))


def test_a_getter_only_property_with_an_unread_field_still_skips_its_initializer() -> None:
    # A custom `get` alone is enough for the store to be dead: reads route through the getter, and
    # no accessor names the property. The rule keys on readability, not on the setter existing.
    src = "var health: int = 100:\n\tget:\n\t\treturn _other\n"
    assert not any(s.token == "100" for s in find_sites(src))


def test_an_effectful_property_initializer_keeps_its_sites() -> None:
    # The store is dead, but *evaluating* the initializer is not, so mutating a token inside one is
    # still observable. One case per node the guard names: a plain call, a method call, a subscript
    # (which can index out of range), and an `await` (which suspends). The `await sig + 1` form is
    # the one that isolates `await_expr` — every other await initializer also contains a call.
    for src in (
        "var health = compute(3):\n\tget:\n\t\treturn _other\n",
        "var health = obj.compute(3):\n\tget:\n\t\treturn _other\n",
        "var health = TABLE[3]:\n\tget:\n\t\treturn _other\n",
        "var health = await sig + 3:\n\tget:\n\t\treturn _other\n",
    ):
        assert any(site.token == "3" for site in find_sites(src)), src


def test_an_inferred_type_property_initializer_is_skipped_too() -> None:
    # `var x := 100:` parses as `class_var_inf`, a different grammar rule from the `=` and
    # `: int =` forms — all three declare an initial value, so all three are skipped alike.
    src = "var health := 100:\n\tget:\n\t\treturn _other\n"
    assert not any(s.token == "100" for s in find_sites(src))


def test_a_whole_property_initializer_expression_is_skipped_not_just_its_literal() -> None:
    # Every token of the dead initializer goes, not only the first literal: the `+` and both
    # numbers in `2 + 3` are equally unreadable.
    src = "var health = 2 + 3:\n\tget:\n\t\treturn _other\n"
    assert find_sites(src) == []


def test_property_initializer_skip_applies_inside_an_inner_class() -> None:
    # gdtoolkit nests the declaration and its property_body_def inside `class_def`, so the
    # sibling-adjacency pairing has to hold at every tree level, not just the top one.
    src = (
        "class Inner:\n"
        "\tvar _a = 5\n"
        "\tvar a = 5:\n"
        "\t\tget:\n"
        "\t\t\treturn _a\n"
        "\t\tset(v):\n"
        "\t\t\t_a = v\n"
    )
    lines = {s.span.line for s in find_sites(src)}
    assert lines == {2}  # the backing field only


def test_consecutive_properties_are_each_paired_with_their_own_body() -> None:
    # Two properties in a row: the adjacency pairing must not let the first declaration claim the
    # second's body (or vice versa), and both backing fields must survive the skip.
    src = (
        "var _a = false\n"
        "var a = false:\n"
        "\tget:\n"
        "\t\treturn _a\n"
        "\tset(val):\n"
        "\t\t_a = val\n"
        "var _b = 30\n"
        "var b = 30:\n"
        "\tget:\n"
        "\t\treturn _b\n"
        "\tset(val):\n"
        "\t\t_b = val\n"
    )
    assert {s.span.line for s in find_sites(src)} == {1, 7}  # both backing fields, neither property


def test_a_static_property_declaration_is_read_like_an_ordinary_one() -> None:
    # `static var` nests the declaration inside an extra `static_class_var_stmt`, so the pairing
    # has to unwrap it — otherwise a static property's dead initializer would still be mutated.
    src = "static var health = 100:\n\tget:\n\t\treturn _other\n"
    assert not any(s.token == "100" for s in find_sites(src))


def test_a_property_with_no_initializer_is_left_alone() -> None:
    # `var differences :` (GUT's compare_result.gd:23) declares accessors but no initial value —
    # there is nothing to skip, and the accessor bodies are ordinary code that stays mutated.
    src = (
        "var differences:\n"
        "\tget:\n"
        "\t\treturn _differences\n"
        "\tset(val):\n"
        "\t\tif val > 0:\n"
        "\t\t\tpass\n"
    )
    assert any(s.token == ">" for s in find_sites(src))


def test_property_accessor_bodies_are_still_mutated() -> None:
    # Only the initializer is skipped. The `set`/`get` bodies are ordinary code and must keep every
    # site they have, or the skip would hide real gaps in the accessors themselves.
    src = (
        "var health: int = 100:\n"
        "\tset(value):\n"
        "\t\tif value > 0:\n"
        "\t\t\t_other = value + 1\n"
        "\tget:\n"
        "\t\treturn _other\n"
    )
    assert {s.token for s in find_sites(src)} == {">", "0", "+", "1"}  # not the `100`


def test_not_deletion_removes_the_keyword_and_stays_valid() -> None:
    # Pin the exact deletion: `if not alive:` -> `if  alive:` (the `not` token gone), still valid.
    src = "func f(alive):\n\tif not alive:\n\t\treturn 0\n\treturn 1\n"
    (mutant,) = [m for m in generate_mutants("f.gd", src) if m.operator_id == "logical-not"]
    mutated, valid = apply_mutant(mutant, src)
    assert valid is True
    assert "if  alive:" in mutated  # the `not` and only the `not` was removed
