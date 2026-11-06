"""Purchasing: aggregate every part of every board (x quantity), choose suppliers, and emit
ready-to-use lists. Guaranteed fallback: a shopping list with a link (direct product page, or a
supplier search URL) for every single line, so the user can always buy by hand."""
from __future__ import annotations

import csv
import html
import io
import json
import math
from collections import OrderedDict
from pathlib import Path

from ..export.bom import group_bom
from ..parts.suppliers import DEFAULT_SUPPLIERS, search_url


def _spare(qty: int, category: str) -> int:
    """Order quantity with overage: passives +10% (min +5), others +1 (min) - cheap insurance against loss/misplacement."""
    if category.split(".")[0] in ("resistor", "capacitor", "led", "ferrite", "diode"):
        return qty + max(5, math.ceil(qty * 0.10))
    if category in ("mounting", "testpoint"):
        return qty
    return qty + (1 if qty < 25 else math.ceil(qty * 0.04))


def aggregate(sb) -> list[dict]:
    rows: "OrderedDict[str, dict]" = OrderedDict()
    for bid, info in sb.boards.items():
        qty_boards = next(b.qty for b in sb.resolved.plan.boards if b.id == bid)
        for r in group_bom(info["circuit"]):
            key = r["mpn"]
            e = rows.setdefault(key, {"mpn": r["mpn"], "manufacturer": r["manufacturer"], "description": r["description"], "category": r["category"],
                                      "footprint": r["footprint"], "qty": 0, "used_on": [], "verified": r["verified"], "datasheet": r["datasheet"],
                                      "part": None})
            e["qty"] += r["qty"] * qty_boards
            e["used_on"].append(f"{bid} x{r['qty']}")
    return list(rows.values())


def attach_offers(rows: list[dict], lib) -> None:
    for r in rows:
        part = lib.get(r["mpn"])
        r["part"] = part
        r["qty_order"] = _spare(r["qty"], r["category"])
        best = None
        if part:
            offers = [o for o in part.offers if o.price_at(r["qty_order"]) is not None]
            if offers:
                best = min(offers, key=lambda o: o.price_at(r["qty_order"]))
        if best is None and part and part.offers:
            best = part.offers[0]
        r["offer"] = best
        if best:
            r["supplier"] = best.supplier
            r["url"] = best.url or search_url(best.supplier if best.supplier in ("digikey", "mouser", "lcsc") else "digikey", r["mpn"])
            r["sku"] = best.sku
            r["unit_price"] = best.price_at(r["qty_order"])
            r["price_estimated"] = best.estimated
            r["direct"] = bool(best.sku)           # a real SKU/product page vs a search link
        else:
            r["supplier"], r["url"], r["sku"], r["unit_price"], r["price_estimated"], r["direct"] = "digikey", search_url("digikey", r["mpn"]), "", None, True, False


