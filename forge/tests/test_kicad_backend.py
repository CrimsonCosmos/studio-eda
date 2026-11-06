"""KiCad (kicad-cli / pcbnew) as authoritative DRC + fab-export backend. Skipped when kicad-cli is missing."""
import json
import zipfile

import pytest

from conftest import mcu_circuit
from studioforge import kicad_backend as kb
from studioforge.export.kicad import to_kicad_pcb
from studioforge.export.package import write_package
from studioforge.pcb.flow import BoardSpec, build_board

pytestmark = pytest.mark.skipif(not kb.available() or kb.capabilities()["pcbnew_python"] is None,
                                reason="kicad-cli / pcbnew not installed")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from studioforge.parts.db import PartsLibrary
    lib = PartsLibrary(tmp_path_factory.mktemp("h"), auto_research=False)
    return {n: build_board(mcu_circuit(lib), BoardSpec(layers=n), log=None) for n in (2, 4)}


def _write(res, d):
    d.mkdir(exist_ok=True)
    pcb = d / "t.kicad_pcb"
    pcb.write_text(to_kicad_pcb(res.board, "t"))
    kb.write_kicad_pro(d / "t.kicad_pro", res.board.rules)
    return pcb


def test_capabilities_document_kicad7_limits():
    c = kb.capabilities()
    assert c["version"] and c["kicad_cli"]
    assert "gerbers" in c["export"] and "drill" in c["export"] and "pos" in c["export"]


def test_gerber_layer_sets():
    assert kb.gerber_layers(2)[:2] == ["F.Cu", "B.Cu"]
    assert kb.gerber_layers(4)[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert sum(1 for l in kb.gerber_layers(6) if l.endswith(".Cu")) == 6


@pytest.mark.parametrize("n", [2, 4])
def test_kicad_drc_has_no_errors_on_generated_boards(built, tmp_path, n):
    rep = kb.run_drc(_write(built[n], tmp_path), report_path=tmp_path / "drc.json")
    assert rep.errors == [] and rep.unconnected == [] and rep.footprint_errors == []
    d = json.loads((tmp_path / "drc.json").read_text())
    assert d["clean"] and d["zones_refilled"]


def test_kicad_drc_detects_a_real_short(built, tmp_path):
    pcb = _write(built[2], tmp_path)
    b = built[2].board
    pads = [p for p in b.pads() if p.net and p.net != "GND"]
    a = pads[0]
    other = next(p for p in pads if p.net != a.net)
    txt = pcb.read_text().rstrip()
    assert txt.endswith(")")
    seg = (f'  (segment (start {a.x} {a.y}) (end {other.x} {other.y}) (width 0.2) (layer "F.Cu") '
           f'(net 1) (tstamp "00000000-0000-0000-0000-000000000001"))\n)')
    pcb.write_text(txt[:-1] + seg)
    rep = kb.run_drc(pcb)
    assert not rep.clean and {"shorting_items", "clearance"} & set(rep.by_type)


@pytest.mark.parametrize("n", [2, 4])
def test_export_fab_layer_set_drills_pos_zip(built, tmp_path, n):
    pcb = _write(built[n], tmp_path)
    files = kb.export_fab(pcb, tmp_path / "out", "t")
    names = {k.split(":", 1)[1] for k in files if k.startswith("gerber:")}
    for layer in ("F_Cu", "B_Cu", "F_Mask", "B_Mask", "F_Paste", "F_Silkscreen", "Edge_Cuts"):
        assert any(layer in nm for nm in names), layer
    assert sum(1 for nm in names if "_Cu" in nm and "Edge" not in nm) == n
    assert any(nm.endswith("PTH.drl") for nm in names) and any(nm.endswith("NPTH.drl") for nm in names)
    pos = files["positions"].read_text().splitlines()
    assert len(pos) == len(built[n].board.circuit.components) + 1
    with zipfile.ZipFile(files["fab_zip"]) as z:
        assert len(z.namelist()) >= n + 6


def test_package_uses_kicad_by_default_and_falls_back(built, tmp_path):
    res = built[4]
    files = write_package(res, tmp_path / "k", "t")
    assert (tmp_path / "k" / "kicad_drc.json").exists()
    assert "KiCad DRC (authoritative)" in (tmp_path / "k" / "REPORT.md").read_text()
    assert any(k.startswith("gerber:") and k.endswith(".gtl") for k in files)
    files2 = write_package(res, tmp_path / "b", "t", use_kicad=False)
    assert not (tmp_path / "b" / "kicad_drc.json").exists()
    assert any(k.startswith("gerber:") for k in files2)
