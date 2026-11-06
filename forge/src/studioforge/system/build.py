"""Orchestrate a resolved system plan into boards, fab packages, harness list, cost roll-up and purchase list."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..blocks.registry import build_block
from ..erc import run_erc
from ..export import bom as bomx
from ..export.package import write_package
from ..model import Circuit
from ..parts.db import PartsLibrary
from ..pcb.flow import BoardSpec, build_board
from . import cost as costx
from .plan import BlockInst, BoardNode, Link, PurchasedItem, SystemPlan
from .resolver import CROSS, Issue, Resolved, Resolver


def rail_name(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "+" + s.replace(".", "V") + ("V" if "." not in s else "")


def assemble_board(res: Resolved, board: BoardNode) -> Circuit:
    lib_circuit = Circuit(board.id)
    c = lib_circuit
    keys = [(board.id, bi.id) for bi in board.blocks if (board.id, bi.id) in res.circuits]
    keys += [(board.id, bi.id) for bi in res.extra_blocks.get(board.id, [])]
    for key in keys:
        c.merge(res.circuits[key], prefix=f"{key[1]}.", block=key[1])
    for l in res.links:
        ka, kb = l.a.split("."), l.b.split(".")
        if len(ka) != 3 or len(kb) != 3 or ka[0] != board.id or kb[0] != board.id:
            continue
        pa, pb = c.ports.get(f"{ka[1]}.{ka[2]}"), c.ports.get(f"{kb[1]}.{kb[2]}")
        if pa is None or pb is None:
            continue
        pairs: list[tuple[str, str]] = []
        if pa.signals and pb.signals:
            sa, sb = list(pa.signals), list(pb.signals)
            cross = CROSS.get(pa.kind, {})
            if set(sa) == set(sb) or all(cross.get(s, s) in pb.signals for s in sa):
                pairs = [(pa.signals[s], pb.signals[cross.get(s, s)]) for s in sa]
            elif len(sa) == len(sb):
                pairs = [(pa.signals[x], pb.signals[y]) for x, y in zip(sorted(sa), sorted(sb))]
            else:
                # connector bundles may carry extra pins (gnd); match what exists
                common = [s for s in sa if s in pb.signals]
                pairs = [(pa.signals[s], pb.signals[s]) for s in common]
        else:
            pairs = [(pa.net, pb.net)]
        # provider/host net name wins so rails keep a readable name
        if pa.kind == "power" and pa.direction == "in":
            pairs = [(y, x) for x, y in pairs]
        for keep, drop in pairs:
            if keep != drop and keep in c.nets and drop in c.nets:
                c.rename_net(drop, keep)
    # readable rail names
    for name, port in list(c.ports.items()):
        if port.kind == "power" and port.direction == "out" and port.net in c.nets and port.voltage:
            new = rail_name(port.voltage)
            if new not in c.nets and not port.net.startswith("+") and port.net != new:
                c.rename_net(port.net, new)
    # a power plane needs the power attribute even after joins
    for n, a in c.net_attrs.items():
        if n.startswith("+") and a.role == "signal":
            a.role = "power"
    return c


@dataclass
class SystemBuild:
    out: Path
    resolved: Resolved
    boards: dict = field(default_factory=dict)          # id -> {circuit, result, files, cost}
    cost: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)


def _mounting(lib: PartsLibrary) -> Circuit:
    return build_block(lib, "mounting_holes", {})


def decide_buy_vs_build(lib: PartsLibrary, plan: SystemPlan, log=print) -> tuple[SystemPlan, list[str]]:
    """Replace blocks that have a purchasable alternative when buying is cheaper/equivalent (or forced by `prefer`/budget).
    Policy: do not build what can be bought cheaply; build when the BOM is much cheaper or the user insists."""
    decisions: list[str] = []
    plan = copy.deepcopy(plan)
    r = Resolver(lib, plan, log=None)
    res = r.resolve()
    swaps: list[tuple[str, BlockInst]] = []
    for b in plan.boards:
        for bi in b.blocks:
            if not bi.buy_alternative:
                continue
            circ = res.circuits[(b.id, bi.id)]
            est = costx.est_build_cost(circ, b.layers or 4, b.qty)
            alt = bi.buy_alternative
            if bi.prefer == "build":
                decisions.append(f"{b.id}.{bi.id}: BUILD (user preference); est. ${est['total']:.2f}/board vs buy ${alt.unit_cost_usd:.2f}")
            elif bi.prefer == "buy" or alt.unit_cost_usd <= est["total"] * 1.15 + 3.0:
                swaps.append((b.id, bi))
                why = "user preference" if bi.prefer == "buy" else "purchased module is no more expensive than building (and saves design/verification risk)"
                decisions.append(f"{b.id}.{bi.id}: BUY '{alt.title}' ${alt.unit_cost_usd:.2f} vs build ${est['total']:.2f} - {why}")
            else:
                decisions.append(f"{b.id}.{bi.id}: BUILD ${est['total']:.2f} vs buy ${alt.unit_cost_usd:.2f} (building is cheaper by {100 * (1 - est['total'] / alt.unit_cost_usd):.0f}%)")
    # budget pressure: if over budget, also switch remaining cheaper-to-build... (handled in rollup, reported there)
    for bid, bi in swaps:
        board = next(b for b in plan.boards if b.id == bid)
        circ = res.circuits[(bid, bi.id)]
        ports = []
        for pn, p in circ.ports.items():
            if p.kind == "ground":
                continue
            d = {"name": pn, "kind": p.kind, "direction": p.direction, "voltage": p.voltage, "current_a": p.current_a, "signals": dict(p.signals)}
            d.update({k: v for k, v in p.meta.items() if k in ("vmin", "vmax", "tolerance", "addr", "role")})
            ports.append(d)
        item = PurchasedItem(f"{bid}_{bi.id}", bi.buy_alternative.title, "module", board.qty, bi.buy_alternative.unit_cost_usd,
                             bi.buy_alternative.spec, bi.buy_alternative.url, bi.buy_alternative.supplier, ports, True,
                             f"replaces block {bid}.{bi.id} ({bi.block})")
        plan.purchased.append(item)
        board.blocks = [x for x in board.blocks if x.id != bi.id]
        for l in plan.links:
            for attr in ("a", "b"):
                v = getattr(l, attr)
                pre = f"{bid}.{bi.id}."
                if v.startswith(pre):
                    setattr(l, attr, f"{item.id}.{v[len(pre):]}")
    return plan, decisions


def build_system(plan: SystemPlan, lib: PartsLibrary, outdir: str | Path, log=print, build_pcbs: bool = True,
                 pcb_time_s: float = 900.0, pitch: float | None = None) -> SystemBuild:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    log = log or (lambda *_a, **_k: None)
    t0 = time.time()
    plan2, decisions = decide_buy_vs_build(lib, plan, log)
    res = Resolver(lib, plan2, log=log).resolve()
    res.decisions = decisions
    sb = SystemBuild(out, res)
    for d in decisions:
        log("[buy/build] " + d)
    for i in res.issues:
        log(f"[{i.severity}] {i.msg}")
    (out / "plan_resolved.json").write_text(json.dumps({"plan": plan2.to_dict(), "params": {f"{k[0]}.{k[1]}": v for k, v in res.params.items()},
                                                          "decisions": decisions}, indent=1, default=str))
    total_boards_usd = 0.0
    for b in res.plan.boards:
        circ = assemble_board(res, b)
        circ.merge(_mounting(lib), prefix="mh.", block="mech")
        erc = run_erc(circ)
        errs = [e for e in erc if e.severity == "error"]
        for e in errs:
            sb.issues.append(Issue("error", f"{b.id}: ERC {e.msg}"))
            log(f"[error] {b.id}: ERC {e.msg}")
        info = {"circuit": circ, "erc": erc}
        sb.boards[b.id] = info
        if build_pcbs and not errs:
            spec = BoardSpec(layers=b.layers, copper_oz=b.copper_oz, width=b.max_width_mm, height=b.max_height_mm, pitch=pitch,
                             time_budget_s=pcb_time_s)
            bres = build_board(circ, spec, log=log)
            info["result"] = bres
            info["files"] = write_package(bres, out / "boards" / b.id, b.id,
                                          extra={"System context": f"Board `{b.id}` x{b.qty} in system `{plan2.name}`. {b.notes}"})
            bb = bres.board
            info["cost"] = {"bom": bomx.bom_cost(circ)["total_usd"], "pcb": round(costx.pcb_fab_cost(bb.width * bb.height, bb.rules.layers, b.qty), 2),
                            "assembly": round(costx.assembly_cost(circ, b.qty), 2)}
        else:
            e = costx.est_build_cost(circ, b.layers or 4, b.qty)
            info["cost"] = {"bom": e["bom"], "pcb": e["pcb"], "assembly": e["assembly"]}
        info["cost"]["per_board"] = round(sum(info["cost"].values()), 2)
        info["cost"]["qty"] = b.qty
        info["cost"]["total"] = round(info["cost"]["per_board"] * b.qty, 2)
        total_boards_usd += info["cost"]["total"]
    items_usd = sum(p.unit_cost_usd * p.qty for p in res.plan.purchased)
    harness_usd = sum(h["cost_usd_each"] * h["qty"] for h in res.harness)
    subtotal = total_boards_usd + items_usd + harness_usd
    cont = 0.10 * subtotal
    sb.cost = {"boards_usd": round(total_boards_usd, 2), "purchased_usd": round(items_usd, 2), "harness_usd": round(harness_usd, 2),
               "contingency_usd": round(cont, 2), "total_usd": round(subtotal + cont, 2), "budget_usd": plan2.budget_usd,
               "within_budget": (plan2.budget_usd is None) or (subtotal + cont <= plan2.budget_usd), "elapsed_s": round(time.time() - t0, 1)}
    from .report import write_system_report
    write_system_report(sb, plan2)
    return sb