def build_purchase_files(sb, lib, outdir: str | Path | None = None) -> dict:
    out = Path(outdir) if outdir else sb.out / "purchase"
    out.mkdir(parents=True, exist_ok=True)
    rows = aggregate(sb)
    attach_offers(rows, lib)
    items = sb.resolved.plan.purchased
    plan = {"parts": [], "items": [], "totals": {}}
    by_supplier: dict[str, list[dict]] = {}
    parts_total = 0.0
    for r in rows:
        ext = (r["unit_price"] or 0.0) * r["qty_order"]
        parts_total += ext
        rec = {"mpn": r["mpn"], "manufacturer": r["manufacturer"], "description": r["description"], "qty_needed": r["qty"], "qty_order": r["qty_order"],
               "supplier": r["supplier"], "url": r["url"], "sku": r["sku"], "unit_price_usd": r["unit_price"], "ext_price_usd": round(ext, 2),
               "price_is_estimate": r["price_estimated"], "direct_link": r["direct"], "datasheet": r["datasheet"], "used_on": r["used_on"],
               "pinout_verified": r["verified"]}
        plan["parts"].append(rec)
        by_supplier.setdefault(r["supplier"], []).append(rec)
    for p in items:
        plan["items"].append({"id": p.id, "title": p.title, "category": p.category, "qty": p.qty, "unit_cost_usd": p.unit_cost_usd,
                              "ext_usd": round(p.unit_cost_usd * p.qty, 2), "spec": p.spec, "url": p.url, "supplier": p.supplier,
                              "estimate": p.cost_is_estimate, "needs_manual_sourcing": not bool(p.url)})
    plan["totals"] = {"parts_usd_est": round(parts_total, 2), "purchased_items_usd": round(sum(p.unit_cost_usd * p.qty for p in items), 2),
                      "lines": len(rows), "suppliers": sorted(by_supplier)}
    (out / "purchase_plan.json").write_text(json.dumps(plan, indent=1))
    files = {"plan": out / "purchase_plan.json"}
    # per-supplier import CSVs
    for sup, lst in by_supplier.items():
        buf = io.StringIO()
        w = csv.writer(buf)
        if sup == "lcsc":
            w.writerow(["LCSC Part Number", "Manufacturer Part Number", "Quantity", "Customer Reference"])
            for r in lst:
                w.writerow([r["sku"], r["mpn"], r["qty_order"], ",".join(r["used_on"])[:60]])
        else:
            w.writerow(["Manufacturer Part Number", "Quantity", "Customer Reference", "Supplier Part Number"])
            for r in lst:
                w.writerow([r["mpn"], r["qty_order"], ",".join(r["used_on"])[:60], r["sku"]])
        p = out / f"cart_{sup}.csv"
        p.write_text(buf.getvalue())
        files[f"cart_{sup}"] = p
    # markdown + html shopping list
    md = ["# Shopping list", "", "Every line has a link. 'search' links open the supplier's search for the exact MPN; verify the match before ordering.", "",
          "## Electronic components", "", "| Qty (order) | MPN | Description | Supplier | Link | Unit USD | Ext USD |", "|--|--|--|--|--|--|--|"]
    for rec in plan["parts"]:
        kind = "product" if rec["direct_link"] else "search"
        up = f"{rec['unit_price_usd']:.4f}{'*' if rec['price_is_estimate'] else ''}" if rec["unit_price_usd"] is not None else "?"
        md.append(f"| {rec['qty_order']} ({rec['qty_needed']} needed) | {rec['mpn']} | {rec['description'][:60]} | {rec['supplier']} | [{kind}]({rec['url']}) | {up} | {rec['ext_price_usd']:.2f} |")
    md += ["", f"Components subtotal (estimated unit prices, includes spares): **${plan['totals']['parts_usd_est']:,.2f}**", "",
           "## Purchased assemblies / modules / mechanical", "", "| Qty | Item | Category | Unit USD | Ext USD | Source |", "|--|--|--|--|--|--|"]
    for it in plan["items"]:
        src = f"[{it['supplier'] or 'link'}]({it['url']})" if it["url"] else "**needs manual sourcing** (search: " + (it["supplier"] or it["title"]) + ")"
        md.append(f"| {it['qty']} | {it['title']} | {it['category']} | {it['unit_cost_usd']:,.2f}{'*' if it['estimate'] else ''} | {it['ext_usd']:,.2f} | {src} |")
    md += ["", "\\* planning estimate, not a live quote."]
    (out / "shopping_list.md").write_text("\n".join(md) + "\n")
    files["shopping_list"] = out / "shopping_list.md"
    rowsh = "".join(f"<tr><td>{r['qty_order']}</td><td>{html.escape(r['mpn'])}</td><td>{html.escape(r['description'][:70])}</td><td>{r['supplier']}</td>"
                    f"<td><a href=\"{html.escape(r['url'])}\">{'product' if r['direct_link'] else 'search'}</a></td></tr>" for r in plan["parts"])
    rowsi = "".join(f"<tr><td>{p['qty']}</td><td>{html.escape(p['title'])}</td><td>${p['ext_usd']:,.2f}</td><td>"
                    f"{('<a href=%s>link</a>' % chr(34) + html.escape(p['url']) + chr(34)) if False else (html.escape(p['url']) if p['url'] else 'manual sourcing')}</td></tr>" for p in plan["items"])
    (out / "shopping_list.html").write_text(f"<!doctype html><meta charset=utf-8><title>Shopping list</title><body style='font-family:sans-serif'>"
                                           f"<h1>Components</h1><table border=1 cellpadding=4>{rowsh}</table><h1>Assemblies</h1><table border=1 cellpadding=4>{rowsi}</table>")
    files["html"] = out / "shopping_list.html"
    return {"files": files, "plan": plan}
