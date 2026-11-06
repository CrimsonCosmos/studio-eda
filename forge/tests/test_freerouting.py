"""Freerouting integration (skipped when java >= 21, the verified jar or KiCad's pcbnew python are missing)."""
import pytest
from conftest import mcu_circuit

from studioforge.pcb import freerouting as fr
from studioforge.pcb.drc import run_drc
from studioforge.pcb.placement import PlaceOptions, place
from studioforge.pcb.planes import add_stitching, assign_planes, fill_zones
from studioforge.pcb.pours import plan_power_pours
from studioforge.pcb.rules import rules_for

needs_fr = pytest.mark.skipif(not fr.available(), reason="java>=21 / Freerouting jar / KiCad pcbnew not available")

SES = """(session a (base_design a) (routes (resolution um 10) (library_out
  (padstack "Via[0-3]_500:250_um" (shape (circle F.Cu 5000 0 0)) (attach off)))
  (network_out (net GND (via "Via[0-3]_500:250_um" 152350 -79322 (type protect))
    (wire (path F.Cu 1500 148700 -73000 152350 -79322 160000 -79322) (type protect))))))"""


def test_parse_ses():
    r = fr.parse_ses(SES)
    assert len(r["vias"]) == 1 and r["vias"][0]["dia"] == 0.5 and r["vias"][0]["drill"] == 0.25
    assert abs(r["vias"][0]["x"] - 15.235) < 1e-9 and abs(r["vias"][0]["y"] - 7.9322) < 1e-9
    assert len(r["tracks"]) == 2 and r["tracks"][0]["width"] == pytest.approx(0.15)
    assert r["tracks"][0]["net"] == "GND"


def test_router_selection_builtin_never_uses_freerouting(lib, monkeypatch):
    from studioforge.pcb.flow import BoardSpec, build_board
    called = []
    monkeypatch.setattr(fr, "route_with_freerouting", lambda *a, **k: called.append(1))
    build_board(mcu_circuit(lib), BoardSpec(layers=2, router="builtin"), log=None)
    assert not called


@needs_fr
@pytest.mark.parametrize("layers", [2, 4])
def test_freerouting_routes_mcu(lib, layers):
    b = place(mcu_circuit(lib), rules_for(layers), PlaceOptions())
    assign_planes(b)
    plan_power_pours(b)
    fr.route_with_freerouting(b, fr.FreeroutingOptions(timeout_s=90))
    fill_zones(b)
    if add_stitching(b):
        fill_zones(b)
    assert b.stats["router"] == "freerouting"
    assert b.stats["unrouted"] == []
    assert b.tracks
    assert not any(t.layer in b.plane_nets for t in b.tracks)
    drc = run_drc(b)
    assert drc.clean, drc.summary()


@needs_fr
def test_flow_uses_freerouting(lib):
    from studioforge.pcb.flow import BoardSpec, build_board
    res = build_board(mcu_circuit(lib), BoardSpec(layers=4, router="freerouting"), log=None)
    assert res.board.stats.get("router") == "freerouting"
    assert res.success
