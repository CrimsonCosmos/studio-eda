"""Real KiCad 7 schematic (.kicad_sch) from a studioforge Circuit.

* Symbols come from KiCad's own libraries (Device R/C/L/LED/D..., Connector_Generic, ...) whenever one fits the part's
  pins; otherwise a box symbol is generated from the part's pin list. All used symbols are embedded in lib_symbols.
* Components are laid out on a grid, grouped by block. Connectivity is expressed with global net labels placed on the
  pin ends (no wires), so `kicad-cli sch export netlist` reproduces the Circuit's nets exactly.
* Reference / Value / Footprint ('Library:Name' so KiCad can assign footprints) / MPN properties are filled in.
"""
from __future__ import annotations

import math
import re
import uuid
from pathlib import Path

from .. import kicad_symbols as ks
from .. import sexpr
from ..model import Circuit, Component, PartDef
from ..sexpr import Q, dump

G = 1.27
_PIN_TYPES = {"power_in": "power_in", "power_out": "power_out", "input": "input", "output": "output", "bidir": "bidirectional",
              "passive": "passive", "open_collector": "open_collector", "nc": "no_connect"}
_FONT = ["effects", ["font", ["size", 1.27, 1.27]]]
_HIDE = ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]


def _u(seed: str) -> Q:
    return Q(str(uuid.uuid5(uuid.NAMESPACE_URL, "studioforge-sch/" + seed)))


def _snap(v: float, g: float = 2 * G) -> float:
    return round(v / g) * g


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", name)


def _fp_id(comp: Component) -> str:
    fp = comp.footprint
    if fp.lib_id:
        return fp.lib_id
    return f"studioforge:{fp.name}"


# ---- box symbols --------------------------------------------------------------------------------
def box_symbol(part: PartDef) -> tuple[list, list[dict]]:
    """('studioforge:<mpn>' lib symbol node, pins [{number, x, y, angle, length, name}]) drawn as a rectangle with
    inputs/power-in on the left and everything else on the right."""
    name = _safe(part.mpn or "part")
    left, right = [], []
    for p in part.pins:
        (left if p.etype in ("power_in", "input") or (p.etype in ("passive", "nc") and len(left) <= len(right)) else right).append(p)
    if not right and len(left) > 1:
        half = (len(left) + 1) // 2
        left, right = left[:half], left[half:]
    n = max(len(left), len(right), 1)
    lw = max([len(p.name) for p in left] + [1]); rw = max([len(p.name) for p in right] + [1])
    w = _snap(max(10.16, (lw + rw) * 1.5 + 5.08), 2 * G)
    h = 2.54 * (n + 1)
    pins = []
    top = h / 2
    for side, lst in ((0, left), (1, right)):
        for i, p in enumerate(lst):
            y = round(top - 2.54 * (i + 1), 4)
            x = -(w / 2 + 2.54) if side == 0 else (w / 2 + 2.54)
            pins.append({"number": p.number, "name": p.name, "type": _PIN_TYPES.get(p.etype, "passive"), "x": x, "y": y,
                         "angle": 0 if side == 0 else 180, "length": 2.54})
    sym = ["symbol", Q(f"studioforge:{name}"), ["in_bom", "yes"], ["on_board", "yes"],
           ["property", Q("Reference"), Q("U"), ["at", 0, round(top + 1.27, 4), 0], _FONT],
           ["property", Q("Value"), Q(part.mpn), ["at", 0, round(-top - 1.27, 4), 0], _FONT],
           ["property", Q("Footprint"), Q(""), ["at", 0, 0, 0], _HIDE],
           ["property", Q("Datasheet"), Q(part.datasheet_url or "~"), ["at", 0, 0, 0], _HIDE],
           ["symbol", Q(f"{name}_0_1"),
            ["rectangle", ["start", -w / 2, top], ["end", w / 2, -top], ["stroke", ["width", 0.254], ["type", "default"]],
             ["fill", ["type", "background"]]]]]
    unit = ["symbol", Q(f"{name}_1_1")]
    for q in pins:
        unit.append(["pin", q["type"], "line", ["at", q["x"], q["y"], q["angle"]], ["length", q["length"]],
                     ["name", Q(q["name"]), _FONT], ["number", Q(q["number"]), _FONT]])
    sym.append(unit)
    return sym, pins


