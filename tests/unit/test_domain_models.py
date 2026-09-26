from __future__ import annotations

import pytest

from pcbrouter.domain import (
    Board,
    BoardMetadata,
    BoardOutline,
    BoardSide,
    BoundingBox,
    Component,
    Footprint,
    Layer,
    LayerKind,
    Net,
    OutlineSegment,
    OutlineShape,
    Pad,
    PadShape,
    PadType,
    Point,
    Track,
    Via,
    mm_to_internal,
)
from pcbrouter.domain.layer import classify_layer_name, copper_stack_position, expand_layer_pattern

MM = 1_000_000


def make_pad(pid: str, x: float, y: float, net: str | None, ref: str = "R1") -> Pad:
    return Pad(
        id=pid,
        number=pid[-1],
        footprint_ref=ref,
        position=Point(mm_to_internal(x), mm_to_internal(y)),
        size=(mm_to_internal(1), mm_to_internal(0.5)),
        shape=PadShape.RECT,
        pad_type=PadType.SMD,
        layers=("F.Cu",),
        net_name=net,
    )


def make_board(outline: BoardOutline | None = None) -> Board:
    fp = Footprint(
        id="fp1",
        lib_id="Lib:R",
        position=Point(10 * MM, 10 * MM),
        rotation_deg=0,
        side=BoardSide.FRONT,
        pads=(make_pad("p1", 9, 10, "A"), make_pad("p2", 11, 10, "B")),
        local_bounds=BoundingBox(-2 * MM, -MM, 2 * MM, MM),
    )
    return Board(
        metadata=BoardMetadata(),
        layers=(
            Layer.from_name("B.Cu"),
            Layer.from_name("F.Cu"),
            Layer.from_name("In1.Cu"),
            Layer.from_name("Edge.Cuts"),
        ),
        nets=(Net("A", 1), Net("B", 2), Net("C", 3)),
        components=(Component("R1", "1k", fp),),
        tracks=(
            Track("t1", Point(9 * MM, 10 * MM), Point(9 * MM, 20 * MM), MM // 4, "F.Cu", "A"),
            Track("t2", Point(9 * MM, 20 * MM), Point(12 * MM, 24 * MM), MM // 4, "B.Cu", "A"),
        ),
        vias=(Via("v1", Point(9 * MM, 20 * MM), 600_000, 300_000, "A", "F.Cu", "B.Cu"),),
        outline=outline or BoardOutline(),
    )


class TestLayers:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("F.Cu", LayerKind.FRONT_COPPER),
            ("B.Cu", LayerKind.BACK_COPPER),
            ("In1.Cu", LayerKind.INNER_COPPER),
            ("In30.Cu", LayerKind.INNER_COPPER),
            ("Edge.Cuts", LayerKind.BOARD_OUTLINE),
            ("F.SilkS", LayerKind.TECHNICAL),
            ("User.1", LayerKind.USER),
        ],
    )
    def test_classify(self, name: str, kind: LayerKind) -> None:
        assert classify_layer_name(name) is kind

    def test_stack_order(self) -> None:
        names = ["B.Cu", "In2.Cu", "F.Cu", "In1.Cu"]
        assert sorted(names, key=copper_stack_position) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_expand_wildcards(self) -> None:
        copper = ["F.Cu", "In1.Cu", "B.Cu"]
        assert expand_layer_pattern("*.Cu", copper) == copper
        assert expand_layer_pattern("*.Mask", copper) == ["F.Mask", "B.Mask"]
        assert expand_layer_pattern("F&B.Cu", copper) == ["F.Cu", "B.Cu"]
        assert expand_layer_pattern("F.Paste", copper) == ["F.Paste"]


