import json

import pytest

from studioforge.footprints import resolve_footprint, validate_footprint
from studioforge.llm.base import MockLLM, extract_json
from studioforge.model import PartDef, PinDef
from studioforge.parts import docs
from studioforge.parts.db import PartNotFound


def test_footprint_generators_cover_pins(lib):
    for p in lib.all():
        fp = resolve_footprint(p.footprint)
        nums = {x.number for x in p.pins if x.etype != "nc"}
        assert not validate_footprint(fp, nums or None), (p.mpn, validate_footprint(fp, nums))


def test_soic8_geometry_matches_kicad():
    fp = resolve_footprint("SOIC-8")
    pads = {p.number: p for p in fp.pads}
    assert (pads["1"].x, pads["1"].y) == (-2.475, -1.905) and (pads["5"].x, pads["5"].y) == (2.475, 1.905)
    assert pads["1"].w == 1.95 and pads["1"].h == 0.6


def test_lqfp48_pin1_and_count():
    fp = resolve_footprint("LQFP-48")
    assert len(fp.pads) == 48
    p1 = next(p for p in fp.pads if p.number == "1")
    assert p1.x < 0 and p1.y < 0


def test_unknown_footprint_is_a_clear_error():
    with pytest.raises(KeyError):
        resolve_footprint("BGA-9999")


def test_catalog_has_seed_and_generics(lib):
    assert lib.count() >= 15
    r = lib.resistor(4700, "0402")
    assert r.mpn == "RES-4.7k-0402-1%" and r.params["ohms"] == 4700
    c = lib.mlcc(10e-6, 25)
    assert c.category == "capacitor.mlcc"
    assert lib.get("TPS563201DDCR").params["vref"] == 0.768


def test_parametric_find_is_cheapest_first(lib):
    mos = lib.find("mosfet", {"vds_max": (">=", 100), "id_max": (">=", 50)})
    assert [p.mpn for p in mos] == ["IRFB4110PBF"]
    bucks = lib.find("ic.regulator.buck", {"vin_max": (">=", 12)})
    assert bucks[0].mpn == "TPS563201DDCR"           # cheaper than TPS54360B


def test_miss_without_research_raises_and_logs(lib):
    with pytest.raises(PartNotFound):
        lib.require("ic.regulator.buck", {"vin_max": (">=", 100)})
    assert lib.pending_misses()


def test_research_ingest_with_mock_llm(lib, tmp_path):
    rec = {"mpn": "LM5164DDAR", "manufacturer": "TI", "description": "100V buck", "category": "ic.regulator.buck", "package": "SO-PowerPAD-8",
           "pins": [{"number": str(i), "name": n, "type": "passive"} for i, n in enumerate(["BST", "VIN", "EN", "RT", "FB", "GND", "SW", "PAD"], 1)],
           "footprint": "SOIC-8-EP", "params": {"vin_max": 100, "iout_max": 1.0},
           "offers": [{"supplier": "digikey", "sku": "296-X", "url": "https://www.digikey.com/x", "unit_price": 3.1}]}
    lib.llm = MockLLM(lambda p, kw: rec)
    lib.auto_research = True
    part = lib.require("ic.regulator.buck", {"vin_max": (">=", 90)})
    assert part.mpn == "LM5164DDAR" and part.provenance == "researched" and part.verified is False
    assert lib.get("LM5164DDAR").offers[0].sku == "296-X"
    # second request is served from the cache with no further LLM call
    n = len(lib.llm.calls)
    assert lib.require("ic.regulator.buck", {"vin_max": (">=", 90)}).mpn == "LM5164DDAR"
    assert len(lib.llm.calls) == n


