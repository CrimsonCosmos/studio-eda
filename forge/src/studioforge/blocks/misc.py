"""Interconnect + escape-hatch blocks: connectors for cross-board links, custom LLM-authored netlists, test points."""
from __future__ import annotations

from ..constraints import Edge
from ..erc import run_erc
from ..model import Circuit
from ..parts.db import PartsLibrary
from .registry import Param, block


def pick_connector(lib: PartsLibrary, n_pins: int, current_a: float, voltage: float):
    """Connector family by per-pin current and voltage - deterministic interface decision."""
    if n_pins == 2 and current_a > 12:
        return lib.get("XT60PW-M"), "XT60 (30 A)"
    if current_a > 3 and voltage <= 300:
        return lib.terminal(n_pins), "5.08 mm screw terminal (15 A)"
    return lib.jst_xh(n_pins), "JST XH (3 A/pin)"


@block("connector_breakout",
       "Board-to-board / board-to-cable connector: family chosen from per-pin current and voltage; each named pin is bound to a net/port.",
       [Param("pins", "list", None, "list of {name, net?, current_a?}; names become nets", True), Param("max_current_a", "number", 1.0, "highest pin current (A)"),
        Param("voltage", "number", 12.0, "max voltage on the connector (V)"), Param("kind", "string", "bundle", "port kind exposed (power|can_bus|uart|motor_phase|...)"),
        Param("label", "string", "link", "port name")], "interconnect")
def connector_breakout(lib: PartsLibrary, pins=None, max_current_a=1.0, voltage=12.0, kind="bundle", label="link") -> Circuit:
    c = Circuit(f"conn_{label}")
    part, fam = pick_connector(lib, len(pins), max_current_a, voltage)
    j = c.add(part, prefix="J", block="connector")
    sig = {}
    for i, p in enumerate(pins, 1):
        net = p.get("net") or p["name"].upper()
        c.connect(net, f"{j.ref}.{i}")
        sig[p["name"]] = net
        if p.get("current_a"):
            c.set_net(net, role="power", current_a=p["current_a"], voltage=voltage)
        if p["name"].lower() in ("gnd", "ground"):
            c.set_net(net, role="ground")
    c.constrain(Edge(j.ref, "any", 4.0))
    c.add_port(label, next(iter(sig.values())), kind, "bidir", voltage=voltage, current_a=max_current_a, signals=sig, connector=fam)
    c.calcs["connector"] = f"{label}: {part.mpn} ({fam}), {len(pins)} pins, {max_current_a:g} A/pin, {voltage:g} V"
    return c


@block("custom_netlist",
       "Escape hatch for anything not covered by a block: a netlist authored by Claude from datasheet typical-application circuits. "
       "Every part is resolved through the parts library (research on miss) and the netlist is ERC-checked before use.",
       [Param("parts", "list", None, "[{ref, part: MPN or description, value?}]", True), Param("nets", "dict", None, "{net: [\"U1.3\", \"R1.1\"]}", True),
        Param("ports", "list", [], "[{name, net, kind, direction, voltage?, current_a?, signals?}]"), Param("name", "string", "custom", "circuit name")], "custom")
def custom_netlist(lib: PartsLibrary, parts=None, nets=None, ports=None, name="custom") -> Circuit:
    c = Circuit(name)
    for p in parts:
        part = lib.resolve(p["part"])
        c.add(part, ref=p["ref"], value=p.get("value") or part.value, block="custom", power_w=float(p.get("power_w", 0)))
    for net, pins in nets.items():
        c.connect(net, *pins)
        if net.upper() in ("GND", "AGND", "PGND"):
            c.set_net(net, role="ground")
    for pt in ports or []:
        c.add_port(pt["name"], pt.get("net", ""), pt["kind"], pt.get("direction", "bidir"), pt.get("voltage", 0.0), pt.get("current_a", 0.0),
                   signals=pt.get("signals"), **pt.get("meta", {}))
    bad = [i for i in run_erc(c) if i.severity == "error"]
    if bad:
        raise ValueError("custom netlist failed ERC: " + "; ".join(i.msg for i in bad[:8]))
    return c


@block("mounting_holes", "Four M3 mounting holes at the board corners.", [Param("diameter", "number", 3.2, "hole diameter (mm)")], "mechanical")
def mounting_holes(lib: PartsLibrary, diameter=3.2) -> Circuit:
    c = Circuit("mounting")
    for corner in ("tl", "tr", "bl", "br"):
        h = c.add(lib.mounting_hole(diameter), prefix="H", block="mech", corner=corner)
    return c
