"""A safe subset of KiCad custom-rule conditions (``.kicad_dru`` ``(condition "...")``).

Supported (everything else is reported as *unsupported*, never guessed):

* properties ``A.NetClass``, ``A.NetName``, ``A.Type``, ``A.Layer`` (and ``B.*``)
  compared with ``==`` / ``!=`` against string literals; ``*`` and ``?`` wildcards
  behave like KiCad's wildcard compare;
* ``A.hasNetclass('X')`` (KiCad 9);
* ``&&``, ``||``, ``!`` and parentheses.

Examples: ``A.NetClass == 'HV' || B.NetClass == 'HV'``,
``A.Type == 'Via' && A.NetName == '/CAN*'``.

Conditions are parsed into a tiny AST and evaluated against item descriptors — no
``eval``, no code execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from pcbrouter.rules.model import ItemType

SUPPORTED_PROPERTIES = frozenset({"NetClass", "NetName", "Type", "Layer"})
SUPPORTED_FUNCTIONS = frozenset({"hasNetclass"})

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|!|\(|\)|,|\.)|"
    r"'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?P<num>-?\d+(?:\.\d+)?)|(?P<bad>\S))"
)


class ConditionError(Exception):
    """The condition uses syntax or features outside the supported subset."""


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """What a condition may ask about one item."""

    net_name: str | None
    net_classes: tuple[str, ...]
    item_type: ItemType
    layer: str | None = None

    def types(self) -> tuple[str, ...]:
        if self.item_type is ItemType.TRACK:
            return ("Track", "Arc")
        return (self.item_type.value,)


# ------------------------------------------------------------------ AST
@dataclass(frozen=True, slots=True)
class Prop:
    who: str  # "A" | "B"
    name: str


@dataclass(frozen=True, slots=True)
class Literal:
    value: str


@dataclass(frozen=True, slots=True)
class Compare:
    left: Prop | Literal
    op: str  # "==" | "!="
    right: Prop | Literal


@dataclass(frozen=True, slots=True)
class Call:
    who: str
    name: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class BoolOp:
    op: str  # "&&" | "||"
    left: Node
    right: Node


type Node = Compare | Call | Not | BoolOp


@dataclass(frozen=True, slots=True)
class Condition:
    text: str
    root: Node
    uses_b: bool

    def matches(self, a: ItemFacts, b: ItemFacts | None = None) -> bool:
        return _eval(self.root, a, b)


# ------------------------------------------------------------------ parser
def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ConditionError(f"cannot read condition at {text[pos:]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind is None:
            continue
        if kind == "bad":
            raise ConditionError(f"unexpected character {m.group('bad')!r}")
        value = m.group(kind)
        out.append(("str" if kind in ("sq", "dq") else kind, value))
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t = tokens
        self.i = 0
        self.uses_b = False

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise ConditionError(f"expected {value or kind}, found {tok[1] if tok else 'end'}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        node = self.or_()
        if self.peek() is not None:
            raise ConditionError(f"unexpected {self.peek()}")
        return node

    def or_(self) -> Node:
        node = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            node = BoolOp("||", node, self.and_())
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.peek() == ("op", "&&"):
            self.take()
            node = BoolOp("&&", node, self.not_())
        return node

    def not_(self) -> Node:
        if self.peek() == ("op", "!"):
            self.take()
            return Not(self.not_())
        return self.primary()

    def primary(self) -> Node:
        if self.peek() == ("op", "("):
            self.take()
            node = self.or_()
            self.take("op", ")")
            return node
        left = self.operand()
        if isinstance(left, Call):
            return left
        tok = self.peek()
        if tok is None or tok[1] not in ("==", "!="):
            raise ConditionError("only == and != comparisons are supported")
        op = self.take()[1]
        right = self.operand()
        if isinstance(right, Call):
            raise ConditionError("function calls cannot be compared")
        return Compare(left, op, right)

    def operand(self) -> Prop | Literal | Call:
        tok = self.peek()
        if tok is None:
            raise ConditionError("unexpected end of condition")
        if tok[0] == "str":
            self.take()
            return Literal(tok[1])
        if tok[0] == "num":
            raise ConditionError("numeric comparisons are not supported")
        if tok[0] == "ident" and tok[1] in ("A", "B"):
            who = self.take()[1]
            if who == "B":
                self.uses_b = True
            self.take("op", ".")
            name = self.take("ident")[1]
            if self.peek() == ("op", "("):
                self.take()
                args: list[str] = []
                while self.peek() != ("op", ")"):
                    arg = self.take()
                    if arg[0] != "str":
                        raise ConditionError(f"unsupported argument {arg[1]!r} to {name}()")
                    args.append(arg[1])
                    if self.peek() == ("op", ","):
                        self.take()
                self.take("op", ")")
                if name not in SUPPORTED_FUNCTIONS:
                    raise ConditionError(f"function {who}.{name}() is not supported")
                return Call(who, name, tuple(args))
            if name not in SUPPORTED_PROPERTIES:
                raise ConditionError(f"property {who}.{name} is not supported")
            return Prop(who, name)
        raise ConditionError(f"unsupported expression {tok[1]!r}")


def parse_condition(text: str) -> Condition:
    parser = _Parser(_tokens(text))
    root = parser.parse()
    return Condition(text, root, parser.uses_b)


# ------------------------------------------------------------------ evaluation
def _values(node: Prop | Literal, a: ItemFacts, b: ItemFacts | None) -> tuple[str, ...] | None:
    if isinstance(node, Literal):
        return (node.value,)
    item = a if node.who == "A" else b
    if item is None:
        return None
    if node.name == "NetClass":
        return item.net_classes or ("Default",)
    if node.name == "NetName":
        return (item.net_name or "",)
    if node.name == "Type":
        return item.types()
    return (item.layer or "",)


def _match(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    # KiCad compares strings with wildcards (either side may carry the pattern).
    return any(fnmatchcase(lv, rv) or fnmatchcase(rv, lv) for lv in left for rv in right)


def _eval(node: Node, a: ItemFacts, b: ItemFacts | None) -> bool:
    if isinstance(node, BoolOp):
        if node.op == "&&":
            return _eval(node.left, a, b) and _eval(node.right, a, b)
        return _eval(node.left, a, b) or _eval(node.right, a, b)
    if isinstance(node, Not):
        return not _eval(node.operand, a, b)
    if isinstance(node, Call):
        item = a if node.who == "A" else b
        if item is None:
            return False
        classes = item.net_classes or ("Default",)
        return any(fnmatchcase(c, arg) for c in classes for arg in node.args)
    left, right = _values(node.left, a, b), _values(node.right, a, b)
    if left is None or right is None:
        return False  # refers to B in a single-item check: KiCad treats it as no match
    same = _match(left, right)
    return same if node.op == "==" else not same
