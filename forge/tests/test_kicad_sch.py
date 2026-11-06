"""KiCad schematic export: parse with kiutils, run through kicad-cli, and check the exported netlist's connectivity
equals the Circuit's. (KiCad 7's kicad-cli has no `sch erc`; it is run only when the installed version provides it.)"""
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from conftest import mcu_circuit
from studioforge.blocks.registry import build_block
from studioforge.export.kicad_sch import to_kicad_sch, write_kicad_sch
from studioforge.kicad_symbols import find_symbols, symbol_for_part, symbols_available

CLI = shutil.which("kicad-cli")
pytestmark = pytest.mark.skipif(not symbols_available(), reason="KiCad symbol libraries not installed")


def _circuits(lib):
    yield mcu_circuit(lib)
    for name, params in [("battery_input", {}), ("buck_converter", {"vin_min": 39, "vin_max": 54.6, "vout": 12, "iout": 3.0}),
                         ("can_node", {}), ("motor_driver_3ph", {"phase_current_a": 8}),
                         ("ldo", {"vin_max": 5.5, "vout": 3.3, "iout": 0.2})]:
        yield build_block(lib, name, params)


def _nets_from_xml(path):
    root = ET.parse(path).getroot()
    nets = {}
    for n in root.iter("net"):
        nets[n.get("name").lstrip("/")] = {(nd.get("ref"), nd.get("pin")) for nd in n.iter("node")}
    return nets


def _circuit_nets(c):
    return {n: {tuple(p.split(".", 1)) for p in pins} for n, pins in c.nets.items() if pins}


def test_symbol_index_finds_kicad_symbols():
    assert find_symbols("resistor", footprint="R_0402_1005Metric")[0].lib_id == "Device:R"
    assert any(s.lib_id.endswith("STM32F103C8Tx") for s in find_symbols("STM32F103C8"))
    assert find_symbols("Conn_01x08", pins=8)[0].lib_id == "Connector_Generic:Conn_01x08"


def test_generic_parts_pick_kicad_symbols(lib):
    assert symbol_for_part(lib.resistor(4700, "0402")) == "Device:R"
    assert symbol_for_part(lib.mlcc(10e-6, 25)) == "Device:C"
    assert symbol_for_part(lib.header(8)) == "Connector_Generic:Conn_01x08"
    assert symbol_for_part(lib.get("ES1J")) == "Device:D"


def test_schematic_parses_with_kiutils_and_has_properties(lib, tmp_path):
    from kiutils.schematic import Schematic
    c = mcu_circuit(lib)
    p = tmp_path / "a.kicad_sch"
    res = write_kicad_sch(c, p)
    sch = Schematic.from_file(str(p))
    assert len(sch.schematicSymbols) == len(c.components)
    by_ref = {s.properties[0].value: s for s in sch.schematicSymbols}
    assert by_ref["C1"].libId == "Device:C"
    props = {pr.key: pr.value for pr in by_ref["C1"].properties}
    assert props["Footprint"].startswith("Capacitor_SMD:C_0402") and props["Value"]
    assert {s.libId for s in sch.libSymbols} >= {"Device:C", "Connector_Generic:Conn_01x08"}
    assert res.symbols["U1"].startswith("studioforge:") or ":" in res.symbols["U1"]
    assert len(sch.globalLabels) >= sum(len(v) for v in c.nets.values())


@pytest.mark.skipif(CLI is None, reason="kicad-cli missing")
def test_netlist_connectivity_matches_circuit(lib, tmp_path):
    for i, c in enumerate(_circuits(lib)):
        sch = tmp_path / f"c{i}.kicad_sch"
        write_kicad_sch(c, sch)
        xml = tmp_path / f"c{i}.xml"
        r = subprocess.run([CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(sch)],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and xml.exists(), (c.name, r.stdout, r.stderr)
        got = _nets_from_xml(xml)
        want = _circuit_nets(c)
        for net, pins in want.items():
            assert got.get(net) == pins, (c.name, net, pins, got.get(net))
        # no KiCad net joins pins that our circuit keeps apart
        owner = {pin: n for n, pins in want.items() for pin in pins}
        for net, pins in got.items():
            assert len({owner[p] for p in pins if p in owner}) <= 1, (c.name, net)
        # footprints are assignable library ids
        root = ET.parse(xml).getroot()
        for comp in root.iter("comp"):
            fp = comp.findtext("footprint") or ""
            assert ":" in fp, (c.name, comp.get("ref"), fp)


@pytest.mark.skipif(CLI is None, reason="kicad-cli missing")
def test_erc_when_available(lib, tmp_path):
    h = subprocess.run([CLI, "sch", "--help"], capture_output=True, text=True).stdout
    if "erc" not in h:
        pytest.skip("this kicad-cli has no `sch erc` (needs KiCad 8+)")
    sch = tmp_path / "e.kicad_sch"
    write_kicad_sch(mcu_circuit(lib), sch)
    r = subprocess.run([CLI, "sch", "erc", "--format", "json", "-o", str(tmp_path / "erc.json"), str(sch)], capture_output=True, text=True)
    assert (tmp_path / "erc.json").exists(), r.stderr
