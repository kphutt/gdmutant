"""Language-neutral source-span editing.

A `Span` is a half-open `[start, end)` region of source text in **1-indexed** (line, column)
coordinates — the coordinate system gdtoolkit/lark report for tokens (a single-character `>` at
column 7 has `column=7`, `end_column=8`). Adapters locate spans via their AST; the engine applies
the replacement here. See docs/decisions/0002.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A half-open `[start, end)` region of source, 1-indexed line and column (end exclusive)."""

    line: int
    column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        if self.line < 1 or self.column < 1:
            raise ValueError(f"span start must be 1-indexed and positive: {self!r}")
        if self.end_line < 1 or self.end_column < 1:
            raise ValueError(f"span end must be 1-indexed and positive: {self!r}")
        if (self.end_line, self.end_column) < (self.line, self.column):
            raise ValueError(f"span end precedes start: {self!r}")


# Split source lines ONLY on "\n", matching gdtoolkit/lark's line counter
# (lark.lexer.LineCounter uses newline_char="\n"). Python's str.splitlines() also treats
# vertical tab, form feed, \x1c-\x1e, NEL (\x85), and U+2028/U+2029 — and a lone \r — as line
# boundaries; any of those before the mutated line would desync our line numbers from the
# parser's and edit the wrong line (docs/decisions/0002 / NF-5). A trailing \r (CRLF) stays
# in the line's content, exactly as the parser sees it.
_NEWLINE = "\n"


def apply_replacement(source: str, span: Span, replacement: str) -> str:
    """Return `source` with the text in `span` replaced by `replacement`.

    Only single-line spans are supported — mutation operators act within one line. Lines are
    counted by literal ``\\n`` only, matching gdtoolkit/lark, so token positions from the parser
    line up with the edit. The replaced region must fall within that line's content.

    Raises:
        ValueError: if the span covers more than one line.
        IndexError: if the line is out of range, or the columns fall outside the line's content.
    """
    if span.line != span.end_line:
        raise ValueError(f"multi-line spans are not supported: {span!r}")

    lines = source.split(_NEWLINE)
    if not (1 <= span.line <= len(lines)):
        raise IndexError(f"line {span.line} out of range (source has {len(lines)} lines)")

    line = lines[span.line - 1]
    start = span.column - 1
    end = span.end_column - 1
    if not (0 <= start <= end <= len(line)):
        raise IndexError(
            f"columns {span.column}..{span.end_column} out of range for a line of "
            f"{len(line)} characters"
        )

    lines[span.line - 1] = line[:start] + replacement + line[end:]
    return _NEWLINE.join(lines)
