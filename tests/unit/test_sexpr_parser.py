from __future__ import annotations

import pytest

from pcbrouter.kicad.errors import MalformedBoardError
from pcbrouter.kicad.parser import MAX_DEPTH, SNode, parse_sexpr


def test_basic_structure() -> None:
    root = parse_sexpr('(kicad_pcb (version 20240108) (net 1 "GND") (flag))')
    assert root.name == "kicad_pcb"
    assert root.value("version") == "20240108"
    net = root.first("net")
    assert net is not None and net.atoms() == ["1", "GND"]
    assert [n.name for n in root.nodes()] == ["version", "net", "flag"]


def test_quoted_strings_and_escapes() -> None:
    root = parse_sexpr(r'(a "with space" "quote \" inside" "back\\slash" "new\nline" "")')
    assert root.atoms() == ["with space", 'quote " inside', "back\\slash", "new\nline", ""]


def test_parentheses_inside_strings_are_text() -> None:
    root = parse_sexpr('(net 5 "Net-(U1-Pad4)")')
    assert root.atoms() == ["5", "Net-(U1-Pad4)"]


def test_quoted_name_is_allowed() -> None:
    root = parse_sexpr('("0" "F.Cu" signal)')
    assert root.name == "0"


def test_multiline_and_tabs() -> None:
    root = parse_sexpr("(a\n\t(b 1)\r\n\t(c 2)\n)")
    assert root.value("b") == "1" and root.value("c") == "2"


def test_has_flag_supports_old_and_new_styles() -> None:
    old = parse_sexpr("(footprint x locked (layer F.Cu))")
    new = parse_sexpr("(footprint x (locked yes))")
    no = parse_sexpr("(footprint x (locked no))")
    none = parse_sexpr("(footprint x)")
    assert old.has_flag("locked") and new.has_flag("locked")
    assert not no.has_flag("locked") and not none.has_flag("locked")


def test_missing_values_return_none() -> None:
    root = parse_sexpr("(a (b))")
    assert root.value("missing") is None
    assert root.value("b") is None
    assert root.atom(3) is None
    assert isinstance(root.first("b"), SNode)


@pytest.mark.parametrize(
    ("text", "fragment", "line"),
    [
        ("(a (b 1)\n", "missing ')'", 1),
        ("(a (b 1)))", "unbalanced ')'", 1),
        ('(a "unterminated\n)', "unterminated string", 1),
        ("(a)\n(b)", "after the end", 2),
        ("(a ())", "empty list", 1),
        ("((a))", "starts with another list", 1),
        ("x (a)", "outside of any list", 1),
    ],
)
def test_malformed_input_reports_location(text: str, fragment: str, line: int) -> None:
    with pytest.raises(MalformedBoardError) as info:
        parse_sexpr(text)
    assert fragment in str(info.value)
    assert info.value.line == line
    assert info.value.column is not None


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_empty_input(text: str) -> None:
    with pytest.raises(MalformedBoardError, match="empty"):
        parse_sexpr(text)


def test_nesting_limit() -> None:
    deep = "(a " * (MAX_DEPTH + 1) + ")" * (MAX_DEPTH + 1)
    with pytest.raises(MalformedBoardError, match="too deep"):
        parse_sexpr(deep)