def test_research_rejects_bad_footprint_then_repairs(lib):
    bad = {"mpn": "X1", "category": "ic.misc", "package": "SOIC-8", "pins": [{"number": "1", "name": "A", "type": "passive"}, {"number": "9", "name": "B", "type": "passive"}], "footprint": "SOIC-8"}
    good = dict(bad, mpn="X1", pins=[{"number": "1", "name": "A", "type": "passive"}, {"number": "8", "name": "B", "type": "passive"}])
    replies = iter([bad, good])
    lib.llm = MockLLM(lambda p, kw: next(replies))
    lib.auto_research = True
    part = lib.research("x1")
    assert part.mpn == "X1" and len(lib.llm.calls) == 2
    assert "footprint" in lib.llm.calls[1][0].lower() or "pins without pads" in lib.llm.calls[1][0].lower()


def test_pin_evidence_heuristic():
    text = "Pin Functions\nNAME NO. I/O\nGND 1 -\nSW 2 O\nVIN 3 I\nVFB 4 I\n"
    ev = docs.pin_evidence(text, [("1", "GND"), ("2", "SW"), ("3", "VIN"), ("4", "VFB"), ("5", "BOGUS")])
    assert ev["1"] and ev["2"] and ev["3"] and ev["4"] and not ev["5"]


def test_extract_json_variants():
    assert extract_json('text ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('noise {"a": {"b": [1,2]}} more') == {"a": {"b": [1, 2]}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_part_roundtrip(lib):
    p = lib.get("IR2104SPBF")
    q = PartDef.from_dict(json.loads(json.dumps(p.to_dict())))
    assert q.mpn == p.mpn and [x.number for x in q.pins] == [x.number for x in p.pins]


def _fake_kicad(tmp_path, monkeypatch):
    from studioforge import footprints as fpm
    lib = tmp_path / "kicad" / "Connector_AMASS.pretty"
    lib.mkdir(parents=True)
    (lib / "AMASS_XT60PW-M_1x02_P7.20mm_Horizontal.kicad_mod").write_text("""(footprint "AMASS_XT60PW-M_1x02_P7.20mm_Horizontal" (layer "F.Cu")
      (pad "1" thru_hole circle (at 0 0) (size 4.1 4.1) (drill 2.7) (layers "*.Cu" "*.Mask"))
      (pad "2" thru_hole circle (at 7.2 0) (size 4.1 4.1) (drill 2.7) (layers "*.Cu" "*.Mask"))
      (pad "" np_thru_hole oval (at -3.15 -6) (size 1.7 1.7) (drill oval 1.7 2.4) (layers "*.Cu" "*.Mask")))""")
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path / "kicad"))
    fpm._REGISTRY.pop("XT60PW", None)
    return fpm


def test_kicad_library_overrides_generator_and_is_reported(lib, tmp_path, monkeypatch):
    """KiCad's local library (a stub tree via KICAD_FOOTPRINT_DIR) replaces the generator and is recorded as the source."""
    from studioforge import footprint_sync as fs
    from studioforge.model import Circuit
    fpm = _fake_kicad(tmp_path, monkeypatch)
    try:
        c = Circuit("t")
        c.add(lib.get("XT60PW-M"), prefix="J")
        info = fs.sync_circuit(c)
        assert info["XT60PW"] == "kicad:Connector_AMASS:AMASS_XT60PW-M_1x02_P7.20mm_Horizontal"
        assert c.calcs["footprint_sources"] == info
        fp = c.components["J1"].footprint
        assert max(p.drill for p in fp.pads) == 2.7 and fp.lib_id.startswith("Connector_AMASS:")
        assert any(p.slot > 0 for p in fp.pads)                              # slotted locating peg survives
    finally:
        fpm._REGISTRY.pop("XT60PW", None)


def test_generator_is_offline_fallback(monkeypatch, tmp_path):
    from studioforge import footprints as fpm
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))                 # no *.pretty here
    monkeypatch.setattr(fpm, "kicad_dir", lambda: None)
    fpm._REGISTRY.pop("SOIC-8", None)
    try:
        fp = fpm.resolve_footprint("SOIC-8")
        assert fp.source.startswith("generator:") and fpm.search_footprints("SOIC-8") == []
    finally:
        fpm._REGISTRY.pop("SOIC-8", None)


