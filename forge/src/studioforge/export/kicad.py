"""KiCad exports: .kicad_pcb (v7 format), XML netlist, and a net-label based .kicad_sch."""
from __future__ import annotations

import math
import uuid
from xml.sax.saxutils import escape

from .. import sexpr
from ..sexpr import Q, dump
from ..model import Circuit, Pad
from ..pcb.board import Board


def _uuid(seed: str) -> Q:
    return Q(str(uuid.uuid5(uuid.NAMESPACE_URL, "studioforge/" + seed)))


def _layers_def(n: int):
    layers = [[0, Q("F.Cu"), "signal"]]
    for i in range(1, n - 1):
        layers.append([i, Q(f"In{i}.Cu"), "signal"])
    layers.append([31, Q("B.Cu"), "signal"])
    layers += [[32, Q("B.Adhes"), "user", Q("B.Adhesive")], [33, Q("F.Adhes"), "user", Q("F.Adhesive")],
               [34, Q("B.Paste"), "user"], [35, Q("F.Paste"), "user"], [36, Q("B.SilkS"), "user", Q("B.Silkscreen")],
               [37, Q("F.SilkS"), "user", Q("F.Silkscreen")], [38, Q("B.Mask"), "user"], [39, Q("F.Mask"), "user"],
               [40, Q("Dwgs.User"), "user", Q("User.Drawings")], [41, Q("Cmts.User"), "user", Q("User.Comments")],
               [44, Q("Edge.Cuts"), "user"], [45, Q("Margin"), "user"], [46, Q("B.CrtYd"), "user", Q("B.Courtyard")],
               [47, Q("F.CrtYd"), "user", Q("F.Courtyard")], [48, Q("B.Fab"), "user"], [49, Q("F.Fab"), "user"]]
    return ["layers"] + layers


