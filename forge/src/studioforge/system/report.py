"""System-level report: cost vs budget, buy/build decisions, power tree, interfaces, harness, issues."""
from __future__ import annotations

from pathlib import Path


def write_system_report(sb, plan) -> Path:
    res = sb.resolved
    L = [f"# {plan.name} - system design", "", f"> Request: {plan.request or '(none)'}", ""]
    c = sb.cost
    L += ["## Cost vs budget (estimates - refresh with live quotes before ordering)", "",
          "| Item | USD |", "|--|--|",
          f"| Custom boards (PCB + parts + assembly) | {c['boards_usd']:,.2f} |", f"| Purchased items | {c['purchased_usd']:,.2f} |",
          f"| Harness / cables | {c['harness_usd']:,.2f} |", f"| Contingency (10%) | {c['contingency_usd']:,.2f} |",
          f"| **Total** | **{c['total_usd']:,.2f}** |"]
    if c.get("budget_usd") is not None:
        L.append(f"| Budget | {c['budget_usd']:,.2f} |")
        L.append(f"| **{'Within budget' if c['within_budget'] else 'OVER BUDGET'}** | {c['budget_usd'] - c['total_usd']:+,.2f} |")
    L += ["", "## Buy vs build decisions", ""] + ([f"- {d}" for d in res.decisions] or ["- no block had a purchasable alternative; everything custom is built"])
    L += ["", "## Boards", "", "| Board | Qty | Parts | Size (mm) | Layers | Unit cost | DRC | Notes |", "|--|--|--|--|--|--|--|--|"]
    for b in plan.boards:
        info = sb.boards.get(b.id, {})
        circ = info.get("circuit")
        r = info.get("result")
        size = f"{r.board.width:.0f} x {r.board.height:.0f}" if r else "-"
        layers = r.board.rules.layers if r else "-"
        drc = ("clean" if r.drc.clean and r.success else r.drc.summary()) if r else "not built"
        L.append(f"| {b.id} | {b.qty} | {len(circ.components) if circ else '-'} | {size} | {layers} | ${info.get('cost', {}).get('per_board', 0):,.2f} | {drc} | {b.title} |")
    L += ["", "## Power tree (demand propagated upstream, windows downstream)", ""]
    for prov, amps in sorted(res.demand.items()):
        L.append(f"- `{prov}` must supply **{amps:.2f} A** (fleet qty x duty applied across boards)")
    L += ["", "## Block parameters decided by the resolver", ""]
    for (bid, blk), pr in sorted(res.params.items()):
        shown = {k: v for k, v in pr.items() if k != "requests"}
        L.append(f"- `{bid}.{blk}`: {shown}" + (f" requests={[(r['name'], r['kind']) for r in pr.get('requests', [])]}" if pr.get("requests") else ""))
    L += ["", "## Interfaces (links)", "", "| A | B | Note |", "|--|--|--|"] + [f"| {l.a} | {l.b} | {l.note} |" for l in res.links]
    L += ["", "## Wiring harness", "", "| From | To | Kind | Wires | AWG | Amps | Volts | Qty | Est $ each |", "|--|--|--|--|--|--|--|--|--|"]
    for h in res.harness:
        L.append(f"| {h['from']} | {h['to']} | {h['kind']} | {h['wires']} | {h['awg']} | {h['amps']} | {h['volts']} | {h['qty']} | {h['cost_usd_each']} |")
    L += ["", "## Purchased items", "", "| Qty | Item | Category | Unit USD | Total USD | Source | Notes |", "|--|--|--|--|--|--|--|"]
    for p in res.plan.purchased:
        link = f"[{p.supplier or 'link'}]({p.url})" if p.url else (p.supplier or "search needed")
        L.append(f"| {p.qty} | {p.title} | {p.category} | {p.unit_cost_usd:,.2f}{'*' if p.cost_is_estimate else ''} | {p.unit_cost_usd * p.qty:,.2f} | {link} | {p.notes} |")
    L += ["", "\\* planning estimate, not a quote.", "", "## Issues", ""]
    issues = list(res.issues) + list(sb.issues)
    L += [f"- **{i.severity}**: {i.msg}" for i in issues] or ["- none"]
    L += ["", "## Assumptions", ""] + [f"- {a}" for a in plan.assumptions] if plan.assumptions else []
    L += ["", "## What this tool does and does not guarantee", "",
          "- Deterministic stages (resolver, placement, routing, DRC, Gerber/drill/BOM export) are reproducible and independently re-checked.",
          "- Electrical design uses datasheet equations but is **not simulated** (loop stability, EMC, thermal in an enclosure). Prototype and measure.",
          "- Mechanical parts, actuators, and software/firmware are outside what this tool designs; they appear as purchased items with planning-grade costs.",
          "- Review the per-board REPORT.md checklist before releasing any board to fabrication."]
    p = sb.out / "SYSTEM.md"
    p.write_text("\n".join(L) + "\n")
    return p
