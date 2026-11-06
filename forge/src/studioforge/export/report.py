"""Human-readable design report with an honest manufacturing-readiness checklist."""
from __future__ import annotations

from ..pcb.flow import BuildResult
from . import bom as bomx


def render_report(res: BuildResult, project: str, extra: dict) -> str:
    b, c, drc, an = res.board, res.board.circuit, res.drc, res.analysis
    rows = bomx.group_bom(c)
    cost = bomx.bom_cost(c)
    L = [f"# {project} - design report", ""]
    L += ["## Summary", "",
          f"- Board: **{b.width:.1f} x {b.height:.1f} mm**, **{b.rules.layers} layers** ({', '.join(b.layers)}), fab profile `{b.rules.profile}`",
          f"- Components: {len(c.components)} ({sum(1 for r in rows)} BOM lines), nets: {len(c.nets)}",
          f"- Copper: {len(b.tracks)} track segments, {len(b.vias)} vias, {len(b.zones)} zones; planes: {b.plane_nets or 'none'}; pours: {list(b.pour_nets) or 'none'}",
          f"- Router: pitch {b.stats.get('grid_pitch')} mm, rip-ups {b.stats.get('ripups', 0)}, neck-downs {b.stats.get('neckdowns', 0)}, unrouted: {b.stats.get('unrouted') or 'none'}",
          f"- DRC: **{'CLEAN' if drc.clean else 'ERRORS'}** ({drc.summary()}); disconnected nets: {drc.stats.get('nets_disconnected', '?')}",
          f"- Estimated BOM cost (1 board): **${cost['total_usd']:.2f}** ({'estimated prices - refresh before ordering' if cost['estimated'] else 'quoted'}), unpriced lines: {cost['lines_unpriced']}",
          ""]
    th = an.get("thermal", {})
    L += ["## Thermal", "", f"- Peak board temperature rise above ambient (steady state, still air): **{th.get('peak_rise_c', 0)} C**"]
    for h in th.get("hot", [])[:8]:
        L.append(f"  - {h['ref']}: {h['power_w']} W -> board +{h['rise_c']} C (peak +{h['peak_c']} C)")
    L += ["", "## EMI / signal-integrity checks", ""]
    for d in an.get("decoupling", []):
        L.append(f"- Decoupling {d['cap']} -> {d['ic_pin']}: {d['dist_mm']} mm (limit {d['limit_mm']}) {'OK' if d['ok'] else '**FAR**'}")
    for d in an.get("loops", []):
        L.append(f"- Hot loop `{d['loop']}`: hull area {d['area_mm2']} mm^2 ({', '.join(d['members'])})")
    for d in an.get("crosstalk", []):
        L.append(f"- Crosstalk risk: {d['victim']} runs {d['coupled_length_mm']} mm within 1 mm of a noisy net on {d['layer']}")
    rp = an.get("reference_plane", {})
    L.append(f"- Tracks over a solid reference plane/pour: {rp.get('over_reference_pct')}% of {rp.get('track_mm')} mm")
    L += ["", "## Warnings", ""]
    allw = list(dict.fromkeys(list(b.warnings) + list(c.warnings) + [v.msg for v in drc.warnings][:20]))
    L += [f"- {w}" for w in allw] or ["- none"]
    if drc.errors:
        L += ["", "## DRC errors", ""] + [f"- {v.kind}: {v.msg}" for v in drc.errors[:40]]
    L += ["", "## Design calculations (from deterministic block builders)", ""]
    for k, v in c.calcs.items():
        L.append(f"- `{k}`: {v}")
    L += ["", "## Bill of materials", "", "| Qty | Designators | MPN | Value | Package | Est. $ | Verified |", "|--|--|--|--|--|--|--|"]
    for r in rows:
        up = f"{r['unit_price']:.3f}" if r["unit_price"] is not None else "?"
        L.append(f"| {r['qty']} | {','.join(r['refs'][:6])}{'...' if len(r['refs']) > 6 else ''} | {r['mpn']} | {r['value']} | {r['package']} | {up} | {'yes' if r['verified'] else 'no'} |")
    unv = [r["mpn"] for r in rows if not r["verified"] and r["provenance"] != "generic"]
    fsrc = c.calcs.get("footprint_sources", {})
    gen_only = sorted(n for n, src in fsrc.items() if src.startswith("generator"))
    official = sorted(n for n, src in fsrc.items() if src.startswith(("kicad:", "datasheet:")))
    L += ["", "## Manufacturing-readiness checklist", "",
          "The software verifies geometry (DRC), connectivity and format validity. It cannot replace these human checks:", ""]
    L += [f"- [{'x' if drc.clean else ' '}] DRC clean and every net connected (automated)",
          f"- [{' ' if unv else 'x'}] Pinouts cross-checked against datasheets" + (f" - **unverified: {', '.join(unv)}**" if unv else ""),
          f"- [{' ' if gen_only else 'x'}] Footprints: {len(official)} use official geometry (KiCad library / datasheet drawing), "
          + (f"**{len(gen_only)} are generator-only and still need a check against the manufacturer drawing: {', '.join(gen_only)}**" if gen_only else "none are generator-only"),
          "- [ ] Schematic reviewed by an engineer (block calculations are listed above; stability/EMC are not simulated)",
          "- [ ] High-current nets reviewed (ampacity warnings above); thermal estimate is a 2-D model, not a substitute for testing",
          "- [ ] Gerbers opened in an independent viewer (KiCad/Gerbv) and the fab's online DFM check passed",
          "- [ ] Prices/stock refreshed from suppliers before ordering (`forge purchase`)"]
    for k, v in (extra or {}).items():
        L += ["", f"## {k}", "", str(v)]
    return "\n".join(L) + "\n"


def kicad_drc_section(rep) -> str:
    """Markdown body for the 'KiCad DRC (authoritative)' section (rep = kicad_backend.DrcReport)."""
    L = [f"KiCad {rep.version or '?'} DRC (zones refilled by KiCad's own filler): **{'CLEAN' if rep.clean else 'VIOLATIONS'}** - {rep.summary()}.",
         "Full machine-readable result: `kicad_drc.json`."]
    if rep.by_type or rep.unconnected:
        L += ["", "| Type | Severity | Count |", "|--|--|--|"]
        counts: dict[tuple[str, str], int] = {}
        for v in rep.violations + rep.unconnected:
            counts[(v["type"], v["severity"])] = counts.get((v["type"], v["severity"]), 0) + 1
        for (t, sev), n in sorted(counts.items(), key=lambda kv: (kv[0][1] != "error", -kv[1])):
            L.append(f"| {t} | {sev} | {n} |")
        for v in (rep.errors + rep.unconnected)[:15]:
            it = v["items"][0] if v["items"] else {}
            L.append(f"\n- {v['type']}: {v['description']} @ ({it.get('x', '?')}, {it.get('y', '?')}) {it.get('desc', '')}")
    return "\n".join(L)
