from __future__ import annotations

from decimal import Decimal

import pytest

from pcbrouter.domain.units import (
    NM_PER_MM,
    UnitConversionError,
    format_mm,
    internal_to_mm,
    mil_to_internal,
    mm_to_internal,
    parse_mm,
)


def test_mm_to_internal_is_integer_nanometres() -> None:
    assert NM_PER_MM == 1_000_000
    assert mm_to_internal(1) == 1_000_000
    assert mm_to_internal(25.4) == 25_400_000
    assert isinstance(mm_to_internal(1.5), int)


def test_float_inputs_convert_exactly() -> None:
    # 0.1 is not representable in binary floating point; conversion must still be exact.
    assert mm_to_internal(0.1) == 100_000
    assert mm_to_internal(0.2) + mm_to_internal(0.1) == mm_to_internal(0.3)


def test_negative_and_decimal_inputs() -> None:
    assert mm_to_internal(-12.5) == -12_500_000
    assert mm_to_internal(Decimal("0.000001")) == 1


def test_rounding_is_half_even_below_one_nanometre() -> None:
    assert mm_to_internal(Decimal("0.0000005")) == 0
    assert mm_to_internal(Decimal("0.0000015")) == 2


def test_roundtrip() -> None:
    for mm in (0.0, 1.0, 0.125, 123.456789, -3.3):
        assert internal_to_mm(mm_to_internal(mm)) == pytest.approx(mm, abs=1e-9)


def test_parse_mm_is_exact_for_kicad_precision() -> None:
    assert parse_mm("12.345678") == 12_345_678
    assert parse_mm(" 100 ") == 100_000_000
    assert parse_mm("-0.775") == -775_000
    assert parse_mm("1e-3") == 1_000


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "--1"])
def test_parse_mm_rejects_garbage(bad: str) -> None:
    with pytest.raises(UnitConversionError):
        parse_mm(bad)


def test_rejects_non_finite_and_bool() -> None:
    with pytest.raises(UnitConversionError):
        mm_to_internal(float("inf"))
    with pytest.raises(UnitConversionError):
        mm_to_internal(float("nan"))
    with pytest.raises(UnitConversionError):
        mm_to_internal(True)


def test_mil_conversion() -> None:
    assert mil_to_internal(1) == 25_400
    assert mil_to_internal(10) == mm_to_internal(0.254)


def test_format_mm() -> None:
    assert format_mm(1_500_000) == "1.5 mm"
    assert format_mm(0) == "0 mm"
    assert format_mm(-250_000) == "-0.25 mm"
    assert format_mm(1) == "0 mm"  # below display precision
