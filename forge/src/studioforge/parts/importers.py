"""Bulk catalog importers (grow the 'large parts list' from public data)."""
from __future__ import annotations

import csv
import re

from ..model import Offer, PartDef, PinDef
from .suppliers import search_url


def import_jlcpcb_csv(lib, path: str, limit: int | None = None) -> int:
    """Import JLCPCB's public parts-library CSV (LCSC Part #, First Category, Second Category, MFR.Part #, Package,
    Manufacturer, Description, Datasheet, Price, Stock). Pinouts are unknown: parts are imported with 2 passive pins
    for two-terminal categories and flagged unverified, so they are usable for passives but ICs still go through research."""
    n = 0
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            mpn = (row.get("MFR.Part #") or row.get("MFR.Part") or "").strip()
            lcsc = (row.get("LCSC Part #") or row.get("LCSC Part") or "").strip()
            if not mpn:
                continue
            cat = (row.get("First Category") or "").lower()
            pins = [PinDef("1", "1"), PinDef("2", "2")] if any(k in cat for k in ("resistor", "capacitor", "inductor", "diode", "led", "fuse", "crystal")) else []
            price = None
            m = re.search(r"\d+-\d+:?\s*\$?([\d.]+)", row.get("Price", "") or "")
            if m:
                price = float(m.group(1))
            pkg = (row.get("Package") or "").strip()
            fp = f"R_{pkg}" if pkg in ("0402", "0603", "0805", "1206") and "resistor" in cat else ""
            if not fp or not pins:
                continue
            offer = Offer("lcsc", lcsc, f"https://www.lcsc.com/product-detail/{lcsc}.html" if lcsc else search_url("lcsc", mpn), price, [], 1,
                          int(row.get("Stock") or 0) if (row.get("Stock") or "").isdigit() else None, price is None, "")
            lib.put(PartDef(mpn, row.get("Manufacturer", ""), row.get("Description", ""), "resistor", pkg, pins, fp, {}, "", offers=[offer],
                            datasheet_url=row.get("Datasheet", ""), provenance="imported", tags=["jlcpcb"]))
            n += 1
            if limit and n >= limit:
                break
    return n
