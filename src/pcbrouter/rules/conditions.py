"""A safe subset of KiCad custom-rule conditions (``.kicad_dru`` ``(condition "...")``).

Supported (everything else is reported as *unsupported*, never guessed):

* properties ``A.NetClass``, ``A.NetName``, ``A.Type``, ``A.Layer`` (and ``B.*``)
  compared with ``==`` / ``!=``. As in KiCad (libeval ``VALUE::EqualTo``), strings
  compare **case-insensitively**, and the right operand is a wildcard pattern
  only when it is a literal containing ``*`` or ``?`` (``[`` is literal, so bus
  nets such as ``/D[0]`` match themselves; ``'/S*' == A.NetName`` is a plain
  compare). Verified against KiCad 8.0.8 DRC (tests/unit/test_rule_fidelity.py);
* ``=~`` is **not** a KiCad operator: KiCad 8 and 9 cannot compile such a
  condition and skip the rule (verified with their DRC), so the rule is
  reported as ignored-by-KiCad (:class:`KiCadSyntaxError`), never applied;
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

SUPPORTED_PROPERTIES = frozenset(
    {"NetClass", "NetName", "Type", "Layer", "Net", "Pad_Type", "Pad_Shape"}
)
#: location functions (KiCad registers the "inside" names as deprecated aliases:
#: insideCourtyard = intersectsCourtyard, insideArea = intersectsKeepout, which
#: searches areas by name exactly like intersectsArea)
_COURTYARD_FUNCS = {
    "intersectsCourtyard": "any", "insideCourtyard": "any",
    "intersectsFrontCourtyard": "front", "insideFrontCourtyard": "front",
    "intersectsBackCourtyard": "back", "insideBackCourtyard": "back",
}  # fmt: skip
_AREA_FUNCS = frozenset({"intersectsArea", "insideArea", "intersectsKeepout"})
SUPPORTED_FUNCTIONS = frozenset(
    {"hasNetclass", "isPlated", "existsOnLayer", "inDiffPair", "memberOfFootprint",
     "enclosedByArea", *_COURTYARD_FUNCS, *_AREA_FUNCS}
)  # fmt: skip
#: number of string arguments each supported function takes
_ARITY = {f: 0 if f == "isPlated" else 1 for f in SUPPORTED_FUNCTIONS}
#: features whose answer depends on per-object context (see ObjectContext)
CONTEXT_FEATURES = frozenset(
    {"memberOfFootprint", "enclosedByArea", *_COURTYARD_FUNCS, *_AREA_FUNCS,
     "Pad_Type", "Pad_Shape", "isPlated", "existsOnLayer"}
)  # fmt: skip
#: properties KiCad registers under a second spelling ("Net_Class" -> "Net Class")
_ALIASES = {"Net_Class": "NetClass"}
#: properties only pads carry (KiCad: undefined, so every comparison false, elsewhere)
_PAD_ONLY = frozenset({"Pad_Type", "Pad_Shape"})
#: property -> the only item types (ItemFacts.types()) that carry it in KiCad
ITEM_ONLY_PROPERTIES: dict[str, frozenset[str]] = {"Name": frozenset({"zone"})}

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|=~|!|\(|\)|,|\.)|"
    r"'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?P<num>-?\d+(?:\.\d+)?)|(?P<bad>\S))"
)


class ConditionError(Exception):
    """The condition uses syntax or features outside the supported subset."""


class KiCadSyntaxError(ConditionError):
    """KiCad itself cannot compile this condition, so KiCad never applies the
    rule (e.g. ``=~``, which KiCad's expression tokenizer does not know)."""


@dataclass(frozen=True, slots=True)
class ObjectContext:
    """Facts about one *existing* board object, computed from its geometry
    (:mod:`pcbrouter.geometry.context`). Footprints are ``(reference, lib_id)``.
    Borderline geometry goes into the ``*_maybe`` sets (unknown), never guessed."""

    footprint: tuple[str, str] | None = None  # parent footprint; None = board-level
    plated: bool | None = None
    pad_type: str | None = None  # KiCad "Pad Type" name
    pad_shape: str | None = None  # KiCad "Pad Shape" name
    layers: tuple[str, ...] | None = None
    court_front: frozenset[tuple[str, str]] = frozenset()  # courtyards touched
    court_back: frozenset[tuple[str, str]] = frozenset()
    court_maybe: frozenset[tuple[str, str]] = frozenset()
    areas: frozenset[str] = frozenset()  # names and uuids of zones it intersects
    areas_maybe: frozenset[str] = frozenset()
    enclosed: frozenset[str] = frozenset()  # names and uuids of zones enclosing it
    enclosed_maybe: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """What a condition may ask about one item."""

    net_name: str | None
    net_classes: tuple[str, ...]
    item_type: ItemType
    layer: str | None = None
    #: False when this call site does not know the item's net (a generic hole):
    #: net properties then evaluate to *unknown*, never to "no net".
    net_known: bool = True
    #: Plated hole? Asked of pads and holes only (vias are always plated); None
    #: = unknown.
    plated: bool | None = None
    #: Every layer the item exists on; None = unknown (tracks and copper
    #: graphics default to ``layer``).
    layers: tuple[str, ...] | None = None
    #: KiCad "Pad Type" / "Pad Shape" names (pads only); None = unknown.
    pad_type: str | None = None
    pad_shape: str | None = None
    #: Diff-pair base names of the net (see :func:`diff_pair_bases`); ``()`` = not
    #: in a pair, None = unknown (board net list not available).
    diff_pair: tuple[str, ...] | None = None
    #: Location/membership facts of an existing object; None for the item being
    #: routed (its position is not fixed), making location functions unknown.
    context: ObjectContext | None = None

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
        """Definitely true for (a, b) in this order."""
        return _eval3(self.root, a, b) is True

    def evaluate(self, a: ItemFacts, b: ItemFacts | None = None) -> bool | None:
        """Does the rule apply to the pair? KiCad tests both orders. ``None`` =
        unknown (an unsupported part, or a fact this call site does not know):
        the caller must treat the rule as possibly applying, never guess."""
        first = _eval3(self.root, a, b)
        if first is True or b is None:
            return first
        second = _eval3(self.root, b, a)
        if second is True:
            return True
        return False if (first is False and second is False) else None

    def may_match(self, a: ItemFacts, b: ItemFacts | None = None) -> bool:
        """False only when the condition is definitely false for (a, b) in either
        order (KiCad tests both); unknown parts count as possibly true."""
        return self.evaluate(a, b) is not False


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
        if tok is not None and tok[1] == "=~":
            raise KiCadSyntaxError(
                "KiCad has no '=~' operator: KiCad 8/9 cannot compile this condition "
                "and skip the rule"
            )
        comparing = tok is not None and tok[1] in ("==", "!=")
        if isinstance(left, ItemOnlyProp) and not comparing:
            return Unknown(f"{left.who}.{left.name}")
        if isinstance(left, (Call, Unknown)) and not comparing:
            return left
        if not comparing:
            raise ConditionError("only == and != comparisons are supported")
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
        nets = [x for x in (left, right) if isinstance(x, Prop) and x.name == "Net"]
        if nets and len(nets) != 2:
            # KiCad's Net is the numeric net code: only A.Net vs B.Net is meaningful here
            raise ConditionError("A.Net can only be compared with B.Net")
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
                if len(args) != _ARITY[name] or any(a == "" for a in args):
                    raise ConditionError(f"{who}.{name}() takes {_ARITY[name]} string argument(s)")
                return Call(who, name, tuple(args))
            name = _ALIASES.get(name, name)
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
class _Undefined:
    """KiCad's undefined value (a property the item does not carry): ``==`` and
    ``!=`` against it are both false (libeval ``VALUE::EqualTo`` / ``NotEqualTo``)."""


UNDEFINED = _Undefined()
type _Value = tuple[str, ...] | _Undefined | None  # None = unknown here


def _dp_split(net: str) -> tuple[str, str, str] | None:
    """``MatchDpSuffix``: (base, complement net, suffix tail) or None."""
    count, comp = 0, ""
    for ch in reversed(net):
        count += 1
        if ch.isdigit() or ch == "_":
            continue
        comp = {"+": "-", "-": "+", "N": "P", "P": "N"}.get(ch, "")
        break
    if not comp:
        return None
    base = net[: len(net) - count]
    return base, base + comp + net[len(net) - count + 1 :], net[len(net) - count :]


def diff_pair_partner(net: str | None, net_names: frozenset[str]) -> str | None:
    """The other net of *net*'s differential pair, when it exists on the board."""
    split = _dp_split(net) if net else None
    return split[1] if split is not None and split[1] in net_names else None


def diff_pair_bases(net: str | None, net_names: frozenset[str]) -> tuple[str, ...]:
    """Base names ``A.inDiffPair(name)`` matches for *net* (KiCad
    ``inDiffPairFunc`` + ``DRC_ENGINE::MatchDpSuffix``): the net ends in ``P``/``N``
    or ``+``/``-`` (optionally followed by digits/underscores), its partner net
    exists on the board, and the base (or, if the base ends in ``_``, the part
    before that last ``_``) is what the argument is matched against."""
    split = _dp_split(net) if net else None
    if split is None or split[1] not in net_names:
        return ()
    base = split[0]
    return (base, base[: base.rfind("_")]) if base.endswith("_") else (base,)


def _value3(node: Prop | Literal, a: ItemFacts, b: ItemFacts | None) -> _Value:
    if isinstance(node, Literal):
        return (node.value,)
    item = a if node.who == "A" else b
    if item is None:
        return UNDEFINED  # refers to B in a single-item check: KiCad treats it as no match
    name = node.name
    if name in _PAD_ONLY:
        if item.item_type is not ItemType.PAD:
            return UNDEFINED
        value = item.pad_type if name == "Pad_Type" else item.pad_shape
        return None if value is None else (value,)
    if name in ("NetClass", "NetName", "Net") and not item.net_known:
        return None
    if name == "NetClass":
        return item.net_classes or ("Default",)
    if name in ("NetName", "Net"):
        return (item.net_name or "",)
    if name == "Type":
        return item.types()
    return (item.layer or "",)


def _item_layers(item: ItemFacts) -> tuple[str, ...] | None:
    if item.layers is not None:
        return item.layers
    if item.item_type in (ItemType.TRACK, ItemType.GRAPHIC) and item.layer:
        return (item.layer,)
    return None


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _fp_matches(selector: str, fp: tuple[str, str]) -> bool:
    """KiCad ``testFootprintSelector``: reference ``.Matches(sel)``, or the library
    id when the selector contains ':'."""
    ref, lib = fp
    return wild_compare(selector, ref, case_sensitive=True) or (
        ":" in selector and wild_compare(selector, lib, case_sensitive=True)
    )


def _area_matches(selector: str, names: frozenset[str]) -> bool:
    """KiCad ``searchAreas``: a zone uuid (exact) or zone names ``.Matches(sel)``."""
    if _UUID_RE.match(selector):
        return selector in names
    return any(wild_compare(selector, n, case_sensitive=True) for n in names)


def _location3(node: Call, item: ItemFacts) -> bool | None:
    sel = node.args[0]
    ctx = item.context
    if node.name == "memberOfFootprint":  # memberOfFootprintFunc: parent footprint
        if ctx is None:
            return False if item.item_type in (ItemType.TRACK, ItemType.VIA) else None
        return ctx.footprint is not None and _fp_matches(sel, ctx.footprint)
    if ctx is None:
        return None  # the routed item: its location is not fixed
    if node.name in _COURTYARD_FUNCS:
        if sel in ("A", "B"):
            return False  # the other item of the pair would have to be a footprint
        side = _COURTYARD_FUNCS[node.name]
        sure = (ctx.court_front if side != "back" else frozenset()) | (
            ctx.court_back if side != "front" else frozenset()
        )
        if sel.upper().startswith("${CLASS:"):  # component classes are not read
            return None if sure or ctx.court_maybe else False
        if any(_fp_matches(sel, fp) for fp in sure):
            return True
        return None if any(_fp_matches(sel, fp) for fp in ctx.court_maybe) else False
    if sel in ("A", "B"):
        return None  # "the other item is the area": zones as pair members
    if node.name == "enclosedByArea":
        if _area_matches(sel, ctx.enclosed):
            return True
        return None if _area_matches(sel, ctx.enclosed_maybe) else False
    if _area_matches(sel, ctx.areas):
        return True
    return None if _area_matches(sel, ctx.areas_maybe) else False


def _call3(node: Call, a: ItemFacts, b: ItemFacts | None) -> bool | None:
    item = a if node.who == "A" else b
    if item is None:
        return False
    if (
        node.name in _COURTYARD_FUNCS
        or node.name in _AREA_FUNCS
        or node.name
        in (
            "memberOfFootprint",
            "enclosedByArea",
        )
    ):
        return _location3(node, item)
    if node.name == "isPlated":  # isPlatedFunc: PTH pad or via
        if item.item_type is ItemType.VIA:
            return True
        if item.item_type in (ItemType.PAD, ItemType.HOLE):
            return item.plated
        return False
    if node.name == "existsOnLayer":  # any layer whose name .Matches(arg) is in the set
        layers = _item_layers(item)
        if layers is None:
            return None
        return any(wild_compare(node.args[0], lyr, case_sensitive=True) for lyr in layers)
    if not item.net_known:
        return None
    if node.name == "inDiffPair":
        if item.item_type is ItemType.GRAPHIC:
            return False  # not a connected item: no net, no pair
        if item.diff_pair is None:
            return None
        return any(wild_compare(node.args[0], base, case_sensitive=True) for base in item.diff_pair)
    # hasNetclass: NETCLASS::ContainsNetclassWithName, constituent name .Matches(arg)
    classes = item.net_classes or ("Default",)
    return any(wild_compare(arg, c, case_sensitive=True) for c in classes for arg in node.args)


def _match(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    # Layer names: wildcard either way (KiCad matches layer patterns separately).
    return any(fnmatchcase(lv, rv) or fnmatchcase(rv, lv) for lv in left for rv in right)


def wild_compare(pattern: str, text: str, case_sensitive: bool = False) -> bool:
    """KiCad's ``WildCompareString(pattern, text, case_sensitive)`` (also what
    ``wxString::Matches`` does, case-sensitively): ``*`` and ``?`` are the only
    wildcards (``[`` is an ordinary character)."""
    wild, s = (pattern, text) if case_sensitive else (pattern.upper(), text.upper())
    w = i = 0
    star_w = star_i = -1
    while i < len(s):
        if w < len(wild) and wild[w] == "*":
            star_w, star_i = w, i
            w += 1
        elif w < len(wild) and (wild[w] == "?" or wild[w] == s[i]):
            w += 1
            i += 1
        elif star_w >= 0:
            w = star_w + 1
            star_i += 1
            i = star_i
        else:
            return False
    while w < len(wild) and wild[w] == "*":
        w += 1
    return w == len(wild)


def _string_equal(left: tuple[str, ...], right: tuple[str, ...], right_pattern: bool) -> bool:
    """KiCad ``VALUE::EqualTo`` for strings: the right operand is a wildcard pattern
    only when it is a literal containing ``*`` or ``?``; otherwise the strings
    compare case-insensitively (``IsSameAs(b, false)``)."""
    if right_pattern:
        return any(wild_compare(rv, lv) for lv in left for rv in right)
    return any(lv.upper() == rv.upper() for lv in left for rv in right)


def _compare3(node: Compare, a: ItemFacts, b: ItemFacts | None) -> bool | None:
    left, right = _value3(node.left, a, b), _value3(node.right, a, b)
    if isinstance(left, _Undefined) or isinstance(right, _Undefined):
        return False
    if left is None or right is None:
        return None
    names = {x.name for x in (node.left, node.right) if isinstance(x, Prop)}
    if "Net" in names:
        same = bool(set(left) & set(right))  # net codes: exact identity
    elif "Layer" in names:
        same = _match(left, right)
    else:
        pattern = isinstance(node.right, Literal) and any(c in node.right.value for c in "*?")
        same = _string_equal(left, right, pattern)
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
    if isinstance(node, Call):
        return _call3(node, a, b)
    return _compare3(node, a, b)
