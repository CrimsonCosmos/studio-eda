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


def test_official_footprint_overrides_generator_and_is_reported(lib, tmp_path, monkeypatch):
    """Official KiCad geometry (here a stub .kicad_mod served from a fake cache) replaces the generator and is recorded as the source."""
    from studioforge import footprint_sync as fs, footprints as fpm
    from studioforge.model import Circuit
    xt = """(footprint "XT" (layer "F.Cu")
      (pad "1" thru_hole circle (at 0 0) (size 4.1 4.1) (drill 2.7) (layers "*.Cu" "*.Mask"))
      (pad "2" thru_hole circle (at 7.2 0) (size 4.1 4.1) (drill 2.7) (layers "*.Cu" "*.Mask"))
      (pad "" np_thru_hole circle (at -3.15 -6) (size 1.7 1.7) (drill 1.7) (layers "*.Cu" "*.Mask")))"""
    monkeypatch.setattr(fs, "fetch", lambda name, home=None, timeout=20: (tmp_path / "x.kicad_mod") if name == "XT60PW" else None)
    (tmp_path / "x.kicad_mod").write_text(xt)
    fpm._REGISTRY.pop("XT60PW", None)
    c = Circuit("t")
    j = c.add(lib.get("XT60PW-M"), prefix="J")
    assert max(p.drill for p in j.footprint.pads) == 2.0                  # generator value (known to be wrong)
    info = fs.sync_circuit(c)
    assert info["XT60PW"].startswith("kicad:")
    assert max(p.drill for p in c.components["J1"].footprint.pads) == 2.7  # official value adopted
    fpm._REGISTRY.pop("XT60PW", None)


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
