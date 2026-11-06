import glob
import warnings

import pytest
from shapely.geometry import box

from conftest import mcu_circuit
from studioforge.export import bom as bomx
from studioforge.export.package import write_package
from studioforge.pcb import analysis
from studioforge.pcb.board import Track
from studioforge.pcb.drc import run_drc
from studioforge.pcb.flow import BoardSpec, build_board
from studioforge.pcb.placement import PlaceOptions, place
from studioforge.pcb.rules import current_for_width, rules_for, trace_width_for_current
from studioforge.pcb.planes import assign_planes


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from studioforge.parts.db import PartsLibrary
    lib = PartsLibrary(tmp_path_factory.mktemp("h"), auto_research=False)
    return {layers: build_board(mcu_circuit(lib), BoardSpec(layers=layers), log=None) for layers in (2, 4)}


@pytest.mark.parametrize("layers", [2, 4])
def test_board_routes_and_passes_independent_drc(built, layers):
    res = built[layers]
    assert res.success, res.drc.summary()
    assert res.board.stats["unrouted"] == []
    assert res.drc.stats["nets_disconnected"] == 0
    assert res.drc.clean


def test_drc_catches_injected_faults(built):
    b = built[2].board
    t = next(t for t in b.tracks if t.net.startswith("S_PA9"))
    b.tracks.append(Track(t.layer, "S_PA10", t.x1, t.y1, t.x1 + 0.1, t.y1, 0.2))      # short to another net
    b.tracks.append(Track(t.layer, "S_PB7", 1, 1, 2, 1, 0.02))                          # under min width
    rep = run_drc(b)
    kinds = {v.kind for v in rep.errors}
    assert "short" in kinds and "min-width" in kinds
    b.tracks.pop(); b.tracks.pop()


def test_placement_is_deterministic(lib):
    r = rules_for(2)
    a = place(mcu_circuit(lib), r, PlaceOptions(seed=7))
    b = place(mcu_circuit(lib), r, PlaceOptions(seed=7))
    assert {k: (v.x, v.y, v.rot) for k, v in a.place.items()} == {k: (v.x, v.y, v.rot) for k, v in b.place.items()}
    assert a.stats["place_overlap_mm2"] == 0


def test_decoupling_caps_end_up_near_their_pins(built):
    for d in analysis.decoupling_report(built[4].board):
        assert d["dist_mm"] <= d["limit_mm"] * 1.5, d


def test_gerbers_and_drills_parse_and_match_model(built, tmp_path):
    gerbonara = pytest.importorskip("gerbonara")
    from gerbonara import ExcellonFile, GerberFile
    from gerbonara.graphic_objects import Flash, Line
    res = built[4]
    b = res.board
    write_package(res, tmp_path, "t")
    files = glob.glob(str(tmp_path / "gerber" / "*"))
    assert len(files) >= 10
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for f in files:
            (ExcellonFile if f.endswith(".drl") else GerberFile).open(f)
        d = ExcellonFile.open(str(tmp_path / "gerber" / "t-PTH.drl"))
        model = {(round(p.x, 3), round(b.height - p.y, 3)) for p in b.pads() if p.drill} | {(round(v.x, 3), round(b.height - v.y, 3)) for v in b.vias}
        assert {(round(o.x, 3), round(o.y, 3)) for o in d.objects} == model
        top = GerberFile.open(str(tmp_path / "gerber" / "t-F_Cu.gbr"))
        lines = [o for o in top.objects if isinstance(o, Line)]
        assert len(lines) == len([t for t in b.tracks if t.layer == "F.Cu"])


def test_kicad_pcb_is_parseable(built, tmp_path):
    kiutils = pytest.importorskip("kiutils.board")
    res = built[4]
    write_package(res, tmp_path, "t")
    kb = kiutils.Board.from_file(str(tmp_path / "t.kicad_pcb"))
    assert len(kb.footprints) == len(res.board.circuit.components)
    assert len([t for t in kb.traceItems if t.__class__.__name__ == "Via"]) == len(res.board.vias)
    assert len(kb.nets) == len(res.board.circuit.nets) + 1


def test_bom_and_cpl(built):
    c = built[2].board.circuit
    rows = bomx.group_bom(c)
    assert sum(r["qty"] for r in rows) == len(c.components)
    assert any(r["mpn"] == "STM32F103C8T6" and r["qty"] == 1 for r in rows)
    cpl = bomx.cpl_csv(built[2].board).splitlines()
    assert cpl[0].startswith("Designator,Val,Package") and len(cpl) == len(c.components) + 1


def test_ipc2221_width_roundtrip_and_monotonic():
    w1, w5 = trace_width_for_current(1.0), trace_width_for_current(5.0)
    assert 0.1 < w1 < 0.6 and w5 > w1
    assert abs(current_for_width(trace_width_for_current(3.0)) - 3.0) < 0.01
    assert trace_width_for_current(3.0, oz=2.0) < trace_width_for_current(3.0, oz=1.0)


def test_thermal_map_scales_with_power(built):
    b = built[4].board
    base = analysis.thermal_map(b)
    for c in b.circuit.components.values():
        c.power_w *= 2
    hot = analysis.thermal_map(b)
    assert hot["peak_rise_c"] > base["peak_rise_c"] * 1.8
    for c in b.circuit.components.values():
        c.power_w /= 2


def test_plane_assignment(lib):
    b = place(mcu_circuit(lib), rules_for(4), PlaceOptions())
    assign_planes(b)
    assert b.plane_nets["In1.Cu"] == "GND"
    b2 = place(mcu_circuit(lib), rules_for(2), PlaceOptions())
    assign_planes(b2)
    assert "GND" in b2.pour_nets and not b2.plane_nets


def test_auto_pitch_follows_pad_geometry(lib):
    """A 0.5 mm-pitch QFP on 0.2/0.2 rules needs a fine grid (corridor ~0.03 mm); coarse parts allow a fast coarse grid."""
    from studioforge.model import Circuit
    from studioforge.pcb.board import Board
    from studioforge.pcb.router import auto_pitch
    fine = mcu_circuit(lib)
    g_fine, _ = auto_pitch(Board(fine, rules_for(2), 30, 30), 0.2, 0.2)
    g_fine4, _ = auto_pitch(Board(fine, rules_for(4), 30, 30), 0.15, 0.15)
    assert g_fine <= 0.075 and g_fine4 >= g_fine
    coarse = Circuit("coarse")
    coarse.add(lib.header(8), prefix="J")
    coarse.add(lib.resistor(1e3, "0805"), prefix="R")
    g_coarse, _ = auto_pitch(Board(coarse, rules_for(2), 30, 30), 0.2, 0.2)
    assert g_coarse >= 0.15


def test_breakout_margin_grows_board_for_fine_pitch(lib):
    from studioforge.pcb.placement import _auto_size, breakout_margin, PlaceOptions
    assert breakout_margin(48) > breakout_margin(8) > breakout_margin(2) == 0
    w, h = _auto_size(mcu_circuit(lib), PlaceOptions())
    assert w * h > 20 * 20
