"""Canonical unit handling.

DECISION: every length/coordinate in the core domain model is an ``int`` number of
**nanometres** (``Nm``).

Why nanometres:

* KiCad itself stores board geometry as integer nanometres (``IU`` in pcbnew), and
  ``.kicad_pcb`` files never carry more than 6 decimal places of millimetres. A file
  value such as ``"12.345678"`` therefore maps to an exact integer with no rounding.
* Integers make equality, hashing, and future spatial indexing (routing grids,
  R-trees, GPU buffers) deterministic. Floating-point drift cannot creep in.
* Range is not a concern: a 2 m board is 2e9 nm, well inside Python's unbounded
  ints and still inside int64 for future numpy/GPU buffers.

Rules:

* Only this module converts between millimetres and internal units.
* Parsers convert text with :func:`parse_mm` (exact decimal arithmetic).
* UI/reporting converts back with :func:`internal_to_mm` at the display boundary.
* Angles are not lengths: they are stored as ``float`` degrees in fields that are
  explicitly named ``*_deg``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

#: Internal length unit: integer nanometres. The alias documents intent in signatures.
type Nm = int

NM_PER_MM: Final[int] = 1_000_000
NM_PER_UM: Final[int] = 1_000
NM_PER_MIL: Final[int] = 25_400  # 1 mil = 0.0254 mm
NM_PER_INCH: Final[int] = 25_400_000

_DECIMAL_NM_PER_MM: Final[Decimal] = Decimal(NM_PER_MM)


class UnitConversionError(ValueError):
    """Raised when a textual length cannot be converted to internal units."""


def mm_to_internal(mm: float | int | Decimal) -> Nm:
    """Convert millimetres to internal nanometres, rounding half-to-even.

    Accepts floats for convenience (e.g. UI spin boxes); the value is routed through
    ``Decimal(str(mm))`` so ``0.1`` becomes exactly 100000 nm rather than 99999.
    """
    if isinstance(mm, bool):  # bool is an int subclass; reject it explicitly.
        raise UnitConversionError("boolean is not a length")
    try:
        value = mm if isinstance(mm, Decimal) else Decimal(str(mm))
    except InvalidOperation as exc:  # pragma: no cover - str(float) is always valid
        raise UnitConversionError(f"invalid length: {mm!r}") from exc
    if not value.is_finite():
        raise UnitConversionError(f"length must be finite, got {mm!r}")
    return int((value * _DECIMAL_NM_PER_MM).to_integral_value(rounding=ROUND_HALF_EVEN))


def internal_to_mm(nm: Nm) -> float:
    """Convert internal nanometres to millimetres (for display and reporting only)."""
    return nm / NM_PER_MM


def parse_mm(text: str) -> Nm:
    """Parse a millimetre value written as text (as found in KiCad files) exactly."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise UnitConversionError(f"not a number: {text!r}") from exc
    return mm_to_internal(value)


def mil_to_internal(mil: float | int) -> Nm:
    """Convert thousandths of an inch to internal nanometres."""
    return round(Decimal(str(mil)) * NM_PER_MIL)


def format_mm(nm: Nm, decimals: int = 4) -> str:
    """Format an internal length as a millimetre string, trimming trailing zeros."""
    text = f"{internal_to_mm(nm):.{decimals}f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return f"{text} mm"
