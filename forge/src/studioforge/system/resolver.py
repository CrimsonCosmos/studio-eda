"""Interface resolver: derives every block's concrete parameters from the links between subassemblies.

  * power:   demand flows upstream (consumer current x fleet qty x duty), voltage windows flow downstream
  * signals: peripheral needs of neighbours become MCU `requests`, solved by the pin-mux solver
  * physical: any link that leaves a board gets a connector chosen from current/voltage, plus a harness line
  * checks:  voltage windows, capacity, protocol/kind compatibility, I2C address clashes, logic levels
It iterates to a fixpoint because changing a converter's output current changes its input current.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from ..blocks.registry import REGISTRY, _load, build_block
from ..model import Circuit, Port
from ..parts.db import PartsLibrary
from .plan import BlockInst, BoardNode, Link, PurchasedItem, SystemPlan

# kind -> {signal on side A : signal on side B}; None => identical names or positional
CROSS = {"uart": {"tx": "rx", "rx": "tx"}}
MCU_KIND = {"pwm": "pwm", "analog": "adc", "gpio": "gpio", "can_logic": "can", "i2c": "i2c", "spi": "spi", "uart": "uart"}
PROVISIONAL = {"iout": 0.5, "ibat_max": 5.0, "phase_current_a": 5.0}
AWG = [(0.5, 26), (1.5, 24), (3, 22), (5, 20), (7, 18), (10, 16), (15, 14), (25, 12), (35, 10), (55, 8), (75, 6), (110, 4), (150, 2), (200, 1)]
WIRE_USD_PER_M = {26: 0.15, 24: 0.2, 22: 0.25, 20: 0.35, 18: 0.5, 16: 0.7, 14: 1.0, 12: 1.5, 10: 2.2, 8: 3.5, 6: 5.5, 4: 8.0, 2: 12.0, 1: 15.0}


def awg_for(amps: float) -> int:
    for a, g in AWG:
        if amps <= a:
            return g
    return 0


@dataclass
class Issue:
    severity: str
    msg: str


@dataclass
class Resolved:
    plan: SystemPlan
    params: dict[tuple[str, str], dict]
    circuits: dict[tuple[str, str], Circuit]
    links: list[Link]
    items: list[PurchasedItem]
    issues: list[Issue] = field(default_factory=list)
    harness: list[dict] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    extra_blocks: dict[str, list[BlockInst]] = field(default_factory=dict)
    demand: dict[str, float] = field(default_factory=dict)
    iterations: int = 0

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


class Resolver:
    def __init__(self, lib: PartsLibrary, plan: SystemPlan, log=print):
        _load()
        self.lib, self.plan, self.log = lib, copy.deepcopy(plan), log
        self.boards = {b.id: b for b in self.plan.boards}
        self.items = {p.id: p for p in self.plan.purchased}
        self.params: dict[tuple[str, str], dict] = {}
        self.binst: dict[tuple[str, str], BlockInst] = {}
        for b in self.plan.boards:
            for bi in b.blocks:
                self.params[(b.id, bi.id)] = dict(bi.params)
                self.binst[(b.id, bi.id)] = bi
        self.circuits: dict[tuple[str, str], Circuit] = {}
        self.links = list(self.plan.links)
        self.issues: list[Issue] = []
        self.decisions: list[str] = []
        self.demand: dict[str, float] = {}
        self._sig: dict[tuple[str, str], str] = {}

    # ---- endpoints -------------------------------------------------------------------------------
    def split(self, end: str):
        p = end.split(".")
        if len(p) == 3:
            return ("block", (p[0], p[1]), p[2])
        return ("item", p[0], p[1])

    def port_of(self, end: str) -> Port | None:
        kind, key, name = self.split(end)
        if kind == "block":
            c = self.circuits.get(key)
            return c.ports.get(name) if c else None
        it = self.items.get(key)
        if it:
            for pt in it.ports:
                if pt["name"] == name:
                    meta = {k: v for k, v in pt.items() if k not in ("name", "kind", "direction", "voltage", "current_a", "signals")}
                    return Port(name, "", pt["kind"], pt.get("direction", "bidir"), pt.get("voltage", 0.0), pt.get("current_a", 0.0), meta,
                                dict(pt.get("signals") or {}))
        return None

    def node_of(self, end: str) -> tuple[str, int]:
        """(node id, fleet qty)"""
        kind, key, _ = self.split(end)
        if kind == "block":
            return key[0], self.boards[key[0]].qty
        return key, self.items[key].qty

    def duty_of(self, end: str) -> float:
        kind, key, _ = self.split(end)
        return self.boards[key[0]].power_duty if kind == "block" else 1.0

    # ---- building ----------------------------------------------------------------------------------
    def _build(self, key) -> None:
        bi = self.binst[key]
        sig = repr(sorted(self.params[key].items(), key=lambda kv: kv[0]))
        if self._sig.get(key) == sig and key in self.circuits:
            return
        prov = dict(self.params[key])
        for name, val in PROVISIONAL.items():                       # first-pass guesses for values the links will determine
            if name in {p.name for p in REGISTRY[bi.block].params} and prov.get(name) in (None, "auto") and name in REGISTRY[bi.block].auto_params:
                prov[name] = val
        try:
            self.circuits[key] = build_block(self.lib, bi.block, prov, f"{key[0]}.{key[1]}")
            self._sig[key] = sig
        except Exception as e:
            self.issues.append(Issue("error", f"{key[0]}.{key[1]}: cannot build block '{bi.block}': {e}"))
            raise

    # ---- iteration ----------------------------------------------------------------------------------
    def resolve(self) -> Resolved:
        for it in range(1, 11):
            changed = False
            for key in list(self.binst):
                self._build(key)
            changed |= self._mcu_requests()
            changed |= self._power_sizing()
            changed |= self._windows()
            changed |= self._motor_params()
            if not changed:
                break
        for key in list(self.binst):
            self._build(key)
        res = Resolved(self.plan, self.params, self.circuits, self.links, list(self.items.values()), self.issues, [], self.decisions,
                       {}, self.demand, it)
        self._validate(res)
        self._connectors(res)
        seen = set()
        res.issues = [i for i in res.issues if not ((i.severity, i.msg) in seen or seen.add((i.severity, i.msg)))]
        return res

    # -- peripherals -> MCU requests
    def _mcu_requests(self) -> bool:
        changed = False
        reqs: dict[tuple[str, str], list[dict]] = {}
        for l in self.links:
            for me, other in ((l.a, l.b), (l.b, l.a)):
                kind, key, pname = self.split(me)
                if kind != "block" or self.binst[key].block != "mcu_stm32f103":
                    continue
                peer = self.port_of(other)
                if peer is None or peer.kind not in MCU_KIND:
                    continue
                keys = list(peer.signals) or ["io"]
                if peer.kind == "gpio":
                    keys = [f"{pname}"] if len(keys) == 1 else keys
                req = {"name": pname, "kind": MCU_KIND[peer.kind]}
                if peer.kind in ("pwm", "analog", "gpio"):
                    req["signals"] = keys
                reqs.setdefault(key, [])
                if req not in reqs[key]:
                    reqs[key].append(req)
        for key, lst in reqs.items():
            if self.params[key].get("requests") != lst:
                self.params[key]["requests"] = lst
                changed = True
        return changed

    # -- demand upstream
    def _power_sizing(self) -> bool:
        changed = False
        demand: dict[str, float] = {}
        for l in self.links:
            pa, pb = self.port_of(l.a), self.port_of(l.b)
            if pa is None or pb is None or pa.kind != "power" or pb.kind != "power":
                continue
            if pa.direction == "out" and pb.direction == "in":
                prov, cons = l.a, l.b
            elif pb.direction == "out" and pa.direction == "in":
                prov, cons = l.b, l.a
            else:
                continue
            node_p, _ = self.node_of(prov)
            node_c, qty_c = self.node_of(cons)
            cport = self.port_of(cons)
            mult = qty_c * self.duty_of(cons) if node_p != node_c else 1.0
            demand[prov] = demand.get(prov, 0.0) + cport.current_a * mult
        self.demand = demand
        for prov, amps in demand.items():
            kind, key, pname = self.split(prov)
            if kind != "block":
                continue
            bi = self.binst[key]
            target = {"buck_converter": "iout", "ldo": "iout", "battery_input": "ibat_max"}.get(bi.block)
            if not target or pname not in ("vout", "vbus"):
                continue
            val = round(max(amps * 1.1, 0.05), 3)
            if bi.params.get(target) not in (None, "auto") and key not in self._auto_keys(target):
                continue                               # explicit value from the plan wins
            self._mark_auto(key, target)
            if self.params[key].get(target) != val:
                self.params[key][target] = val
                changed = True
        return changed

    def _auto_keys(self, target: str) -> set:
        return {k for (k, t) in getattr(self, "_autos", set()) if t == target}

    def _mark_auto(self, key, target) -> None:
        if not hasattr(self, "_autos"):
            self._autos = set()
        self._autos.add((key, target))

    def _is_auto(self, key, name) -> bool:
        return self.binst[key].params.get(name) in (None, "auto") or (key, name) in getattr(self, "_autos", set())

    # -- voltage windows downstream
    def _windows(self) -> bool:
        changed = False
        for l in self.links:
            pa, pb = self.port_of(l.a), self.port_of(l.b)
            if pa is None or pb is None or pa.kind != "power" or pb.kind != "power":
                continue
            if pa.direction == "out" and pb.direction == "in":
                prov, cons, pp = l.a, l.b, pa
            elif pb.direction == "out" and pa.direction == "in":
                prov, cons, pp = l.b, l.a, pb
            else:
                continue
            kind, key, pname = self.split(cons)
            if kind != "block":
                continue
            bi = self.binst[key]
            tol = pp.meta.get("tolerance", 0.05)
            vmin = pp.meta.get("vmin", pp.voltage * (1 - tol))
            vmax = pp.meta.get("vmax", pp.voltage * (1 + tol))
            mapping = {("buck_converter", "vin"): {"vin_min": vmin, "vin_max": vmax},
                       ("ldo", "vin"): {"vin_min": vmin, "vin_max": vmax},
                       ("motor_driver_3ph", "vbus"): {"vbus_max": vmax, "vbus_nom": pp.voltage},
                       ("battery_input", "src"): {"vbat_min": vmin, "vbat_max": vmax, "vbat_nom": pp.voltage}}.get((bi.block, pname))
            if not mapping:
                continue
            for name, val in mapping.items():
                if self._is_auto(key, name):
                    self._mark_auto(key, name)
                    val = round(val, 3)
                    if self.params[key].get(name) != val:
                        self.params[key][name] = val
                        changed = True
        return changed

    # -- motor current from the motor's rated phase current
    def _motor_params(self) -> bool:
        changed = False
        for l in self.links:
            for me, other in ((l.a, l.b), (l.b, l.a)):
                kind, key, pname = self.split(me)
                if kind == "block" and self.binst[key].block == "motor_driver_3ph" and pname == "phase":
                    pt = self.port_of(other)
                    if pt is not None and pt.current_a and self._is_auto(key, "phase_current_a"):
                        self._mark_auto(key, "phase_current_a")
                        if self.params[key].get("phase_current_a") != pt.current_a:
                            self.params[key]["phase_current_a"] = pt.current_a
                            changed = True
        return changed

    # ---- validation ----------------------------------------------------------------------------------
    def _validate(self, res: Resolved) -> None:
        I = self.issues
        i2c_addrs: dict[str, list[int]] = {}
        for l in self.links:
            pa, pb = self.port_of(l.a), self.port_of(l.b)
            if pa is None or pb is None:
                I.append(Issue("error", f"link {l.a} <-> {l.b}: unknown port"))
                continue
            if pa.kind != pb.kind and "bundle" not in (pa.kind, pb.kind):
                I.append(Issue("error", f"link {l.a} <-> {l.b}: incompatible kinds {pa.kind}/{pb.kind}"))
                continue
            if pa.kind == "power":
                if pa.direction == pb.direction and pa.direction != "bidir":
                    I.append(Issue("error", f"link {l.a} <-> {l.b}: both ends are power {pa.direction}"))
                    continue
                prov, cons, cend, pend = (pa, pb, l.b, l.a) if pa.direction == "out" else (pb, pa, l.a, l.b)
                vmin, vmax = cons.meta.get("vmin", 0), cons.meta.get("vmax", 1e9)
                if not (vmin - 1e-6 <= prov.voltage <= vmax + 1e-6) and not (prov.meta.get("vmin") and prov.meta["vmin"] >= vmin - 1e-6 and prov.meta.get("vmax", 0) <= vmax + 1e-6):
                    I.append(Issue("error", f"link {pend} -> {cend}: provider {prov.voltage:g} V outside consumer window {vmin:g}-{vmax:g} V"))
                pk, pkey, _ = self.split(pend)
                if pk == "item":
                    need = self.demand.get(pend, 0.0)
                    cap = prov.current_a
                    if cap and need > cap * 1.0001:
                        I.append(Issue("error", f"{pend}: load {need:.1f} A exceeds rated {cap:g} A"))
                    elif cap and need > 0.8 * cap:
                        I.append(Issue("warning", f"{pend}: load {need:.1f} A is {100 * need / cap:.0f}% of rated {cap:g} A"))
            elif pa.kind in ("pwm", "analog", "gpio", "can_logic", "spi", "i2c"):
                if pa.voltage and pb.voltage and abs(pa.voltage - pb.voltage) > 0.55:
                    I.append(Issue("error", f"link {l.a} <-> {l.b}: logic level mismatch {pa.voltage:g} V vs {pb.voltage:g} V (level shifter needed)"))
                if pa.kind == "i2c":
                    host, dev = (l.a, pb) if pa.meta.get("role") == "host" else (l.b, pa)
                    addr = dev.meta.get("addr")
                    if addr is not None:
                        i2c_addrs.setdefault(host, []).append(addr)
        for host, addrs in i2c_addrs.items():
            if len(addrs) != len(set(addrs)):
                I.append(Issue("error", f"I2C address conflict on {host}: {sorted(addrs)}"))
        for key, c in self.circuits.items():
            for w in c.warnings:
                I.append(Issue("warning", f"{key[0]}.{key[1]}: {w}"))

    # ---- connectors for links that leave a board -------------------------------------------------------------------
    def _connectors(self, res: Resolved) -> None:
        extra: dict[str, list[BlockInst]] = {}
        new_links: list[Link] = []
        n = 0
        for l in self.links:
            pa, pb = self.port_of(l.a), self.port_of(l.b)
            if pa is None or pb is None:
                new_links.append(l)
                continue
            na, _ = self.node_of(l.a)
            nb, _ = self.node_of(l.b)
            crossing = na != nb
            ends = [l.a, l.b]
            rewired = [l.a, l.b]
            if crossing:
                if pa.kind == "power":
                    amps = (max(self.demand.get(l.a, 0.0), self.demand.get(l.b, 0.0)) or max(pa.current_a, pb.current_a)) * 1.25   # size wire for demand + margin
                elif pa.kind == "motor_phase":
                    amps = max(pa.current_a, pb.current_a) * 1.25
                else:
                    amps = max(pa.current_a, pb.current_a, 0.1)
                volts = max(pa.voltage, pb.voltage, pa.meta.get("vmax", 0), pb.meta.get("vmax", 0))
                for idx, (end, port) in enumerate(((l.a, pa), (l.b, pb))):
                    kind, key, pname = self.split(end)
                    if kind != "block" or port.meta.get("external"):
                        continue
                    n += 1
                    sigs = list(port.signals) or ["sig"]
                    pins = [{"name": s} for s in sigs]
                    if port.kind == "power":
                        pins = [{"name": "vcc", "current_a": amps}, {"name": "gnd"}]
                    elif port.kind not in ("motor_phase",):
                        pins = pins + [{"name": "gnd"}]
                    bid = f"conn{n}"
                    bi = BlockInst(bid, "connector_breakout", {"pins": pins, "max_current_a": amps, "voltage": volts, "kind": port.kind, "label": "link"})
                    extra.setdefault(key[0], []).append(bi)
                    self.binst[(key[0], bid)] = bi
                    self.params[(key[0], bid)] = dict(bi.params)
                    self._build((key[0], bid))
                    # inside the board: original port <-> connector port (identity)
                    cport_end = f"{key[0]}.{bid}.link"
                    new_links.append(Link(end, cport_end, "auto-connector"))
                    rewired[idx] = cport_end
                wires = max(1, len(port.signals) if port.kind != "power" else 2)
                length = 0.6
                g = awg_for(amps)
                res.harness.append({"from": l.a, "to": l.b, "kind": pa.kind, "wires": wires, "awg": g, "amps": round(amps, 2), "volts": round(volts, 1),
                                    "length_m": length, "qty": max(self.node_of(l.a)[1], self.node_of(l.b)[1]),
                                    "cost_usd_each": round(wires * length * WIRE_USD_PER_M.get(g, 1.0) + 2 * (0.3 if amps <= 3 else 1.5 if amps <= 30 else 6.0), 2)})
            new_links.append(Link(rewired[0], rewired[1], l.note) if rewired != ends else l)
        # keep original crossing links as logical (harness) links; internal ones feed net unification
        res.links = new_links
        res.extra_blocks = extra
