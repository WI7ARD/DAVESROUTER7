"""Small dialogs: errors, stage placeholders, About, compute information."""

from __future__ import annotations

import platform

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QMessageBox, QWidget

from pcbrouter import APP_NAME, STAGE, __version__
from pcbrouter.ai.provider import STAGE_UNAVAILABLE_MESSAGE
from pcbrouter.compute.manager import ComputeManager


def show_error(
    parent: QWidget | None,
    title: str,
    message: str,
    details: str | None = None,
) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    if details:
        box.setDetailedText(details)
    box.exec()


def stage_unavailable_text(feature: str, planned_stage: str) -> str:
    return (
        f"{feature}\n\n{STAGE_UNAVAILABLE_MESSAGE}.\n\n"
        f"This build is Stage {STAGE}: a read-only board inspector. {feature} is planned for "
        f"{planned_stage}. No action was taken and the board was not modified."
    )


def show_stage_unavailable(parent: QWidget | None, feature: str, planned_stage: str) -> None:
    QMessageBox.information(
        parent,
        f"{feature} — {STAGE_UNAVAILABLE_MESSAGE}",
        stage_unavailable_text(feature, planned_stage),
    )


def about_text() -> str:
    return (
        f"<h3>{APP_NAME}</h3>"
        f"<p>Version <b>{__version__}</b> (Stage {STAGE} of 10)</p>"
        "<p>Read-only KiCad board inspector and the foundation for deterministic, "
        "AI-assisted PCB autorouting.</p>"
        "<p><b>Stage 1 does not perform autorouting or AI API calls.</b></p>"
        f"<p>Python {platform.python_version()} · Qt {qVersion()} · PySide6 {pyside_version}"
        f"<br>{platform.platform()}</p>"
    )


def show_about(parent: QWidget | None) -> None:
    QMessageBox.about(parent, f"About {APP_NAME}", about_text())


def compute_info_text(compute: ComputeManager) -> str:
    lines = [f"Active compute backend: {compute.active.name}", ""]
    cpu = compute.cpu.device_info()
    lines += ["CPU", f"  {cpu.name}"] + [f"  {k}: {v}" for k, v in cpu.details.items()]
    gpu = compute.gpu.device_info()
    lines += ["", "GPU candidate", f"  {gpu.name}"]
    lines += [f"  {k}: {v}" for k, v in gpu.details.items()]
    det = compute.gpu.detection
    if len(det.devices) > 1:
        lines += ["  Other devices:"] + [f"    {d.vendor}: {d.name}" for d in det.devices[1:]]
    lines += [f"  Note: {n}" for n in gpu.notes]
    if compute.fallback_reason:
        lines += ["", f"Fallback: {compute.fallback_reason}"]
    return "\n".join(lines)


def show_compute_info(parent: QWidget | None, compute: ComputeManager) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("Compute Backend Information")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(compute_info_text(compute))
    box.exec()
