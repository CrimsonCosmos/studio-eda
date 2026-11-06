"""Part research agent.

When the catalog has no part for a requirement, Claude Code (headless, with web
search/fetch and curl) is asked to find a real part, download its *official*
documentation into the cache and report a strict JSON record including where to
buy it. Deterministic code then validates the record, cross-checks it against the
downloaded datasheet text, and stores the part. LLM output is never trusted blindly.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..footprints import register, resolve_footprint, validate_footprint
from ..llm.base import LLMUnavailable, extract_json
from ..llm.claude_code import RESEARCH_TOOLS
from ..model import Footprint, Offer, PartDef, PinDef, PIN_TYPES
from . import docs

SCHEMA = """{
  "mpn": "exact manufacturer part number",
  "manufacturer": "...",
  "description": "one line",
  "category": "dotted category, e.g. ic.regulator.buck | mosfet | ic.mcu | connector.header | ic.interface.can | sensor.imu",
  "package": "package name as in the datasheet",
  "pins": [{"number": "1", "name": "VIN", "type": "power_in|power_out|input|output|bidir|passive|nc|open_collector|tristate", "alt": ["optional alternate functions"]}],
  "footprint": "a name known to the generator list (e.g. SOIC-8, SOT-23-6, LQFP-48, QFN-32-5x5-EP3.1, TO-220-3, D_SMA) OR an object {\\"name\\":..., \\"pads\\":[{\\"number\\":\\"1\\",\\"x\\":-2.475,\\"y\\":-1.905,\\"w\\":1.95,\\"h\\":0.6,\\"shape\\":\\"rect|oval|roundrect|circle\\",\\"drill\\":0}], \\"courtyard\\":[xmin,ymin,xmax,ymax], \\"height\\":1.5} taken from the datasheet land-pattern drawing (mm, y down, pin 1 first)",
  "params": {"numeric electrical limits with consistent keys: vin_max, vin_min, iout_max, vds_max, id_max, rds_on, vcc_min, vcc_max, ..."},
  "datasheet_url": "official manufacturer URL of the datasheet PDF",
  "datasheet_file": "relative path of the PDF you saved (datasheet.pdf)",
  "product_page_url": "...",
  "offers": [{"supplier": "digikey|mouser|lcsc|arrow|farnell|...", "sku": "supplier part number", "url": "direct product page URL", "unit_price": 1.23, "stock": 1000, "moq": 1}],
  "emi": "aggressor|victim|neutral",
  "heat": "hot|warm|cool",
  "height": 1.5,
  "alternates": ["pin-compatible or equivalent MPNs"],
  "notes": "anything an engineer must know (errata, sourcing, lifecycle)"
}"""

SYSTEM = (
    "You are a meticulous electronics component librarian working for an automated PCB design pipeline. "
    "Rules: (1) Use only information you actually found; never invent MPNs, pinouts, prices, SKUs or URLs. "
    "(2) Prefer the manufacturer's own site for the datasheet; download the real PDF into the current directory "
    "with curl -L (check it starts with %PDF) and also save other official documents (user guide, reference manual, "
    "application notes that define layout) under ./docs/. (3) Pin tables must come from the datasheet pinout table, "
    "complete and exact, including thermal/exposed pads and no-connects. (4) Offers must be real product-page URLs on "
    "authorized distributors; if a site blocks you, still give the search URL and set unit_price to null. "
    "(5) Final answer: ONLY one JSON object matching the schema, no prose.")


def build_prompt(spec: str, where: dict | None, category_hint: str, feedback: str = "") -> str:
    req = spec
    if where:
        req += "\nHard requirements: " + ", ".join(f"{k} {op} {v}" for k, (op, v) in where.items())
    if category_hint:
        req += f"\nCategory: {category_hint}"
    p = (f"Find ONE real, currently-orderable electronic component that satisfies:\n{req}\n\n"
         f"Prefer parts that are in stock at Digi-Key, Mouser or LCSC, in a hand-solderable or standard SMT package, "
         f"and low cost, but correctness of the pinout beats price.\n"
         f"Steps: search the web, open the manufacturer datasheet, download it to ./datasheet.pdf, collect pinout, "
         f"package, ratings and distributor links. Return JSON in this schema:\n{SCHEMA}\n")
    if feedback:
        p += f"\nYour previous answer was rejected by validation. Fix these problems and answer again:\n{feedback}\n"
    return p


class ResearchError(RuntimeError):
    pass


def research_part(lib, spec: str, category_hint: str = "", where: dict | None = None, retries: int = 2) -> PartDef:
    if lib.llm is None:
        raise LLMUnavailable("no LLM configured for part research")
    scratch = lib.root / "docs" / "_pending"
    scratch.mkdir(parents=True, exist_ok=True)
    feedback = ""
    last: Exception | None = None
    for attempt in range(retries + 1):
        res = lib.llm.run(build_prompt(spec, where, category_hint, feedback), system=SYSTEM, tools=RESEARCH_TOOLS,
                          cwd=scratch, json_output=True, timeout=1500, budget_usd=3.0)
        rec = res.data if isinstance(res.data, dict) else extract_json(res.text)
        try:
            return ingest_record(lib, rec, scratch, where)
        except ResearchError as e:
            feedback = str(e)
            last = e
    raise ResearchError(f"research for '{spec}' failed validation after {retries + 1} attempts: {last}")


def ingest_record(lib, rec: dict, scratch: Path, where: dict | None = None) -> PartDef:
    errs: list[str] = []
    for k in ("mpn", "pins", "package", "footprint", "category"):
        if not rec.get(k):
            errs.append(f"missing required field '{k}'")
    if errs:
        raise ResearchError("; ".join(errs))
    mpn = str(rec["mpn"]).strip()
    pins: list[PinDef] = []
    seen: set[str] = set()
    for p in rec["pins"]:
        et = p.get("type", "passive")
        if et not in PIN_TYPES:
            errs.append(f"pin {p.get('number')}: bad type '{et}'")
            et = "passive"
        num = str(p["number"])
        if num in seen:
            errs.append(f"duplicate pin number {num}")
        seen.add(num)
        pins.append(PinDef(num, str(p["name"]), et, tuple(p.get("alt", ()))))
    # footprint
    fpspec = rec["footprint"]
    fp = None
    if isinstance(fpspec, str):
        try:
            fp = resolve_footprint(fpspec)
        except KeyError:
            errs.append(f"footprint '{fpspec}' is not a known generator name; provide pad geometry from the datasheet")
    elif isinstance(fpspec, dict):
        try:
            pads = []
            from ..model import Pad
            for q in fpspec["pads"]:
                pads.append(Pad(str(q["number"]), float(q["x"]), float(q["y"]), float(q["w"]), float(q["h"]),
                                q.get("shape", "rect"), float(q.get("drill", 0) or 0), True, not q.get("drill")))
            xs0 = min(p.x - p.w / 2 for p in pads); xs1 = max(p.x + p.w / 2 for p in pads)
            ys0 = min(p.y - p.h / 2 for p in pads); ys1 = max(p.y + p.h / 2 for p in pads)
            court = tuple(fpspec.get("courtyard") or (xs0 - 0.3, ys0 - 0.3, xs1 + 0.3, ys1 + 0.3))
            fp = Footprint(f"{mpn}_{fpspec.get('name', 'custom')}", pads, court, [], float(fpspec.get("height", rec.get("height", 1.0))),
                           "datasheet:" + (rec.get("datasheet_url") or "agent"))
        except Exception as e:
            errs.append(f"bad footprint object: {e}")
    if fp is not None:
        for pr in validate_footprint(fp, {p.number for p in pins if p.etype != "nc"}):
            errs.append("footprint: " + pr)
    if errs:
        raise ResearchError("; ".join(errs))
    register(fp, fp.name)

    # documentation
    ddir = lib.doc_dir(mpn)
    pdf = ddir / "datasheet.pdf"
    src = scratch / str(rec.get("datasheet_file") or "datasheet.pdf")
    if not (pdf.exists() and docs.is_pdf(pdf)):
        if src.exists() and docs.is_pdf(src):
            pdf.write_bytes(src.read_bytes())
        elif rec.get("datasheet_url"):
            ok, msg = docs.download_pdf(rec["datasheet_url"], pdf)
            if not ok:
                lib.research_log.append(f"{mpn}: datasheet download failed: {msg}")
    for extra in (scratch / "docs").glob("*") if (scratch / "docs").exists() else []:
        if extra.is_file():
            (ddir / "docs").mkdir(exist_ok=True)
            extra.replace(ddir / "docs" / extra.name)
    for f in list(scratch.glob("*.pdf")) + list(scratch.glob("*.txt")) + list(scratch.glob("*.html")):
        try:
            f.unlink()
        except OSError:
            pass

    doc_rel: list[str] = []
    verified = False
    notes = str(rec.get("notes", ""))
    if pdf.exists() and docs.is_pdf(pdf):
        doc_rel.append(str(pdf.relative_to(lib.root)))
        for f in (ddir / "docs").glob("*") if (ddir / "docs").exists() else []:
            doc_rel.append(str(f.relative_to(lib.root)))
        txt = docs.pdf_to_text(pdf)
        if txt:
            names = [p.name for p in pins if p.etype != "nc"]
            found, missing = docs.check_pin_names(txt.read_text(errors="ignore"), names)
            verified = bool(found) and not missing
            notes += f" [datasheet cross-check: {len(found)} pin names found, missing={missing}]"
        (ddir / "sha256.txt").write_text(docs.sha256(pdf) + "  datasheet.pdf\n")
    else:
        notes += " [WARNING: official datasheet PDF could not be cached; pinout unverified]"

    offers = []
    for o in rec.get("offers", []) or []:
        offers.append(Offer(str(o.get("supplier", "")).lower(), str(o.get("sku", "") or ""), str(o.get("url", "") or ""),
                            o.get("unit_price"), [], int(o.get("moq") or 1), o.get("stock"),
                            o.get("unit_price") is None, ""))
    if not offers:
        from .suppliers import search_offers
        offers = search_offers(mpn, None)
        notes += " [no direct offers found; search links only]"
    part = PartDef(mpn, str(rec.get("manufacturer", "")), str(rec.get("description", "")), str(rec["category"]),
                   str(rec["package"]), pins, fp.name, dict(rec.get("params") or {}), "", rec.get("emi", "neutral"),
                   rec.get("heat", "cool"), float(rec.get("height") or fp.height), str(rec.get("datasheet_url", "")),
                   doc_rel, offers, list(rec.get("alternates") or []), ["researched"], "researched", verified, notes.strip())
    if rec.get("product_page_url"):
        part.notes += f" product page: {rec['product_page_url']}"
    # footprint stored inline when it is not reproducible from a generator name
    if not isinstance(rec["footprint"], str):
        part.footprint = fp
    lib.put(part)
    (ddir / "part.json").write_text(json.dumps(part.to_dict(), indent=1))
    return part
