"""Language-neutral operator catalog.

An `Operator` produces, for a source token, the mutated token(s) to try — each becomes one
candidate mutant when an adapter finds that token in real source and applies the swap via
`engine.spans`. The catalog covers DESIGN.md FG-2.1's token-level mutations: comparison, boolean,
arithmetic, and constant (a boolean-literal flip **and** a numeric-literal bump). Beyond the FG-2.1
minimum it also covers constructs common in real code: compound assignment (`+=`↔`-=`, `*=`↔`/=`),
modulo (`%`→`*`/`/`), and unary-`not` removal (a token-level deletion).

Two shapes implement the `Operator` protocol:

* `TableOperator` — a fixed original→replacements lookup (comparison / boolean / arithmetic /
  the boolean-literal flip).
* `NumericBumpOperator` — a *computed* rule for numeric literals. Numeric literals are an infinite
  domain, so they can't be a static table — which is why the operator interface is a protocol, not
  a single table type (Slice 3's adapter binds against this shape).

Structural statement-deletion (also FG-2.1) removes an AST statement, not a token — it needs
statement-node handling rather than a token swap, so it lives in the GDScript adapter
(`_statement_deletions`), not this token catalog, with a generation-time guard against
Godot-uncompilable deletions. See docs/decisions/0002 and 0007.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Operator(Protocol):
    """A mutation operator: it names itself and yields replacement tokens for a source token."""

    @property
    def id(self) -> str:
        """A stable identifier for this operator (recorded on each mutant / in reports)."""
        ...

    def replacements(self, token: str) -> tuple[str, ...]:
        """Replacement tokens this operator produces for `token` (empty if it doesn't apply)."""
        ...


def applies(operator: Operator, token: str) -> bool:
    """True if `operator` produces at least one mutant for `token`."""
    return len(operator.replacements(token)) > 0


@dataclass(frozen=True)
class TableOperator:
    """An operator defined by a fixed original-token → replacement-tokens table."""

    id: str
    table: Mapping[str, tuple[str, ...]]

    def replacements(self, token: str) -> tuple[str, ...]:
        """`token`'s replacements per `table`, or empty if `token` isn't one of its keys."""
        return self.table.get(token, ())


#: A decimal integer or float literal. Every part is optional so one pattern covers ``42``,
#: ``3.5``, ``.5``, ``1.``, ``1e3`` and ``1.5E+10``; "at least one digit somewhere" is checked
#: after the match instead (a bare ``.`` or ``-`` matches the pattern but is not a literal).
#: A digit run is ``[0-9]+(?:_[0-9]+)*`` so a separator must sit *between* digits, matching both
#: GDScript and Python's own rule — which also means ``int()`` can never choke on what matched.
#: ``[0-9]`` is ASCII by construction, so a non-ASCII digit (``٣``) is not a literal here.
_DECIMAL_LITERAL = re.compile(
    r"""(?P<sign>-?)
        (?P<integer>[0-9]+(?:_[0-9]+)*)?
        (?:(?P<point>\.)(?P<fraction>[0-9]+(?:_[0-9]+)*)?)?
        (?P<exponent>[eE][+-]?[0-9]+)?""",
    re.VERBOSE,
)
#: A radix-prefixed integer literal: ``0x`` hexadecimal or ``0b`` binary. Both prefixes are
#: lowercase-only because that is all GDScript has (gdtoolkit rejects ``0X1f``), and the digit run
#: carries the same separator rule as a decimal one. Whether the digits actually fit the base is
#: settled by `int` below, so ``0b1234`` falls through to "not a literal" rather than matching.
_RADIX_LITERAL = re.compile(
    r"(?P<sign>-?)0(?P<radix>[xb])(?P<digits>[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*)"
)
#: Radix marker -> (base, the `format` spec that writes a number back in that base).
_RADIX_BASES = {"x": (16, "x"), "b": (2, "b")}


def _grouping(run: str) -> int:
    """How far apart `run`'s ``_`` separators sit, counted in digits, or 0 if it has none.

    ``1_000_000`` reports 3 and ``0b1010_1010`` reports 4. Measuring the *spacing* rather than the
    separator positions is what lets a bump that changes the digit count keep a sensible shape:
    re-grouping ``1000000`` and ``999999`` by 3 gives ``1_000_001`` and ``999_999``, and each of
    those bumps back to the literal it came from.

    Spacing is only meaningful when it is uniform, so an irregular run (``1_00_000``) reports 0 and
    loses its separators when re-rendered: there is no spacing to carry over, and the ungrouped
    digits still read as the same number.
    """
    positions: list[int] = []
    digits = 0
    for character in reversed(run):
        if character == "_":
            positions.append(digits)
        else:
            digits += 1
    if not positions:
        return 0
    size = min(positions)
    return size if set(positions) == set(range(size, digits, size)) else 0


def _group_digits(digits: str, size: int) -> str:
    """`digits` with a ``_`` every `size` of them, counted from the right (`size` 0 adds none)."""
    if not size:
        return digits
    grouped: list[str] = []
    for index, character in enumerate(reversed(digits)):
        if index and index % size == 0:
            grouped.append("_")
        grouped.append(character)
    return "".join(reversed(grouped))


@dataclass(frozen=True)
class _DecimalForm:
    """How a decimal literal is *written*, so a bumped value can be re-rendered in the same shape.

    The value itself is not held here. It is passed to `render` as an integer count of the
    literal's own last decimal place, which is what makes "bump by one" mean the same thing for
    ``42`` and for ``0.016``.
    """

    #: Digits before the point, and whether they are zero-padded (``007``) so the width is kept.
    #: Padding is conditional here and unconditional for a radix literal on purpose: a leading zero
    #: in hex reads as deliberate width, whereas padding every decimal would turn the ``999_999``
    #: bump of ``1_000_000`` into ``0_999_999``.
    integer_width: int
    zero_padded: bool
    integer_grouping: int
    #: Digits after the point (the literal's precision, and so the size of one bump) and their
    #: separator spacing. The count never changes under a bump, so a fraction is re-grouped exactly.
    scale: int
    fraction_grouping: int
    #: The written ``.`` (``1.`` keeps its trailing point) and exponent suffix (``e-3``, ``E+10``),
    #: reused verbatim: bumping the mantissa leaves the exponent's meaning untouched.
    point: str
    exponent: str

    def render(self, units: int) -> str:
        """`units` (a count of the literal's last decimal place) written the way this literal is."""
        magnitude = abs(units)
        integer_digits = str(magnitude // 10**self.scale)
        fraction_digits = str(magnitude % 10**self.scale).zfill(self.scale) if self.scale else ""
        if self.zero_padded:
            integer_digits = integer_digits.zfill(self.integer_width)
        if self.integer_width == 0 and integer_digits == "0":
            integer_digits = ""  # `.5` keeps its bare form; `.9` bumped up still grows its `1`
        return (
            ("-" if units < 0 else "")
            + _group_digits(integer_digits, self.integer_grouping)
            + self.point
            + _group_digits(fraction_digits, self.fraction_grouping)
            + self.exponent
        )


@dataclass(frozen=True)
class _RadixForm:
    """How a radix-prefixed literal is written, so a bumped value can be re-rendered in the shape.

    Unlike a decimal literal the digits are always re-padded to the written width: ``0x0F`` is a
    deliberate two-nibble constant, and ``0x10`` bumped down reads better as ``0x0f`` than ``0xf``.
    Padding only ever adds, so a bump needing more digits (``0xff`` -> ``0x100``) still gets them.
    """

    prefix: str  # "0x" or "0b", as written
    spec: str  # the `format` spec that writes a value back in this base
    width: int
    uppercase: bool  # the literal writes its A-F digits uppercase, so its bumps should too
    grouping: int

    def render(self, value: int) -> str:
        """`value` written in this literal's base, prefix, width, case and grouping."""
        digits = format(abs(value), self.spec).zfill(self.width)
        if self.uppercase:
            digits = digits.upper()
        return ("-" if value < 0 else "") + self.prefix + _group_digits(digits, self.grouping)


def _decimal_bumps(match: re.Match[str]) -> tuple[str, ...]:
    """The ±1 bumps of a matched decimal literal, or empty if it holds no digits to bump."""
    integer, fraction = match["integer"] or "", match["fraction"] or ""
    integer_digits, fraction_digits = integer.replace("_", ""), fraction.replace("_", "")
    if not (integer_digits or fraction_digits):
        return ()  # a bare `.`, `-`, `e3` or empty token: the pattern allows it, GDScript doesn't
    # Read the literal as an integer count of its own last decimal place, so one bump is one unit
    # of the precision the author wrote: `42` -> `43`, `0.016` -> `0.017`, `1.5e-3` -> `1.6e-3`.
    units = int(match["sign"] + integer_digits + fraction_digits)
    if match["sign"] and units == 0:
        return ()  # `-0`/`-0.0` isn't a real negative literal; skip it (keeps bumps reversible)
    form = _DecimalForm(
        integer_width=len(integer_digits),
        zero_padded=len(integer_digits) > 1 and integer_digits.startswith("0"),
        integer_grouping=_grouping(integer),
        scale=len(fraction_digits),
        fraction_grouping=_grouping(fraction),
        point=match["point"] or "",
        exponent=match["exponent"] or "",
    )
    return (form.render(units + 1), form.render(units - 1))


def _radix_bumps(match: re.Match[str]) -> tuple[str, ...]:
    """The ±1 bumps of a matched hex/binary literal, or empty if its digits don't fit its base."""
    base, spec = _RADIX_BASES[match["radix"]]
    digits = match["digits"].replace("_", "")
    try:
        value = int(match["sign"] + digits, base)
    except ValueError:
        return ()  # e.g. `0b1234`: the pattern is base-agnostic, the base is not
    if match["sign"] and value == 0:
        return ()  # `-0x0`: same non-canonical negative zero the decimal path skips
    form = _RadixForm(
        prefix="0" + match["radix"],
        spec=spec,
        width=len(digits),
        uppercase=digits != digits.lower(),
        grouping=_grouping(match["digits"]),
    )
    return (form.render(value + 1), form.render(value - 1))


@dataclass(frozen=True)
class NumericBumpOperator:
    """Bump a numeric literal by one unit of its own last digit — FG-2.1's "bump a number" case.

    Numeric literals are an infinite domain, so this is a computed rule, not a table. gdtoolkit
    folds a leading sign into the literal's token (``-5`` is one token, not ``-`` + ``5``), so
    negative literals are handled here too.

    **One rule covers every form**: add and subtract one unit of the last digit position the author
    actually wrote. On an integer that is the familiar ±1 (``5`` -> ``6``, ``4``; ``0`` -> ``1``,
    ``-1``; ``-5`` -> ``-4``, ``-6``). On a float it is one unit of the written precision
    (``3.5`` -> ``3.6``, ``3.4``; ``0.016`` -> ``0.017``, ``0.015``). On a hex or binary literal it
    is ±1 in that literal's own base (``0x1f`` -> ``0x20``, ``0x1e``).

    Bumping a float by a whole ``1.0`` was the alternative, and it is the wrong size: a ``0.016``
    frame delta shifted to ``1.016`` is a mutant *any* test that reaches the line kills, which
    raises the score without measuring anything. One unit of the written precision is instead the
    off-by-a-bit bug a tuned constant plausibly has, so a survivor there is a real, unpinned value.

    The literal's written shape is reproduced rather than normalised, so the mutant reads like the
    code around it and is always valid source: zero padding (``007`` -> ``008``), digit separators
    (``1_000_000`` -> ``1_000_001``), a bare or trailing point (``.5`` -> ``.6``, ``1.`` -> ``2.``),
    hex digit case (``0xFF`` -> ``0x100``) and an exponent suffix (``1.5e-3`` -> ``1.6e-3``) all
    survive the bump.

    Every replacement is itself a literal this operator recognizes, so a bump can always be bumped
    back to the **value** it started from. The **spelling** comes back only while the digits still
    record it, and a bump that changes how many digits there are can erase the very thing that did.
    Four shapes, one cause: ``0xFF`` bumped up is ``0x100``, with no letters left to say it was
    written uppercase; ``099`` bumped up is ``100``, as wide as the original and so no longer
    visibly padded; ``1_00`` bumped down is ``99``, too short to place a separator in; and ``.9``
    bumped up is ``1.0``, which had to grow the integer digit the original left off. Each of those
    still bumps back to the right number, spelled the ordinary way.

    Nothing in the engine ever reads a mutation backwards (`mutants.generate` only calls
    `replacements` forward), so none of this costs anything at run time. It is stated exactly rather
    than approximately because an operator that claims more than it does is how a wrong mutant gets
    trusted.
    """

    id: str = "numeric"

    def replacements(self, token: str) -> tuple[str, ...]:
        """`token` bumped up and down by one unit of its last digit (as strings), or empty if
        `token` isn't a numeric literal, or is a non-canonical negative zero (``-0``, ``-0.0``)."""
        radix = _RADIX_LITERAL.fullmatch(token)
        if radix is not None:
            return _radix_bumps(radix)
        decimal = _DECIMAL_LITERAL.fullmatch(token)
        if decimal is None:
            return ()
        return _decimal_bumps(decimal)


COMPARISON = TableOperator(
    "comparison",
    {
        ">": (">=",),
        ">=": (">",),
        "<": ("<=",),
        "<=": ("<",),
        "==": ("!=",),
        "!=": ("==",),
    },
)
BOOLEAN = TableOperator("boolean", {"and": ("or",), "or": ("and",)})
#: Arithmetic. GDScript overloads ``+`` for string concatenation, and its ``String`` defines no
#: ``-``, so the GDScript adapter's `find_sites` skips a ``+`` whose expression has a string-literal
#: operand — the same operand typing it applies to ``%`` (see `MODULO`). It also skips a ``/`` that
#: separates the segments of a node path (``$Sprite2D/Label``), which is punctuation, not division.
#: Catalogs carry no operand-type or grammar-position information, so both judgments belong to the
#: adapter, not this table.
ARITHMETIC = TableOperator("arithmetic", {"+": ("-",), "-": ("+",), "*": ("/",), "/": ("*",)})
CONSTANT = TableOperator("constant", {"true": ("false",), "false": ("true",)})
NUMERIC = NumericBumpOperator()
#: Compound assignment — gdtoolkit tokenizes ``+=``/``-=``/``*=``/``/=`` atomically, so these are a
#: plain involutive swap. Without it, ``energy += speed`` produces **zero** mutants. GDScript
#: overloads ``+=`` for string appending and its ``String`` defines no ``-``, so the GDScript
#: adapter's `find_sites` skips a ``+=`` whose right operand is a string — the same operand typing
#: it applies to ``+`` (see `ARITHMETIC`) and ``%`` (see `MODULO`).
COMPOUND_ASSIGN = TableOperator(
    "compound-assign", {"+=": ("-=",), "-=": ("+=",), "*=": ("/=",), "/=": ("*=",)}
)
#: Modulo — swap ``%`` for ``*`` or ``/``. Directional on purpose: it targets the ``%`` the catalog
#: otherwise ignores, without adding a ``%`` mutant to every ``*``/``/`` in the codebase. GDScript
#: overloads ``%`` for string formatting, so the GDScript adapter's `find_sites` skips a ``%`` whose
#: left operand is a string literal (formatting, not modulo), and for a node's unique name, so it
#: skips the ``%`` of ``%HealthBar`` too. Not involutive (see operator tests).
MODULO = TableOperator("modulo", {"%": ("*", "/")})
#: Unary ``not`` removal — deleting the keyword flips the guarded condition, a strong mutation.
#: Modelled as a swap to the empty string (a token-level deletion); the adapter's NF-5 re-parse
#: drops any result that doesn't parse. Not involutive: ``""`` is not a token to swap back.
LOGICAL_NOT = TableOperator("logical-not", {"not": ("",)})

#: The default catalog, in a stable order (new operators appended so existing mutant ids/order are
#: unchanged — NF-1). The intermediate list widens the heterogeneous operator types to the
#: `Operator` protocol before forming the tuple.
_CATALOG: list[Operator] = [
    COMPARISON,
    BOOLEAN,
    ARITHMETIC,
    CONSTANT,
    NUMERIC,
    COMPOUND_ASSIGN,
    MODULO,
    LOGICAL_NOT,
]
CATALOG: tuple[Operator, ...] = tuple(_CATALOG)


def all_replacements(token: str, catalog: tuple[Operator, ...] = CATALOG) -> list[tuple[str, str]]:
    """Every ``(operator_id, replacement)`` candidate the catalog produces for `token`."""
    return [(op.id, repl) for op in catalog for repl in op.replacements(token)]
