"""Import KiCad .kicad_mod footprints (both legacy `module` and v6+ `footprint`).

Pads are reduced to what the placer/router/DRC need: centre, axis-aligned size (rotated pads -> bounding box,
custom shapes -> bbox of anchor + primitives), drill (+ slot length for oval holes), copper/paste flags.
Courtyard = bbox of F.CrtYd graphics; silk = F.SilkS lines/rects/circles/arcs/polys as lines + circles.
3D models and zones are ignored.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import sexpr
from .model import Footprint, Pad


def _at(node):
    a = sexpr.find(node, "at")
    if not a:
        return 0.0, 0.0, 0.0
    return float(a[1]), float(a[2]), float(a[3]) if len(a) > 3 else 0.0


def _xy(node, key):
    s = sexpr.find(node, key)
    return (float(s[1]), float(s[2])) if s else None


def _rot(x, y, deg):
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def _pts(node) -> list[tuple[float, float]]:
    pn = sexpr.find(node, "pts")
    return [(float(p[1]), float(p[2])) for p in (pn[1:] if pn else []) if isinstance(p, list) and p and p[0] == "xy"]


def _arc_pts(item) -> list[tuple[float, float]]:
    s, m, e = _xy(item, "start"), _xy(item, "mid"), _xy(item, "end")
    if s and m and e:
        return [s, m, e]
    return [p for p in (s, e) if p]            # legacy (start=centre, end=point, angle) - approximate


def _gfx_points(item) -> list[tuple[float, float]]:
    """Points whose bbox bounds a graphic item (circle -> centre +- radius)."""
    h = item[0]
    if h in ("fp_poly", "gr_poly"):
        return _pts(item)
    if h in ("fp_circle", "gr_circle"):
        c, e = _xy(item, "center"), _xy(item, "end")
        if not c or not e:
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if h in ("fp_arc", "gr_arc"):
        return _arc_pts(item)
    return [p for p in (_xy(item, "start"), _xy(item, "end")) if p]


def _pad_geometry(item, w, hh, rot):
    """(dx, dy, w, h) of the pad's axis-aligned footprint relative to its `at` point, covering rotation and custom primitives."""
    prim = sexpr.find(item, "primitives")
    pts = [(-w / 2, -hh / 2), (w / 2, -hh / 2), (w / 2, hh / 2), (-w / 2, hh / 2)]
    if prim:
        for pr in prim[1:]:
            if isinstance(pr, list) and pr and pr[0].startswith("gr_"):
                pts += _gfx_points(pr)
    rd = sexpr.find(item, "rect_delta")
    if rd:
        dx, dy = abs(float(rd[1])), abs(float(rd[2]))
        pts += [(-w / 2 - dy / 2, -hh / 2), (w / 2 + dy / 2, hh / 2), (-w / 2, -hh / 2 - dx / 2), (w / 2, hh / 2 + dx / 2)]
    rp = [_rot(x, y, -rot) for x, y in pts]       # KiCad angles are CCW on a y-down board: x' = x cos + y sin
    xs, ys = [p[0] for p in rp], [p[1] for p in rp]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)


