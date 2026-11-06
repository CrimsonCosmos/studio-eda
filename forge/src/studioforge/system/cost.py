"""Cost estimation (all numbers are *estimates* and labelled as such) and buy-vs-build comparison."""
from __future__ import annotations

import copy
import math

from ..export import bom as bomx
from ..model import Circuit


def pcb_fab_cost(area_mm2: float, layers: int, qty: int) -> float:
    """Per-board prototype/small-batch PCB price estimate (USD). Rough curve fitted to typical Chinese-fab list prices."""
    cm2 = area_mm2 / 100.0
    per_cm2 = {2: 0.012, 4: 0.040, 6: 0.075}.get(layers, 0.1)
    setup = {2: 4.0, 4: 18.0, 6: 40.0}.get(layers, 60.0)
    qty_factor = 1.0 if qty < 10 else 0.8 if qty < 50 else 0.6 if qty < 200 else 0.45
    return per_cm2 * cm2 * qty_factor + setup / max(qty, 1)


def assembly_cost(circuit: Circuit, qty: int, self_assembled: bool = False) -> float:
    """Per-board SMT assembly estimate (USD) when using an assembly service; 0 if hand-assembled."""
    if self_assembled:
        return 0.0
    smd = sum(len(c.footprint.pads) for c in circuit.components.values() if not any(p.thru for p in c.footprint.pads) and c.part.category not in ("mounting", "testpoint"))
    th = sum(1 for c in circuit.components.values() if any(p.thru for p in c.footprint.pads) and c.part.category not in ("mounting", "testpoint"))
    unique = len({c.part.mpn for c in circuit.components.values()})
    setup = 25.0 / max(qty, 1)
    return setup + 0.0165 * smd + 0.30 * th + 0.0 * unique


def est_board_area(circuit: Circuit, utilization: float = 0.38) -> float:
    a = 0.0
    for c in circuit.components.values():
        x0, y0, x1, y1 = c.footprint.courtyard
        a += (x1 - x0 + 0.4) * (y1 - y0 + 0.4)
    return a / utilization


def est_build_cost(circuit: Circuit, layers: int = 4, qty: int = 1) -> dict:
    bom = bomx.bom_cost(circuit)
    area = est_board_area(circuit)
    pcb = pcb_fab_cost(area, layers, qty)
    asm = assembly_cost(circuit, qty)
    return {"bom": bom["total_usd"], "pcb": round(pcb, 2), "assembly": round(asm, 2), "total": round(bom["total_usd"] + pcb + asm, 2),
            "area_mm2": round(area), "bom_unpriced_lines": bom["lines_unpriced"]}
