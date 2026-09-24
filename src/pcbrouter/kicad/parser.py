"""Generic S-expression parser for KiCad files.

Strategy: KiCad's board format is a plain S-expression tree. Rather than depending
on a third-party KiCad library (the mature ones are GPL-licensed, or require
KiCad's own ``pcbnew`` module), we parse the syntax ourselves — it is small, fully
specified and easy to make defensive — and let :mod:`pcbrouter.kicad.adapter`
interpret the tree.

The parser knows nothing about PCBs. It produces :class:`SNode` objects whose
children are either nested nodes or ``str`` atoms (quoted strings are unescaped;
numbers stay as text so the adapter can convert them exactly).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from pcbrouter.kicad.errors import MalformedBoardError

# One regex pass tokenises the whole file. Order matters: whitespace, parens,
# quoted strings (with backslash escapes), then bare atoms.
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<open>\()
  | (?P<close>\))
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<atom>[^\s()"]+)
  | (?P<bad>")
    """,
    re.VERBOSE | re.DOTALL,
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)

#: Nesting deeper than this is treated as malformed (guards against recursion bombs).
MAX_DEPTH = 256


def _unescape(body: str) -> str:
    if "\\" not in body:
        return body
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), body)


@dataclass(slots=True)
class SNode:
    """A parenthesised list ``(name child child ...)``."""

    name: str
    children: list[SNode | str] = field(default_factory=list)
    offset: int = 0  # character offset in the source, for error messages

    # ------------------------------------------------------------ navigation
    def nodes(self, name: str | None = None) -> Iterator[SNode]:
        """Direct child nodes, optionally filtered by name."""
        for child in self.children:
            if isinstance(child, SNode) and (name is None or child.name == name):
                yield child

    def first(self, name: str) -> SNode | None:
        return next(self.nodes(name), None)

    def atoms(self) -> list[str]:
        """Direct atom children, in order."""
        return [c for c in self.children if isinstance(c, str)]

    def atom(self, index: int = 0) -> str | None:
        atoms = self.atoms()
        return atoms[index] if index < len(atoms) else None

    def value(self, name: str, index: int = 0) -> str | None:
        """Atom ``index`` of the first child node called ``name``: ``(name value)``."""
        node = self.first(name)
        return node.atom(index) if node is not None else None

    def has_flag(self, flag: str) -> bool:
        """True if ``flag`` appears as a bare atom child (e.g. ``locked``) or as a
        ``(flag yes)`` node (KiCad 8+ style)."""
        if flag in self.atoms():
            return True
        node = self.first(flag)
        if node is None:
            return False
        val = node.atom()
        return val is None or val in ("yes", "true")


def line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, col


def parse_sexpr(text: str) -> SNode:
    """Parse ``text`` containing exactly one top-level S-expression.

    Raises :class:`MalformedBoardError` with line/column for unbalanced parentheses,
    unterminated strings, empty input or trailing garbage.
    """
    stack: list[SNode] = []
    root: SNode | None = None
    expect_name = False

    for match in _TOKEN_RE.finditer(text):
        kind = match.lastgroup
        if kind == "ws":
            continue
        pos = match.start()
        if kind == "open":
            if root is not None and not stack:
                raise _error(text, pos, "unexpected data after the end of the board")
            if expect_name:
                raise _error(text, pos, "list starts with another list instead of a name")
            if len(stack) >= MAX_DEPTH:
                raise _error(text, pos, "nesting is too deep")
            node = SNode(name="", offset=pos)
            if stack:
                stack[-1].children.append(node)
            stack.append(node)
            expect_name = True
        elif kind == "close":
            if not stack:
                raise _error(text, pos, "unbalanced ')'")
            if expect_name:
                raise _error(text, pos, "empty list '()'")
            node = stack.pop()
            if not stack:
                root = node
        elif kind in ("str", "atom"):
            raw = match.group()
            value = _unescape(raw[1:-1]) if kind == "str" else raw
            if not stack:
                raise _error(text, pos, "data outside of any list")
            if expect_name:
                stack[-1].name = value
                expect_name = False
            else:
                stack[-1].children.append(value)
        else:  # "bad": an opening quote with no closing quote
            raise _error(text, pos, "unterminated string")

    if stack:
        raise _error(text, stack[-1].offset, "missing ')' — the file appears truncated")
    if root is None:
        raise MalformedBoardError("file is empty or contains no S-expression")
    return root


def _error(text: str, offset: int, message: str) -> MalformedBoardError:
    line, col = line_col(text, offset)
    return MalformedBoardError(message, line=line, column=col)