class TestItems:
    def test_track_lengths(self) -> None:
        straight = Track("t", Point(0, 0), Point(3 * MM, 4 * MM), MM, "F.Cu", None)
        assert straight.length == 5 * MM
        arc = Track(
            "a", Point(0, 0), Point(0, 20 * MM), MM, "F.Cu", None, mid=Point(-10 * MM, 10 * MM)
        )
        assert arc.is_arc
        assert arc.length == pytest.approx(31_415_927, abs=1)
        assert arc.bounds.min_x == pytest.approx(-10 * MM - MM // 2, abs=2)

    def test_pad_bounds_rotated(self) -> None:
        pad = Pad(
            "p",
            "1",
            "U1",
            Point(0, 0),
            (2 * MM, MM),
            PadShape.RECT,
            PadType.SMD,
            ("F.Cu",),
            None,
            rotation_deg=90,
        )
        assert pad.bounds == BoundingBox(-MM // 2, -MM, MM // 2, MM)

    def test_footprint_outline_rotation(self) -> None:
        fp = Footprint(
            "f",
            "L:X",
            Point(10 * MM, 0),
            90,
            BoardSide.FRONT,
            (),
            BoundingBox(-2 * MM, -MM, 2 * MM, MM),
        )
        box = BoundingBox.from_points(fp.outline())
        assert box == BoundingBox(9 * MM, -2 * MM, 11 * MM, 2 * MM)

    def test_via_layer_span(self) -> None:
        order = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        blind = Via("v", Point(0, 0), MM, None, None, "F.Cu", "In1.Cu")
        assert blind.spans_layer("F.Cu", order) and blind.spans_layer("In1.Cu", order)
        assert not blind.spans_layer("B.Cu", order)
        unknown = Via("v2", Point(0, 0), MM, None, None, None, None)
        assert unknown.spans_layer("B.Cu", order)

    def test_net_display(self) -> None:
        assert Net("").display_name == "<no net>"
        assert Net("").is_unconnected
        assert Net("GND", 1).display_name == "GND"


class TestBoard:
    def test_statistics_and_index(self) -> None:
        board = make_board()
        s = board.statistics
        assert (s.footprint_count, s.pad_count, s.net_count, s.track_count, s.via_count) == (
            1, 2, 3, 2, 1,
        )  # fmt: skip
        assert s.copper_layer_count == 3
        assert s.total_track_length == 10 * MM + 5 * MM
        idx = board.index
        assert idx.components_by_ref["R1"].value == "1k"
        a = idx.net_statistics["A"]
        assert (a.pad_count, a.track_count, a.via_count, a.routed_length) == (1, 2, 1, 15 * MM)
        c = idx.net_statistics["C"]
        assert (c.pad_count, c.track_count, c.via_count) == (0, 0, 0)

    def test_copper_layers_sorted_by_stack(self) -> None:
        assert make_board().copper_layer_names == ["F.Cu", "In1.Cu", "B.Cu"]

    def test_board_is_immutable(self) -> None:
        board = make_board()
        with pytest.raises(AttributeError):
            board.tracks = ()  # type: ignore[misc]

    def test_closed_loops_from_unordered_reversed_segments(self) -> None:
        a, b, c, d = Point(0, 0), Point(10 * MM, 0), Point(10 * MM, 10 * MM), Point(0, 10 * MM)
        segs = (
            OutlineSegment(OutlineShape.LINE, c, b),  # reversed
            OutlineSegment(OutlineShape.LINE, a, b),
            OutlineSegment(OutlineShape.LINE, d, a),
            OutlineSegment(OutlineShape.LINE, c, d),
        )
        loops, open_chains = BoardOutline(segs).closed_loops()
        assert open_chains == 0
        assert len(loops) == 1
        assert set(loops[0]) == {a, b, c, d}

    def test_closed_loops_reports_open_chain_and_circle(self) -> None:
        segs = (
            OutlineSegment(OutlineShape.LINE, Point(0, 0), Point(10 * MM, 0)),
            OutlineSegment(OutlineShape.CIRCLE, Point(50 * MM, 50 * MM), Point(55 * MM, 50 * MM)),
        )
        loops, open_chains = BoardOutline(segs).closed_loops()
        assert open_chains == 1
        assert len(loops) == 1  # the circle