def to_kicad_pcb(board: Board, title: str = "studioforge") -> str:
    c = board.circuit
    nets = {"": 0}
    for n in c.nets:
        nets[n] = len(nets)
    root: list = ["kicad_pcb", ["version", 20221018], ["generator", Q("studioforge")],
                  ["general", ["thickness", board.rules.thickness_mm]], ["paper", Q("A4")],
                  _layers_def(len(board.layers)),
                  ["setup", ["pad_to_mask_clearance", board.rules.mask_expansion],
                   ["pcbplotparams", ["layerselection", "0x00010fc_ffffffff"], ["usegerberextensions", "false"]]]]
    for n, i in nets.items():
        root.append(["net", i, Q(n)])
    # footprints
    for ref, comp in c.components.items():
        pl = board.place[ref]
        fp = comp.footprint
        pn = c.pin_net_map()
        fl: list = ["footprint", Q(f"studioforge:{fp.name}"), ["layer", Q("F.Cu")], ["uuid", _uuid(ref)],
                    ["at", pl.x, pl.y, pl.rot] if pl.rot else ["at", pl.x, pl.y],
                    ["property", Q("Reference"), Q(ref), ["at", 0, -(abs(fp.courtyard[1]) + 1.0), 0], ["layer", Q("F.SilkS")],
                     ["effects", ["font", ["size", 0.8, 0.8], ["thickness", 0.12]]]],
                    ["property", Q("Value"), Q(comp.value or comp.part.mpn), ["at", 0, abs(fp.courtyard[3]) + 1.0, 0], ["layer", Q("F.Fab")],
                     ["effects", ["font", ["size", 0.8, 0.8], ["thickness", 0.12]]]],
                    ["property", Q("MPN"), Q(comp.part.mpn), ["at", 0, 0, 0], ["layer", Q("F.Fab")], ["hide", "yes"],
                     ["effects", ["font", ["size", 1, 1], ["thickness", 0.15]]]],
                    ["attr", "smd" if not any(p.thru for p in fp.pads) else "through_hole"]]
        for s in fp.silk:
            if s[0] == "line":
                fl.append(["fp_line", ["start", s[1], s[2]], ["end", s[3], s[4]], ["stroke", ["width", 0.12], ["type", "solid"]], ["layer", Q("F.SilkS")], ["uuid", _uuid(f"{ref}-s{len(fl)}")]])
            elif s[0] == "circle":
                fl.append(["fp_circle", ["center", s[1], s[2]], ["end", s[1] + s[3], s[2]], ["stroke", ["width", 0.12], ["type", "solid"]], ["fill", "none"], ["layer", Q("F.SilkS")], ["uuid", _uuid(f"{ref}-c{len(fl)}")]])
        x0, y0, x1, y1 = fp.courtyard
        fl.append(["fp_rect", ["start", x0, y0], ["end", x1, y1], ["stroke", ["width", 0.05], ["type", "solid"]], ["fill", "none"], ["layer", Q("F.CrtYd")], ["uuid", _uuid(f"{ref}-cy")]])
        for pad in fp.pads:
            net = pn.get(f"{ref}.{pad.number}") if pad.number else None
            thru = pad.thru
            ptype = "thru_hole" if (thru and pad.plated) else ("np_thru_hole" if thru else "smd")
            shape = {"rect": "rect", "circle": "circle", "oval": "oval", "roundrect": "roundrect"}.get(pad.shape, "rect")
            layers = ["*.Cu", "*.Mask"] if thru else ["F.Cu", "F.Paste", "F.Mask"] if pad.paste else ["F.Cu", "F.Mask"]
            pl_ = ["pad", Q(pad.number), ptype, shape, ["at", pad.x, pad.y, pl.rot] if pl.rot else ["at", pad.x, pad.y],
                   ["size", pad.w, pad.h]]
            if thru:
                pl_.append(["drill", pad.drill])
            pl_.append(["layers"] + [Q(l) for l in layers])
            if shape == "roundrect":
                pl_.append(["roundrect_rratio", pad.rratio])
            if net:
                pl_.append(["net", nets[net], Q(net)])
            pl_.append(["uuid", _uuid(f"{ref}-p{pad.number}-{pad.x}-{pad.y}")])
            fl.append(pl_)
        root.append(fl)
    k = 0
    for t in board.tracks:
        k += 1
        root.append(["segment", ["start", t.x1, t.y1], ["end", t.x2, t.y2], ["width", t.width], ["layer", Q(t.layer)], ["net", nets[t.net]], ["uuid", _uuid(f"t{k}")]])
    for v in board.vias:
        k += 1
        root.append(["via", ["at", v.x, v.y], ["size", v.dia], ["drill", v.drill], ["layers", Q("F.Cu"), Q("B.Cu")], ["net", nets[v.net]], ["uuid", _uuid(f"v{k}")]])
    # outline
    pts = list(board.outline.exterior.coords)
    for a, b in zip(pts[:-1], pts[1:]):
        k += 1
        root.append(["gr_line", ["start", a[0], a[1]], ["end", b[0], b[1]], ["stroke", ["width", 0.1], ["type", "default"]], ["layer", Q("Edge.Cuts")], ["uuid", _uuid(f"e{k}")]])
    # zones (with our computed fill so the file opens identical; KiCad can refill with 'B')
    for z in board.zones:
        if not z.polygons or z.net not in nets:
            continue
        k += 1
        outline = board.outline.buffer(-board.rules.edge_clearance)
        if z.kind == "power":                      # pre-shaped pour: its own outline, not the whole layer
            outline = z.polygons[0]
        zl = ["zone", ["net", nets[z.net]], ["net_name", Q(z.net)], ["layer", Q(z.layer)], ["uuid", _uuid(f"z{k}")],
              ["hatch", "edge", 0.5], ["connect_pads", ["clearance", board.rules.clearance]], ["min_thickness", 0.2],
              ["filled_areas_thickness", "no"],
              ["fill", "yes", ["thermal_gap", board.rules.plane_thermal_gap], ["thermal_bridge_width", board.rules.plane_spoke]],
              ["polygon", ["pts"] + [["xy", x, y] for x, y in outline.exterior.coords[:-1]]]]
        for poly in z.polygons:
            zl.append(["filled_polygon", ["layer", Q(z.layer)], ["pts"] + [["xy", x, y] for x, y in poly.exterior.coords[:-1]]])
        root.append(zl)
    return dump(root) + "\n"


def to_kicad_netlist(circuit: Circuit) -> str:
    """KiCad XML netlist (importable via Pcbnew > File > Import > Netlist)."""
    out = ['(export (version "E")', f'  (design (source "{escape(circuit.name)}") (tool "studioforge"))', "  (components"]
    for ref, c in circuit.components.items():
        fp = c.part.footprint if isinstance(c.part.footprint, str) else c.part.footprint.name
        out.append(f'    (comp (ref "{ref}") (value "{escape(c.value or c.part.mpn)}") (footprint "studioforge:{escape(fp)}")'
                   f' (fields (field (name "MPN") "{escape(c.part.mpn)}")))')
    out.append("  )")
    out.append("  (nets")
    for i, (n, pins) in enumerate(circuit.nets.items(), 1):
        out.append(f'    (net (code "{i}") (name "{escape(n)}")')
        for p in pins:
            ref, _, pin = p.partition(".")
            out.append(f'      (node (ref "{ref}") (pin "{pin}"))')
        out.append("    )")
    out.append("  )\n)")
    return "\n".join(out) + "\n"
