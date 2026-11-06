"""Electrical rule check on a Circuit (pin types, floating rails, dangling pins)."""
from __future__ import annotations

from dataclasses import dataclass

from .model import Circuit


@dataclass
class ErcIssue:
    severity: str          # error | warning
    msg: str


def run_erc(c: Circuit, allow_open: set[str] | None = None) -> list[ErcIssue]:
    issues: list[ErcIssue] = []
    pn = c.pin_net_map()
    port_nets = set()
    for p in c.ports.values():
        port_nets.add(p.net)
        port_nets.update(p.signals.values())
    # unconnected pins
    for ref, comp in c.components.items():
        for pin in comp.part.pins:
            key = f"{ref}.{pin.number}"
            if key in pn or pin.etype == "nc":
                continue
            if allow_open and (key in allow_open or f"{ref}.{pin.name}" in allow_open):
                continue
            if comp.part.category in ("mounting", "testpoint") or comp.dnp:
                continue
            if comp.part.category.startswith("ic.mcu") and pin.etype in ("bidir", "input"):
                continue                                  # unused GPIO is normal
            sev = "error" if pin.etype in ("power_in", "power_out") else "warning"
            if comp.part.category.startswith(("connector", "module")) and pin.etype == "passive":
                sev = "warning"
            issues.append(ErcIssue(sev, f"{key} ({comp.part.mpn} pin {pin.name}, {pin.etype}) is not connected"))
    for net, pins in c.nets.items():
        types = []
        for p in pins:
            ref, _, num = p.partition(".")
            types.append((p, c.components[ref].part.pin(num).etype))
        outs = [p for p, t in types if t == "power_out"]
        drv = [p for p, t in types if t == "output"]
        if len(outs) > 1:
            issues.append(ErcIssue("error", f"net {net}: multiple power outputs {outs}"))
        if len(drv) > 1:
            issues.append(ErcIssue("error", f"net {net}: multiple driven outputs {drv}"))
        if len(pins) == 1 and net not in port_nets:
            issues.append(ErcIssue("warning", f"net {net} has a single pin {pins[0]}"))
        pins_in = [p for p, t in types if t == "power_in"]
        if pins_in and not outs and net not in port_nets and net not in ("GND", "AGND", "PGND"):
            # a powered net must come from a regulator output, a port, a connector/module pin, or a passive path
            has_passive_source = any(c.components[p.partition(".")[0]].part.category.startswith(("connector", "module", "inductor", "ferrite", "fuse", "resistor", "diode"))
                                     for p, _ in types)
            if not has_passive_source:
                issues.append(ErcIssue("warning", f"net {net}: power-input pins {pins_in[:3]} have no source on this board"))
    return issues


def erc_errors(c: Circuit) -> list[ErcIssue]:
    return [i for i in run_erc(c) if i.severity == "error"]