# ---- build -----------------------------------------------------------------------------------------
class SchResult(str):
    """The schematic text; `.symbols` maps ref -> lib_id used ('studioforge:..' for generated boxes)."""
    symbols: dict[str, str]


def _prop_at(node: list, key: str) -> tuple[float, float]:
    for c in node:
        if isinstance(c, list) and c and c[0] == "property" and str(c[1]) == key:
            at = sexpr.find(c, "at")
            if at:
                return float(at[1]), float(at[2])
    return 0.0, 0.0


def to_kicad_sch(circuit: Circuit, title: str | None = None, use_kicad_symbols: bool = True) -> SchResult:
    root_id = _u("root:" + circuit.name)
    pin_net = circuit.pin_net_map()
    lib_syms: dict[str, list] = {}
    comp_sym: dict[str, tuple[str, list[dict], list]] = {}
    for ref, comp in circuit.components.items():
        part = comp.part
        lid = ks.symbol_for_part(part) if use_kicad_symbols else None
        node = ks.symbol_node(lid) if lid else None
        if node is not None:
            pins = ks.symbol_pins(lid)
        else:
            lid = f"studioforge:{_safe(part.mpn or 'part')}"
            if lid not in lib_syms:
                node, pins = box_symbol(part)
                lib_syms[lid] = node
            else:
                node = lib_syms[lid]
                pins = box_symbol(part)[1]
        lib_syms.setdefault(lid, node)
        comp_sym[ref] = (lid, pins, node)

    # ---- layout: blocks stacked vertically, shelf-packed rows inside --------------------------------
    def extent(ref):
        lid, pins, node = comp_sym[ref]
        nets = [circuit.net_of(f"{ref}.{p['number']}") or "" for p in pins]
        x0 = x1 = y0 = y1 = 0.0
        for p, nm in zip(pins, nets):
            lab = (len(nm) + 2) * 1.4 if nm else 0.0
            out = (p["angle"] + 180) % 360
            px, py = p["x"], -p["y"]                     # schematic orientation (y down)
            dx, dy = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[int(out) % 360]
            x0, x1 = min(x0, px, px + dx * lab), max(x1, px, px + dx * lab)
            y0, y1 = min(y0, py, py + dy * lab), max(y1, py, py + dy * lab)
        for k in ("Reference", "Value"):
            fx, fy = _prop_at(node, k)
            x0, x1, y0, y1 = min(x0, fx - 6), max(x1, fx + 6), min(y0, -fy - 2), max(y1, -fy + 2)
        # body graphics of KiCad symbols without pins on the sides (e.g. 2-pin passives) stay inside the pin bbox
        return x0 - 3, x1 + 3, y0 - 3, y1 + 3

    groups: dict[str, list[str]] = {}
    for ref, comp in circuit.components.items():
        groups.setdefault(comp.block or "main", []).append(ref)

    def order(ref):
        m = re.match(r"([A-Za-z]+)(\d+)", ref)
        pre = m.group(1) if m else ref
        return ({"U": 0, "J": 1, "Q": 2}.get(pre, 3), pre, int(m.group(2)) if m else 0)
    placement: dict[str, tuple[float, float]] = {}
    texts: list[list] = []
    y_cursor, row_w, max_x = 20.0, 280.0, 0.0
    for gname, refs in groups.items():
        refs.sort(key=order)
        texts.append(["text", Q(f"Block: {gname}"), ["at", 15.0, y_cursor, 0],
                      ["effects", ["font", ["size", 3, 3], ["bold", "yes"]], ["justify", "left", "bottom"]], ["uuid", _u("blk:" + gname)]])
        y_cursor += 8
        x, row_h = 15.0, 0.0
        for ref in refs:
            ex0, ex1, ey0, ey1 = extent(ref)
            wcell, hcell = ex1 - ex0, ey1 - ey0
            if x > 15.0 and x + wcell > row_w:
                x, y_cursor, row_h = 15.0, y_cursor + row_h + 4, 0.0
            placement[ref] = (_snap(x - ex0), _snap(y_cursor - ey0))
            x += wcell + 4
            row_h = max(row_h, hcell)
            max_x = max(max_x, x)
        y_cursor += row_h + 14
    papers = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]
    paper = next((n for n, w, h in papers if max_x + 20 <= w and y_cursor + 20 <= h), "A0")

    out: list = ["kicad_sch", ["version", 20230121], ["generator", Q("studioforge")], ["uuid", root_id], ["paper", Q(paper)],
                 ["title_block", ["title", Q(title or circuit.name)], ["comment", 1, Q("generated by studioforge")]]]
    out.append(["lib_symbols"] + list(lib_syms.values()))
    project = _safe(circuit.name)
    for ref, comp in circuit.components.items():
        lid, pins, node = comp_sym[ref]
        ox, oy = placement[ref]
        rx, ry = _prop_at(node, "Reference")
        vx, vy = _prop_at(node, "Value")
        sym: list = ["symbol", ["lib_id", Q(lid)], ["at", ox, oy, 0], ["unit", 1], ["in_bom", "yes" if comp.part.category not in ("mounting",) else "no"],
                     ["on_board", "yes"], ["dnp", "yes" if comp.dnp else "no"], ["fields_autoplaced", "yes"], ["uuid", _u("sym:" + ref)],
                     ["property", Q("Reference"), Q(ref), ["at", round(ox + rx, 4), round(oy - ry, 4), 0], _FONT],
                     ["property", Q("Value"), Q(comp.value or comp.part.mpn), ["at", round(ox + vx, 4), round(oy - vy, 4), 0], _FONT],
                     ["property", Q("Footprint"), Q(_fp_id(comp)), ["at", ox, oy, 0], _HIDE],
                     ["property", Q("Datasheet"), Q(comp.part.datasheet_url or "~"), ["at", ox, oy, 0], _HIDE],
                     ["property", Q("MPN"), Q(comp.part.mpn), ["at", ox, oy, 0], _HIDE]]
        for p in pins:
            sym.append(["pin", Q(p["number"]), ["uuid", _u(f"pin:{ref}.{p['number']}")]])
        sym.append(["instances", ["project", Q(project), ["path", Q("/" + str(root_id)), ["reference", Q(ref)], ["unit", 1]]]])
        out.append(sym)
        for p in pins:
            px, py = round(ox + p["x"], 4), round(oy - p["y"], 4)
            net = pin_net.get(f"{ref}.{p['number']}")
            if net is None:
                out.append(["no_connect", ["at", px, py], ["uuid", _u(f"nc:{ref}.{p['number']}")]])
                continue
            rot = int((p["angle"] + 180) % 360)
            just = ["justify", "left"] if rot in (0, 90) else ["justify", "right"]
            out.append(["global_label", Q(net), ["shape", "passive"], ["at", px, py, rot], ["fields_autoplaced", "yes"],
                        ["effects", ["font", ["size", 1.27, 1.27]], just], ["uuid", _u(f"lbl:{ref}.{p['number']}")],
                        ["property", Q("Intersheetrefs"), Q("${INTERSHEET_REFS}"), ["at", px, py, 0],
                         ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]]])
    out += texts
    out.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
    res = SchResult(dump(out) + "\n")
    res.symbols = {ref: v[0] for ref, v in comp_sym.items()}
    return res


def write_kicad_sch(circuit: Circuit, path: str | Path, **kw) -> SchResult:
    txt = to_kicad_sch(circuit, **kw)
    Path(path).write_text(txt)
    return txt