@pytest.mark.skipif(not __import__("studioforge.footprints", fromlist=["x"]).kicad_available(), reason="KiCad libraries not installed")
class TestRealKicad:
    def test_lib_id_and_legacy_resolve_to_kicad(self):
        a = resolve_footprint("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
        b = resolve_footprint("SOIC-8")
        assert a.source == "kicad:Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" and b.source == a.source and b.lib_id == a.lib_id
        assert len(a.pads) == 8

    def test_xt60pw_has_kicad_drills_and_slots(self):
        fp = resolve_footprint("XT60PW")
        assert max(p.drill for p in fp.pads) == pytest.approx(2.7) and any(p.slot > 0 for p in fp.pads)

    def test_unknown_lib_id(self):
        with pytest.raises(KeyError):
            resolve_footprint("Package_SO:NOPE_123")

    def test_all_seed_parts_use_kicad(self, lib):
        from studioforge.footprint_sync import sync_circuit
        from studioforge.model import Circuit
        c = Circuit("all")
        for p in lib.all():
            c.add(p, prefix="U")
        info = sync_circuit(c)
        assert all(v.startswith("kicad:") for v in info.values()), info

    def test_search_and_package_mapping(self):
        from studioforge.footprints import find_footprints, footprint_for_package
        r = find_footprints("SOIC-8", pins=8)
        assert r and r[0]["lib_id"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
        assert footprint_for_package("SOT-23-6", 6).lib_id == "Package_TO_SOT_SMD:SOT-23-6"
        assert footprint_for_package("SO-8", 8).lib_id.startswith("Package_SO:SOIC-8")
        assert footprint_for_package("Package_SO:TSSOP-16_4.4x5mm_P0.65mm", 16).pads

    def test_parser_robustness_over_libraries(self):
        """Every footprint of a sample of libraries parses; rotated/custom/roundrect pads give finite boxes."""
        from studioforge import footprints as fpm
        from studioforge.kicad_mod import parse_footprint
        n = 0
        for lb in ("Package_QFP", "Package_DFN_QFN", "Connector_USB", "Package_BGA", "Connector_Molex"):
            for name in fpm._names(lb)[:60]:
                fp = parse_footprint(fpm.kicad_mod_path(f"{lb}:{name}").read_text())
                assert fp.pads and all(p.w > 0 and p.h > 0 for p in fp.pads), (lb, name)
                x0, y0, x1, y1 = fp.courtyard
                assert x1 > x0 and y1 > y0
                n += 1
        assert n > 100

    def test_rotated_and_custom_pad_bbox(self):
        from studioforge.kicad_mod import parse_footprint
        fp = parse_footprint('''(footprint "X"
          (pad "1" smd rect (at 1 2 90) (size 2 1) (layers "F.Cu" "F.Mask"))
          (pad "2" smd custom (at 5 0) (size 1 1) (layers "F.Cu" "F.Mask")
            (primitives (gr_poly (pts (xy 0 0) (xy 3 0) (xy 3 1) (xy 0 1)) (width 0) (fill yes))))
          (pad "3" smd roundrect (at 0 0 45) (size 2 2) (layers "F.Cu") (roundrect_rratio 0.1)))''')
        p1, p2, p3 = fp.pads
        assert (p1.w, p1.h) == (1, 2)
        assert p2.w == pytest.approx(3.5) and p2.x == pytest.approx(6.25)
        assert p3.w == pytest.approx(2 * 2 ** 0.5) and p3.rratio == pytest.approx(0.1)


def test_compare_flags_drill_differences():
    from studioforge import footprint_sync as fs
    a, b = resolve_footprint("XT60PW"), resolve_footprint("XT60PW")
    assert fs.compare(a, b) == []
    from dataclasses import replace
    b2 = type(b)(b.name, [replace(p, drill=2.7) for p in b.pads], b.courtyard)
    assert any("drill" in d for d in fs.compare(a, b2))


def test_kicad_parser_drops_paste_only_pads():
    from studioforge.kicad_mod import parse_footprint
    fp = parse_footprint('(footprint "X" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")) (pad "" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Paste")))')
    assert len(fp.pads) == 1
