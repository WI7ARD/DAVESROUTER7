"""A safe subset of KiCad custom-rule conditions (``.kicad_dru`` ``(condition "...")``).

Supported (everything else is reported as *unsupported*, never guessed):

* properties ``A.NetClass``, ``A.NetName``, ``A.Type``, ``A.Layer`` (and ``B.*``)
  compared with ``==`` / ``!=`` against string literals; ``*`` and ``?`` wildcards
  behave like KiCad's wildcard compare;
* ``A.NetName =~ 'BUS.*'``: regex *search* (substring) match, as in KiCad;
  an invalid pattern is reported as unsupported, never guessed;
* ``A.hasNetclass('X')`` (KiCad 9);
* ``&&``, ``||``, ``!`` and parentheses.

Examples: ``A.NetClass == 'HV' || B.NetClass == 'HV'``,
``A.Type == 'Via' && A.NetName == '/CAN*'``.

Conditions are parsed into a tiny AST and evaluated against item descriptors — no
``eval``, no code execution.

A condition that uses unsupported parts (e.g. ``A.insideArea('X')``) keeps its rule
*unsupported*, but :func:`parse_partial` still reads it with those parts as
``Unknown``. Three-valued (Kleene) evaluation then tells when such a rule
*cannot* apply to a pair of items: ``A.NetClass == 'HV' && A.insideArea('C*')``
is definitely false for a net outside class HV, whatever the area. Unknown parts
are never assumed false.

Some properties exist only on one kind of item. In KiCad, ``A.Name`` is
registered only by zones (``zone.cpp``; pads have "Pad Number" / "Pin Name",
tracks, vias and footprints none). For an item without the property KiCad's
evaluator yields an *undefined* value, and both ``==`` and ``!=`` against it are
false (``PCBEXPR_VAR_REF::GetValue``, ``VALUE::EqualTo`` / ``NotEqualTo``, KiCad
source at the Real100 commit). So ``A.Name == 'outer_pour'`` is definitely false
for a track, via or pad, and unknown only for zones (whose names the engine does
not track).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from pcbrouter.rules.model import ItemType

SUPPORTED_PROPERTIES = frozenset({"NetClass", "NetName", "Type", "Layer"})
SUPPORTED_FUNCTIONS = frozenset({"hasNetclass"})
#: property -> the only item types (ItemFacts.types()) that carry it in KiCad
ITEM_ONLY_PROPERTIES: dict[str, frozenset[str]] = {"Name": frozenset({"zone"})}

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|=~|!|\(|\)|,|\.)|"
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
        # KiCad spells item types lowercase in conditions (e.g. A.Type == 'track').
        if self.item_type is ItemType.TRACK:
            return ("track", "arc")
        return (self.item_type.value.lower(),)


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
    op: str  # "==" | "!=" | "=~" (regex search)
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


@dataclass(frozen=True, slots=True)
class Unknown:
    """An unsupported sub-expression (partial parses only): true or false."""

    text: str


@dataclass(frozen=True, slots=True)
class ItemOnlyProp:
    """A property only some item types carry (parser intermediate)."""

    who: str
    name: str
    kinds: frozenset[str]


@dataclass(frozen=True, slots=True)
class OnlyOn:
    """``==`` / ``!=`` on an item-only property (partial parses only): definitely
    false for items that lack the property (KiCad: undefined compares false),
    unknown for items that carry it."""

    who: str
    kinds: frozenset[str]
    text: str


type Node = Compare | Call | Not | BoolOp | Unknown | OnlyOn


@dataclass(frozen=True, slots=True)
class Condition:
    text: str
    root: Node
    uses_b: bool

    def matches(self, a: ItemFacts, b: ItemFacts | None = None) -> bool:
        return _eval(self.root, a, b)

    def may_match(self, a: ItemFacts, b: ItemFacts | None = None) -> bool:
        """False only when the condition is definitely false for (a, b) in either
        order (KiCad tests both); unknown parts count as possibly true."""
        return _eval3(self.root, a, b) is not False or (
            b is not None and _eval3(self.root, b, a) is not False
        )


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
    def __init__(self, tokens: list[tuple[str, str]], partial: bool = False) -> None:
        self.t = tokens
        self.i = 0
        self.uses_b = False
        #: unsupported properties/functions become Unknown instead of an error
        self.partial = partial

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
        tok = self.peek()
        comparing = tok is not None and tok[1] in ("==", "!=", "=~")
        if isinstance(left, ItemOnlyProp) and not comparing:
            return Unknown(f"{left.who}.{left.name}")
        if isinstance(left, (Call, Unknown)) and not comparing:
            return left
        if not comparing:
            raise ConditionError("only ==, != and =~ comparisons are supported")
        op = self.take()[1]
        right = self.operand()
        only = [x for x in (left, right) if isinstance(x, ItemOnlyProp)]
        if only:
            other = right if only[0] is left else left
            text = f"{left} {op} {right}"
            if len(only) == 1 and op in ("==", "!=") and isinstance(other, Literal):
                return OnlyOn(only[0].who, only[0].kinds, text)
            return Unknown(text)
        if isinstance(left, Unknown) or isinstance(right, Unknown):
            return Unknown(f"{left} {op} {right}")
        if isinstance(left, Call) or isinstance(right, Call):
            raise ConditionError("function calls cannot be compared")
        if op == "=~":
            if not isinstance(right, Literal):
                raise ConditionError("=~ needs a string-literal pattern")
            try:
                re.compile(right.value)
            except re.error as exc:
                raise ConditionError(f"invalid =~ pattern {right.value!r}: {exc}") from exc
        assert not isinstance(left, ItemOnlyProp) and not isinstance(right, ItemOnlyProp)
        return Compare(left, op, right)

    def operand(self) -> Prop | Literal | Call | Unknown | ItemOnlyProp:
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
                    if self.partial:
                        return Unknown(f"{who}.{name}()")
                    raise ConditionError(f"function {who}.{name}() is not supported")
                return Call(who, name, tuple(args))
            if name not in SUPPORTED_PROPERTIES:
                if self.partial and name in ITEM_ONLY_PROPERTIES:
                    return ItemOnlyProp(who, name, ITEM_ONLY_PROPERTIES[name])
                if self.partial:
                    return Unknown(f"{who}.{name}")
                raise ConditionError(f"property {who}.{name} is not supported")
            return Prop(who, name)
        raise ConditionError(f"unsupported expression {tok[1]!r}")


def parse_condition(text: str) -> Condition:
    parser = _Parser(_tokens(text))
    root = parser.parse()
    return Condition(text, root, parser.uses_b)


def parse_partial(text: str) -> Condition | None:
    """Parse with unsupported parts as ``Unknown`` (for :meth:`Condition.may_match`);
    None when even that is impossible (unreadable syntax)."""
    try:
        parser = _Parser(_tokens(text), partial=True)
        root = parser.parse()
    except ConditionError:
        return None
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
    if isinstance(node, (Unknown, OnlyOn)):  # only partial parses contain them
        raise ConditionError(f"cannot evaluate unsupported part {node.text}")
    if isinstance(node, Call):
        item = a if node.who == "A" else b
        if item is None:
            return False
        classes = item.net_classes or ("Default",)
        return any(fnmatchcase(c, arg) for c in classes for arg in node.args)
    left, right = _values(node.left, a, b), _values(node.right, a, b)
    if left is None or right is None:
        return False  # refers to B in a single-item check: KiCad treats it as no match
    if node.op == "=~":
        return any(re.search(pat, val) is not None for val in left for pat in right)
    same = _match(left, right)
    return same if node.op == "==" else not same


def _eval3(node: Node, a: ItemFacts, b: ItemFacts | None) -> bool | None:
    """Kleene three-valued evaluation: None = unknown."""
    if isinstance(node, Unknown):
        return None
    if isinstance(node, OnlyOn):
        item = a if node.who == "A" else b
        if item is None:
            return False  # refers to B in a single-item check: no match, as in _eval
        return None if node.kinds & set(item.types()) else False
    if isinstance(node, BoolOp):
        left, right = _eval3(node.left, a, b), _eval3(node.right, a, b)
        if node.op == "&&":
            if left is False or right is False:
                return False
            return True if (left and right) else None
        if left is True or right is True:
            return True
        return False if (left is False and right is False) else None
    if isinstance(node, Not):
        inner = _eval3(node.operand, a, b)
        return None if inner is None else not inner
    return _eval(node, a, b)
