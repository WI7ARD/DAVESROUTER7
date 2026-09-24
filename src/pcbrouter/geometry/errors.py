"""Normalised geometry errors. UI shows ``user_message``; logs get the detail."""

from __future__ import annotations


class GeometryError(Exception):
    """Base class for geometry-engine failures."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class InvalidGeometryError(GeometryError):
    """Input geometry is malformed (e.g. a polygon with fewer than three vertices)."""


class UnsupportedGeometryError(GeometryError):
    """A construct is recognised but cannot be represented (not even conservatively)."""
