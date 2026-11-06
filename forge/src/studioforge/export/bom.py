"""BOM, pick-and-place and purchasing-oriented part lists."""
from __future__ import annotations

import csv
import io
from collections import OrderedDict

from ..model import Circuit
from ..pcb.board import Board


def group_bom(circuit: Circuit) -> list[dict]:
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for ref, c in circuit.components.items():
        if c.dnp or c.part.category in ("mounting", "testpoint"):
            key = ("__mech__", c.part.mpn)
        else:
            key = (c.part.mpn, c.value, c.part.footprint if isinstance(c.part.footprint, str) else c.part.footprint.name)
        g = groups.setdefault(key, {"refs": [], "part": c.part, "value": c.value, "dnp": c.dnp or c.part.category in ("mounting", "testpoint")})
        g["refs"].append(ref)
    rows = []
    for key, g in groups.items():
        p = g["part"]
        offer = p.best_offer(len(g["refs"]))
        lcsc = next((o.sku for o in p.offers if o.supplier == "lcsc" and o.sku), "")
        rows.append({
            "refs": sorted(g["refs"], key=lambda r: (r.rstrip("0123456789"), int("".join(ch for ch in r if ch.isdigit()) or 0))),
            "qty": len(g["refs"]), "mpn": p.mpn, "manufacturer": p.manufacturer, "value": g["value"],
            "footprint": p.footprint if isinstance(p.footprint, str) else p.footprint.name, "package": p.package,
            "description": p.description, "category": p.category, "dnp": g["dnp"],
            "lcsc": lcsc, "unit_price": offer.price_at(len(g["refs"])) if offer else None,
            "price_estimated": offer.estimated if offer else True, "verified": p.verified, "provenance": p.provenance,
            "datasheet": p.datasheet_url,
            "links": {o.supplier: (o.url) for o in p.offers if o.url},
        })
    rows.sort(key=lambda r: (r["refs"][0].rstrip("0123456789"), r["mpn"]))
    return rows


def bom_csv(circuit: Circuit) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Designator", "Qty", "MPN", "Manufacturer", "Value", "Footprint", "Description", "Unit price USD (est.)",
                "Ext price USD", "Datasheet", "Digi-Key", "Mouser", "LCSC", "Pinout verified"])
    for r in group_bom(circuit):
        up = r["unit_price"]
        w.writerow([",".join(r["refs"]), r["qty"], r["mpn"], r["manufacturer"], r["value"], r["footprint"], r["description"],
                    f"{up:.4f}" if up is not None else "", f"{up * r['qty']:.2f}" if up is not None else "", r["datasheet"],
                    r["links"].get("digikey", ""), r["links"].get("mouser", ""), r["links"].get("lcsc", ""),
                    "yes" if r["verified"] else "NO"])
    return out.getvalue()


def bom_jlcpcb(circuit: Circuit) -> str:
    """JLCPCB assembly BOM columns (Comment, Designator, Footprint, LCSC Part #)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for r in group_bom(circuit):
        if r["dnp"]:
            continue
        w.writerow([r["value"] or r["mpn"], ",".join(r["refs"]), r["footprint"], r["lcsc"]])
    return out.getvalue()


def cpl_csv(board: Board) -> str:
    """Pick-and-place (JLCPCB column names). Origin = board bottom-left, y up, mm.
    NOTE: assemblers apply per-part rotation offsets; verify rotation of polarised parts."""
    H = board.height
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"])
    for ref, comp in board.circuit.components.items():
        if comp.dnp or comp.part.category in ("mounting", "testpoint"):
            continue
        pl = board.place[ref]
        rot = (-pl.rot) % 360        # KiCad file rotation is CCW; assembler y-up CCW
        w.writerow([ref, comp.value, comp.part.package, f"{pl.x:.3f}mm", f"{H - pl.y:.3f}mm", f"{rot:g}", "Top"])
    return out.getvalue()


def cpl_generic(board: Board) -> str:
    H = board.height
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Ref", "Val", "MPN", "Package", "PosX", "PosY", "Rot", "Side"])
    for ref, comp in board.circuit.components.items():
        pl = board.place[ref]
        w.writerow([ref, comp.value, comp.part.mpn, comp.part.package, f"{pl.x:.3f}", f"{H - pl.y:.3f}", f"{(-pl.rot) % 360:g}", "top"])
    return out.getvalue()


def bom_cost(circuit: Circuit, boards: int = 1) -> dict:
    rows = group_bom(circuit)
    total = 0.0
    unknown = 0
    for r in rows:
        if r["unit_price"] is None:
            unknown += 1
            continue
        total += r["unit_price"] * r["qty"] * boards
    return {"lines": len(rows), "parts": sum(r["qty"] for r in rows), "total_usd": round(total, 2), "lines_unpriced": unknown,
            "estimated": any(r["price_estimated"] for r in rows)}
