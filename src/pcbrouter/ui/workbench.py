"""Stage 8 routing workbench: view modes, locks, net constraints, corridors, local
reroute, route inspector, route diff, "why a via?", failed-search heat map/playback.

Everything here is UI glue over deterministic services: locks and constraints live
on the working board (and are undoable), route diffs are computed on forks, and
nothing reaches the working board without an explicit accept.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np
from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMenu,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import format_mm, internal_to_mm, mm_to_internal
from pcbrouter.history.history import UndoableAction
from pcbrouter.routing.inspect import (
    candidate_objects,
    explain_vias,
    route_diff,
    route_info,
    section_ids,
)
from pcbrouter.routing.occupancy import GridSpec
from pcbrouter.routing.request import SoftRegion, SoftRegionKind
from pcbrouter.routing.result import RouteCandidate
from pcbrouter.routing.working_board import WorkingBoard
from pcbrouter.ui import overlays, theme
from pcbrouter.ui.pcb_canvas import ItemKind
from pcbrouter.ui.workers import JobRunner

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

log = logging.getLogger(__name__)

VIEW_LABELS = {
    "working": "&Working (all copper)",
    "original": "&Original (file copper only)",
    "overlay": "O&verlay (file copper dimmed)",
    "difference": "&Difference (added copper only)",
}
PLAYBACK_STEPS = 60


class LockAction(UndoableAction):
    """Lock/unlock changes as history entries (undoable)."""

    def __init__(self, wb: WorkingBoard, label: str, before: dict[str, Any], after: dict[str, Any]):
        self.wb, self._label, self.before, self.after = wb, label, before, after

    @property
    def label(self) -> str:
        return self._label

    @property
    def kind(self) -> str:
        return "lock"

    @property
    def metadata(self) -> Mapping[str, Any]:
        return {"locks": self.after}

    def apply(self) -> None:
        self.wb.restore_lock_state(self.after)

    def revert(self) -> None:
        self.wb.restore_lock_state(self.before)


def _mm_spin(value: float, lo: float = -10_000, hi: float = 10_000) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(3)
    s.setSuffix(" mm")
    s.setValue(value)
    return s


class BoxEditor(QWidget):
    """x0, y0, x1, y1 in mm."""

    def __init__(self, box: tuple[float, float, float, float]) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.spins = [_mm_spin(v) for v in box]
        for label, spin in zip(("X min", "Y min", "X max", "Y max"), self.spins, strict=True):
            form.addRow(label, spin)

    def value(self) -> tuple[float, float, float, float]:
        a, b, c, d = (s.value() for s in self.spins)
        return (min(a, c), min(b, d), max(a, c), max(b, d))


class NetConstraintsDialog(QDialog):
    """Router ▸ Net Constraints: user preferences for one net, validated against the
    hard rules (a width below the minimum is refused here and by the router)."""

    def __init__(self, wb: WorkingBoard, net: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Routing Constraints — {net}")
        self.wb, self.net = wb, net
        cur = dict(wb.net_constraints.get(net, {}))
        rules = wb.engine.resolver.width_rules(net)
        self.min_width = rules.minimum.value
        form = QFormLayout()
        self.width_spin = _mm_spin(float(cur.get("width_mm", 0.0)), 0.0, 20.0)
        self.width_spin.setSpecialValueText("from rules")
        self.width_spin.setToolTip(
            f"Rule: preferred {rules.preferred.describe()}; " f"minimum {rules.minimum.describe()}"
        )
        form.addRow("Width:", self.width_spin)
        self.layer_boxes: dict[str, QCheckBox] = {}
        allowed = cur.get("allowed_layers") or list(wb.engine.geometry.copper_layers)
        for layer in wb.engine.geometry.copper_layers:
            box = QCheckBox(layer)
            box.setChecked(layer in allowed)
            self.layer_boxes[layer] = box
            form.addRow("Allowed layer:" if len(self.layer_boxes) == 1 else "", box)
        self.max_vias = QSpinBox()
        self.max_vias.setRange(-1, 256)
        self.max_vias.setSpecialValueText("from rules")
        self.max_vias.setValue(int(cur.get("max_vias", -1)))
        form.addRow("Max vias:", self.max_vias)
        self.priority = QSpinBox()
        self.priority.setRange(0, 100)
        self.priority.setValue(int(cur.get("priority", 0)))
        form.addRow("Priority (board routing):", self.priority)
        self.preserve = QCheckBox("Preserve existing routes")
        self.preserve.setChecked(bool(cur.get("preserve_existing", True)))
        form.addRow(self.preserve)
        self.use_prefer = QCheckBox("Preferred corridor")
        self.use_avoid = QCheckBox("Avoid region")
        box_default = self._default_box()
        self.prefer = BoxEditor(tuple(cur.get("prefer_box") or box_default))
        self.avoid = BoxEditor(tuple(cur.get("avoid_box") or box_default))
        self.use_prefer.setChecked("prefer_box" in cur)
        self.use_avoid.setChecked("avoid_box" in cur)
        form.addRow(self.use_prefer, self.prefer)
        form.addRow(self.use_avoid, self.avoid)
        self.error = QLabel("")
        self.error.setStyleSheet("color:#ff3b30")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        note = QLabel(
            "Preferences for the router. Hard rules (minimum width, clearance, "
            "keepouts) always apply; corridors are soft cost fields, not keepouts."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _default_box(self) -> tuple[float, float, float, float]:
        b = self.wb.board.bounds
        if b is None:
            return (0.0, 0.0, 10.0, 10.0)
        return (
            internal_to_mm(b.min_x),
            internal_to_mm(b.min_y),
            internal_to_mm(b.max_x),
            internal_to_mm(b.max_y),
        )

    def result_constraints(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.width_spin.value() > 0:
            nm = mm_to_internal(self.width_spin.value())
            if self.min_width is not None and nm < self.min_width:
                raise ValueError(f"width below the hard minimum {format_mm(self.min_width)}")
            out["width_mm"] = self.width_spin.value()
        layers = [layer for layer, box in self.layer_boxes.items() if box.isChecked()]
        if not layers:
            raise ValueError("select at least one layer")
        if len(layers) != len(self.layer_boxes):
            out["allowed_layers"] = layers
        if self.max_vias.value() >= 0:
            out["max_vias"] = self.max_vias.value()
        if self.priority.value():
            out["priority"] = self.priority.value()
        if not self.preserve.isChecked():
            out["preserve_existing"] = False
        if self.use_prefer.isChecked():
            out["prefer_box"] = list(self.prefer.value())
        if self.use_avoid.isChecked():
            out["avoid_box"] = list(self.avoid.value())
        return out

    def accept(self) -> None:
        try:
            self.result_constraints()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()


class RegionDialog(QDialog):
    def __init__(
        self,
        title: str,
        box: tuple[float, float, float, float],
        parent: QWidget | None = None,
        kinds: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.editor = BoxEditor(box)
        self.avoid = QCheckBox("Avoid (unchecked: prefer)")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.editor)
        if kinds:
            layout.addWidget(self.avoid)
        layout.addWidget(buttons)

    def box_nm(self) -> BoundingBox:
        return BoundingBox(*(mm_to_internal(v) for v in self.editor.value()))


class WorkbenchController(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.jobs = JobRunner(self)
        self.last_diff_lines: list[str] = []
        self.last_explain: dict[str, Any] | None = None
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._play_step)
        self._play_data: dict[str, Any] | None = None
        self._play_pos = 0
        a = self._act
        self.view_group = QActionGroup(self)
        self.view_actions: dict[str, QAction] = {}
        for mode, label in VIEW_LABELS.items():
            act = a(label, lambda _=False, m=mode: self.set_view_mode(m), checkable=True)
            self.view_group.addAction(act)
            self.view_actions[mode] = act
        self.view_actions["working"].setChecked(True)
        self.act_lock = a("&Lock / Unlock Selected", self.toggle_lock, "L")
        self.act_lock_region = a("Lock &Region…", self.lock_region)
        self.act_unlock_all = a("&Unlock All", self.unlock_all)
        self.act_constraints = a("Net Routing &Constraints…", self.edit_constraints)
        self.act_corridor = a("Add Routing &Corridor…", self.add_corridor)
        self.act_clear_corridors = a("Clear Corridors", self.clear_corridors)
        self.act_reroute = a("&Reroute Selected Section", self.reroute_section, "Shift+R")
        self.act_explain = a("&Explain Route Vias", self.explain_selected)
        self.act_record = a("Record Failed Search (debug)", self._toggle_record, checkable=True)
        self.act_play = a("Play Search Exploration", self.play_search)

    def _act(
        self, text: str, slot: Any, shortcut: str | None = None, checkable: bool = False
    ) -> QAction:
        act = QAction(text, self.w)
        if shortcut:
            act.setShortcut(shortcut)
        act.setCheckable(checkable)
        (act.toggled if checkable else act.triggered).connect(slot)
        return act

    def install(self, view: QMenu, router: QMenu, edit: QMenu) -> None:
        modes = view.addMenu("Board &View")
        for act in self.view_actions.values():
            modes.addAction(act)
        edit.addSeparator()
        for act in (self.act_lock, self.act_lock_region, self.act_unlock_all):
            edit.addAction(act)
        router.addSeparator()
        for act in (
            self.act_reroute,
            self.act_constraints,
            self.act_corridor,
            self.act_clear_corridors,
            self.act_explain,
        ):
            router.addAction(act)
        debug = router.addMenu("Search &Debug")
        debug.addAction(self.act_record)
        debug.addAction(self.act_play)

    @property
    def working(self) -> WorkingBoard | None:
        wb: WorkingBoard | None = self.w.bus.context.project.working
        return wb

    # ------------------------------------------------------------ view modes
    def set_view_mode(self, mode: str) -> None:
        self.w.canvas.set_view_mode(mode)
        act = self.view_actions.get(mode)
        if act is not None and not act.isChecked():
            act.setChecked(True)
        wb = self.working
        added = len(wb.generated_ids()) if wb else 0
        self.w.statusBar().showMessage(
            f"View: {mode} — {added} router/user-added object(s) on the working board", 6000
        )

    # ------------------------------------------------------------ locks
    def _selection(self) -> tuple[ItemKind, str] | None:
        sel: tuple[ItemKind, str] | None = self.w._selected
        return sel

    def toggle_lock(self) -> bool:
        wb, sel = self.working, self._selection()
        if wb is None or sel is None:
            self.w.statusBar().showMessage("Select a track, via, net or component to lock.", 5000)
            return False
        kind, obj_id = sel
        idx = wb.board.index
        if kind in (ItemKind.TRACK, ItemKind.VIA):
            key = obj_id
        elif kind is ItemKind.NET:
            key = f"net:{obj_id}"
        elif kind is ItemKind.COMPONENT and obj_id in idx.components_by_id:
            key = f"comp:{idx.components_by_id[obj_id].reference}"
        elif kind is ItemKind.PAD and obj_id in idx.pads_by_id:
            key = f"comp:{idx.pads_by_id[obj_id].footprint_ref}"
        else:
            return False
        before = wb.lock_state()
        locks = set(wb.locks) ^ {key}
        after = {**before, "locks": sorted(locks)}
        verb = "Lock" if key in locks else "Unlock"
        self.w.bus.context.history.push(LockAction(wb, f"{verb} {key}", before, after))
        self.w._update_undo_actions()
        self.refresh_lock_overlay()
        self.w.statusBar().showMessage(
            (
                f"{verb}ed {key}: the router will not change it."
                if verb == "Lock"
                else f"Unlocked {key}."
            ),
            6000,
        )
        return True

    def lock_region(self, box: BoundingBox | None = None) -> bool:
        wb = self.working
        if wb is None:
            return False
        if box is None:
            r = self.w.canvas.mapToScene(self.w.canvas.viewport().rect()).boundingRect()
            dlg = RegionDialog("Lock Region", (r.left(), r.top(), r.right(), r.bottom()), self.w)
            if self.w.run_dialog(dlg) != QDialog.DialogCode.Accepted:
                return False
            box = dlg.box_nm()
        before = wb.lock_state()
        after = {
            **before,
            "regions": [*before["regions"], [box.min_x, box.min_y, box.max_x, box.max_y]],
        }
        self.w.bus.context.history.push(LockAction(wb, "Lock region", before, after))
        self.w._update_undo_actions()
        self.refresh_lock_overlay()
        return True

    def unlock_all(self) -> None:
        wb = self.working
        if wb is None:
            return
        before = wb.lock_state()
        self.w.bus.context.history.push(
            LockAction(wb, "Unlock all", before, {"locks": [], "regions": []})
        )
        self.w._update_undo_actions()
        self.refresh_lock_overlay()

    def refresh_lock_overlay(self) -> None:
        wb = self.working
        ov = self.w.engine_ui.overlays
        if wb is None:
            ov.clear("locks")
            return
        geo = wb.geometry
        shapes = []
        ids = set(wb.locks) | set(wb.protected_ids())
        for net_key in (k for k in wb.locks if k.startswith("net:")):
            ids |= {t.id for t in wb.board.tracks if t.net_name == net_key[4:]}
            ids |= {v.id for v in wb.board.vias if v.net_name == net_key[4:]}
        for obj_id in ids:
            for prefix in ("track:", "via:"):
                item = geo.copper.get(prefix + obj_id)
                if item is not None:
                    shapes += list(item.shapes)
        items = overlays.outline_items(shapes, theme.SELECTION_COLOR, dashed=True)
        items += overlays.bounds_items(wb.locked_regions, theme.KEEPOUT_COLOR)
        for it in items:
            it.setToolTip("LOCKED — the router and optimiser will not change this")
        ov.set_group("locks", items)

    # ------------------------------------------------------------ constraints / corridors
    def edit_constraints(self) -> bool:
        wb = self.working
        net = self.w.routing_ui.selected_net()
        if wb is None or net is None:
            self.w.statusBar().showMessage("Select a net first.", 5000)
            return False
        dlg = NetConstraintsDialog(wb, net, self.w)
        if self.w.run_dialog(dlg) != QDialog.DialogCode.Accepted:
            return False
        values = dlg.result_constraints()
        if values:
            wb.net_constraints[net] = values
        else:
            wb.net_constraints.pop(net, None)
        self.w.statusBar().showMessage(
            f"Routing constraints for {net}: {values or 'rules only'}", 8000
        )
        return True

    def add_corridor(self, box: BoundingBox | None = None, avoid: bool | None = None) -> bool:
        wb = self.working
        if wb is None:
            return False
        if box is None or avoid is None:
            r = self.w.canvas.mapToScene(self.w.canvas.viewport().rect()).boundingRect()
            dlg = RegionDialog(
                "Routing Corridor", (r.left(), r.top(), r.right(), r.bottom()), self.w, kinds=True
            )
            if self.w.run_dialog(dlg) != QDialog.DialogCode.Accepted:
                return False
            box, avoid = dlg.box_nm(), dlg.avoid.isChecked()
        kind = SoftRegionKind.AVOID if avoid else SoftRegionKind.PREFER
        wb.corridors.append(SoftRegion(kind, box))
        self._corridor_overlay()
        return True

    def clear_corridors(self) -> None:
        wb = self.working
        if wb is not None:
            wb.corridors.clear()
        self._corridor_overlay()

    def _corridor_overlay(self) -> None:
        wb = self.working
        ov = self.w.engine_ui.overlays
        if wb is None or not wb.corridors:
            ov.clear("corridors")
            return
        items = []
        for c in wb.corridors:
            color = (
                theme.KEEPOUT_COLOR
                if c.kind is SoftRegionKind.AVOID
                else theme.CANDIDATE_VALID_COLOR
            )
            for it in overlays.bounds_items([c.box], color):
                it.setToolTip(f"{c.kind.value} corridor (soft cost, not a keepout)")
                items.append(it)
        ov.set_group("corridors", items)

    # ------------------------------------------------------------ local reroute
    def reroute_section(self) -> bool:
        wb, sel = self.working, self._selection()
        if wb is None or sel is None or sel[0] is not ItemKind.TRACK:
            self.w.statusBar().showMessage("Select a router-generated track first.", 5000)
            return False
        ids = section_ids(wb, sel[1])
        if not ids:
            self.w.statusBar().showMessage(
                "This track is source, locked, or not part of a reroutable section.", 6000
            )
            return False
        track = wb.board.index.tracks_by_id[sel[1]]
        net = track.net_name or ""
        req = self.w.routing_ui.request_for(net)
        self.w.statusBar().showMessage(f"Rerouting {len(ids)} segment(s) of {net}…", 6000)
        # the worker removes the section from its own copy and routes around it
        return self.w.routing_ui.route_net(req, remove_ids=tuple(ids))

    # ------------------------------------------------------------ route inspector
    def on_object_selected(self, kind: ItemKind, obj_id: str) -> None:
        wb = self.working
        if wb is None or kind not in (ItemKind.TRACK, ItemKind.VIA):
            return
        try:
            info = route_info(wb, obj_id)
        except Exception:  # inspection must never break selection
            log.debug("route_info failed", exc_info=True)
            return
        if info is None:
            return
        rows = [(k.replace("_", " "), None if v is None else str(v)) for k, v in info.items()]
        self.w.inspector.add_rows("Route (Stage 8)", rows)

    # ------------------------------------------------------------ diff
    def compute_diff(self, cand: RouteCandidate, remove_ids: tuple[str, ...] = ()) -> None:
        wb = self.working
        if wb is None:
            return
        tracks, vias = candidate_objects(wb, cand.proposal)

        def job() -> list[str]:
            return route_diff(wb, tracks, vias, list(remove_ids)).lines()

        def done(lines: object, _s: float) -> None:
            if isinstance(lines, list):
                self.last_diff_lines = [str(x) for x in lines]
                self.w.routing_ui.panel.set_diff(self.last_diff_lines)

        self.jobs.start("diff", job, done, lambda m, _d: log.info("diff failed: %s", m))

    # ------------------------------------------------------------ explain vias
    def explain_selected(self) -> bool:
        wb = self.working
        net = self.w.routing_ui.selected_net()
        if wb is None or net is None:
            return False

        def done(facts: object, _s: float) -> None:
            if isinstance(facts, dict):
                self.last_explain = facts
                session = self.w.ai_service.session
                if session is not None:  # deterministic fact for the next AI turn
                    session.fact_answers.append(
                        {"tool": "explain_vias", "target": net, "fact": facts}
                    )
                self.w.statusBar().showMessage(f"{net}: {facts.get('answer')}", 15000)

        return self.jobs.start(
            "explain",
            lambda: explain_vias(wb, net),
            done,
            lambda m, _d: self.w.statusBar().showMessage(m, 8000),
        )

    # ------------------------------------------------------------ failed search
    def _toggle_record(self, on: bool) -> None:
        self.w.routing_ui.record_search = on

    def show_explored(self, explored: dict[str, Any], upto: int | None = None) -> None:
        spec: GridSpec = explored["spec"]
        cells = np.asarray(explored["cells"])
        if upto is not None:
            cells = cells[:upto]
        counts = np.bincount(cells, minlength=spec.nx * spec.ny)[: spec.nx * spec.ny]
        heat = np.clip(counts.reshape(spec.ny, spec.nx) / max(1, int(counts.max())), 0, 1)
        rgba = np.zeros((spec.ny, spec.nx, 4), dtype=np.uint8)
        rgba[..., 0] = 255
        rgba[..., 1] = (180 * (1 - heat)).astype(np.uint8)
        rgba[..., 3] = np.where(counts > 0, 60 + (140 * heat).astype(np.uint8), 0)
        from pcbrouter.domain.geometry import Point

        item = overlays._image_item(
            rgba,
            Point(spec.origin_x, spec.origin_y),
            spec.cell,
            "Failed search: explored cells (debug)",
        )
        self.w.engine_ui.overlays.set_group("debug:explored", [item])
        self._play_data = explored

    def play_search(self) -> bool:
        if self._play_data is None:
            self.w.statusBar().showMessage(
                "No recorded search. Enable Router ▸ Search Debug ▸ Record Failed Search.", 6000
            )
            return False
        self._play_pos = 0
        self._play_timer.start(50)
        return True

    def _play_step(self) -> None:
        data = self._play_data
        if data is None:
            self._play_timer.stop()
            return
        total = len(data["cells"])
        self._play_pos += max(1, total // PLAYBACK_STEPS)
        self.show_explored(data, min(self._play_pos, total))
        if self._play_pos >= total:
            self._play_timer.stop()

    def on_board_changed(self) -> None:
        self._play_timer.stop()
        self._play_data = None
        self.last_diff_lines = []
        self.view_actions["working"].setChecked(True)
        self.w.canvas.set_view_mode("working")

    def on_working_changed(self) -> None:
        self.refresh_lock_overlay()
        self._corridor_overlay()

    def shutdown(self) -> None:
        self._play_timer.stop()
        self.jobs.wait(10_000)
