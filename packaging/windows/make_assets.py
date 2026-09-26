"""Generate the application icon and the installer wizard bitmaps with Qt.

Outputs (committed to the repository, so builds do not need to run this):

* ``packaging/windows/assets/app.ico`` – multi-size icon (16…256 px, PNG-compressed
  entries, supported since Windows Vista) for the executables and the installer.
* ``packaging/windows/assets/wizard.bmp`` – 164×314 Welcome/Finish page image.
* ``packaging/windows/assets/header.bmp`` – 150×57 page header image.
* ``src/pcbrouter/resources/app_icon.png`` – 256 px window icon used at runtime.

The artwork is drawn as vectors at every size (not downscaled), so small icons stay
crisp. Run: ``python packaging/windows/make_assets.py``.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "packaging" / "windows" / "assets"
RUNTIME_ICON = ROOT / "src" / "pcbrouter" / "resources" / "app_icon.png"

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

BOARD_TOP = QColor("#1f7a4d")
BOARD_BOTTOM = QColor("#0f4d31")
BOARD_EDGE = QColor("#0a3321")
COPPER = QColor("#e0b04c")
HOLE = QColor("#0a3321")

# Artwork in a 256×256 design space: (polyline, end pads). 45° bends like real copper.
TRACES: tuple[tuple[tuple[float, float], ...], ...] = (
    ((58, 78), (140, 78), (178, 116), (198, 116)),
    ((58, 128), (112, 128), (152, 168), (198, 168)),
    ((58, 188), (100, 188)),
)
VIAS: tuple[tuple[float, float], ...] = ((100, 188),)


def draw_icon(painter: QPainter, size: float) -> None:
    """Draw the icon into a ``size``×``size`` square at the painter's origin."""
    s = size / 256.0
    small = size <= 24
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = 12 * s if not small else 1.0
    board = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(board.topLeft(), board.bottomRight())
    gradient.setColorAt(0.0, BOARD_TOP)
    gradient.setColorAt(1.0, BOARD_BOTTOM)
    painter.setBrush(gradient)
    painter.setPen(QPen(BOARD_EDGE, max(1.0, 6 * s)))
    radius = 36 * s
    painter.drawRoundedRect(board, radius, radius)

    # Small icons: fewer, thicker traces so the shape survives at 16 px.
    traces = TRACES[:2] if small else TRACES
    width = max(2.0, 16 * s) if not small else max(2.0, 26 * s)
    pen = QPen(COPPER, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for points in traces:
        path = QPainterPath(QPointF(points[0][0] * s, points[0][1] * s))
        for x, y in points[1:]:
            path.lineTo(x * s, y * s)
        painter.drawPath(path)

    if not small:
        pad_r, hole_r = 17 * s, 6.5 * s
        pads = [pt for trace in traces for pt in (trace[0], trace[-1])]
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y in pads + list(VIAS):
            painter.setBrush(COPPER)
            painter.drawEllipse(QPointF(x * s, y * s), pad_r, pad_r)
            painter.setBrush(HOLE)
            painter.drawEllipse(QPointF(x * s, y * s), hole_r, hole_r)
    painter.restore()


def render_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    draw_icon(painter, size)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # PySide6's stubs declare format as bytes, but the runtime only accepts str.
    if not image.save(buffer, "PNG"):  # type: ignore[call-overload]
        raise RuntimeError("PNG encoding failed")
    buffer.close()
    return bytes(data.data())


def ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    """ICO container with PNG entries: ICONDIR, one ICONDIRENTRY per image, data."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        blobs += blob
        offset += len(blob)
    return header + entries + blobs


def render_wizard() -> QImage:
    width, height = 164, 314
    image = QImage(width, height, QImage.Format.Format_RGB888)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, BOARD_TOP)
    gradient.setColorAt(1.0, BOARD_BOTTOM)
    painter.fillRect(0, 0, width, height, gradient)

    # Faint copper bus running down the panel: parallel 45° jogs, never crossing.
    faint = QColor(COPPER)
    faint.setAlpha(60)
    pen = QPen(faint, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    for x in (14, 38, 62, 86, 110, 134):
        path = QPainterPath(QPointF(x, 180))
        path.lineTo(x, 214)
        path.lineTo(x + 16, 230)
        path.lineTo(x + 16, height + 10)
        painter.drawPath(path)

    icon_size = 112
    painter.translate((width - icon_size) / 2, 40)
    draw_icon(painter, icon_size)
    painter.end()
    return image


def render_header() -> QImage:
    width, height = 150, 57
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(QColor("white"))
    painter = QPainter(image)
    icon_size = 44
    painter.translate(width - icon_size - 8, (height - icon_size) / 2)
    draw_icon(painter, icon_size)
    painter.end()
    return image


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(parents=True, exist_ok=True)
    RUNTIME_ICON.parent.mkdir(parents=True, exist_ok=True)

    images = [(size, png_bytes(render_icon(size))) for size in ICO_SIZES]
    (ASSETS / "app.ico").write_bytes(ico_bytes(images))
    RUNTIME_ICON.write_bytes(images[-1][1])
    for name, image in (("wizard.bmp", render_wizard()), ("header.bmp", render_header())):
        if not image.save(str(ASSETS / name), "BMP"):  # type: ignore[call-overload]
            raise RuntimeError(f"could not write {name}")
    for path in (ASSETS / "app.ico", ASSETS / "wizard.bmp", ASSETS / "header.bmp", RUNTIME_ICON):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