def parse_footprint(text: str, name: str | None = None) -> Footprint:
    root = sexpr.parse(text)
    if not root or root[0] not in ("footprint", "module"):
        raise ValueError("not a KiCad footprint")
    name = name or str(root[1])
    pads: list[Pad] = []
    nomask: list[bool] = []
    cx: list[float] = []
    cy: list[float] = []
    silk: list[tuple] = []

    def silk_line(a, b):
        if a != b:
            silk.append(("line", a[0], a[1], b[0], b[1]))

    for item in root[2:]:
        if not isinstance(item, list) or not item:
            continue
        h = item[0]
        if h == "pad":
            number, ptype, shape = str(item[1]), str(item[2]), str(item[3])
            x, y, rot = _at(item)
            sz = sexpr.find(item, "size")
            w, hh = float(sz[1]), float(sz[2])
            layers = sexpr.find(item, "layers")
            lnames = [str(t) for t in layers[1:]] if layers else []
            smd = ptype == "smd"
            if smd and not any(l.endswith(".Cu") or l == "*.Cu" for l in lnames):
                continue                      # paste-only / mask-only sub-aperture, not copper
            quarter = abs(rot / 90.0 - round(rot / 90.0)) < 1e-6
            odd = quarter and int(round(rot / 90.0)) % 2 == 1
            if shape == "custom" or sexpr.find(item, "rect_delta") or not quarter:
                dx, dy, w2, h2 = _pad_geometry(item, w, hh, rot)
                x, y, w, hh = x + dx, y + dy, w2, h2
                oshape = "rect" if shape in ("custom", "trapezoid") or not quarter else shape
                if not quarter and shape == "circle":
                    oshape = "circle"
            else:
                if odd:
                    w, hh = hh, w
                oshape = shape
            drill, slot, slot_y = 0.0, 0.0, True
            dr = sexpr.find(item, "drill")
            if dr and not smd:
                nums = []
                for t in dr[1:]:
                    try:
                        nums.append(float(t))
                    except (TypeError, ValueError):
                        pass
                if "oval" in dr and len(nums) >= 2 and abs(nums[0] - nums[1]) > 1e-6:
                    drill, slot = min(nums[:2]), abs(nums[0] - nums[1])
                    slot_y = nums[1] > nums[0]
                    if odd:
                        slot_y = not slot_y
                    if not quarter:
                        drill, slot = max(nums[:2]), 0.0       # slot on a free-angle pad: keep it as a round max-size hole
                else:
                    drill = max(nums) if nums else 0.0
            if ptype == "np_thru_hole":
                number = ""
            rr = sexpr.find(item, "roundrect_rratio")
            rratio = float(rr[1]) if rr else 0.25
            paste = smd and any("Paste" in l for l in lnames)
            if oshape not in ("rect", "circle", "oval", "roundrect"):
                oshape = "rect"
            pads.append(Pad(number, x, y, w, hh, oshape, 0.0 if smd else drill, ptype != "np_thru_hole", paste, rratio,
                            0.0 if smd else slot, slot_y))
            nomask.append(smd and not any("Mask" in l for l in lnames))
        elif h in ("fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc"):
            layer = sexpr.find(item, "layer")
            ln = str(layer[1]) if layer else ""
            if "CrtYd" in ln:
                for px, py in _gfx_points(item):
                    cx.append(px); cy.append(py)
            elif ln == "F.SilkS":
                if h == "fp_line":
                    silk_line(_xy(item, "start"), _xy(item, "end"))
                elif h == "fp_rect":
                    (x0, y0), (x1, y1) = _xy(item, "start"), _xy(item, "end")
                    silk_line((x0, y0), (x1, y0)); silk_line((x1, y0), (x1, y1))
                    silk_line((x1, y1), (x0, y1)); silk_line((x0, y1), (x0, y0))
                elif h == "fp_circle":
                    c, e = _xy(item, "center"), _xy(item, "end")
                    silk.append(("circle", c[0], c[1], math.hypot(e[0] - c[0], e[1] - c[1])))
                elif h == "fp_poly":
                    pp = _pts(item)
                    for a, b in zip(pp, pp[1:] + pp[:1]):
                        silk_line(a, b)
                elif h == "fp_arc":
                    ap = _arc_pts(item)
                    for a, b in zip(ap, ap[1:]):
                        silk_line(a, b)
    # copper-layer paste-segmentation pads (F.Cu + F.Paste, no mask) that sit inside a same-numbered main pad are not extra pads
    keep: list[Pad] = []
    for i, p in enumerate(pads):
        if nomask[i] and p.number and any(
                (j != i and not nomask[j] and q.number == p.number and abs(q.x - p.x) + p.w / 2 <= q.w / 2 + 1e-6 + 0 and
                 abs(q.y - p.y) + p.h / 2 <= q.h / 2 + 1e-6) for j, q in enumerate(pads)):
            continue
        keep.append(p)
    pads = keep
    if cx:
        cyard = (min(cx), min(cy), max(cx), max(cy))
    elif pads:
        cyard = (min(p.x - p.w / 2 for p in pads) - 0.25, min(p.y - p.h / 2 for p in pads) - 0.25,
                 max(p.x + p.w / 2 for p in pads) + 0.25, max(p.y + p.h / 2 for p in pads) + 0.25)
    else:
        cyard = (-1, -1, 1, 1)
    return Footprint(name, pads, cyard, silk, 1.0, f"kicad:{name}")


def load_file(path: str | Path) -> Footprint:
    return parse_footprint(Path(path).read_text())
