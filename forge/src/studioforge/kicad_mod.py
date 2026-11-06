"""Import KiCad .kicad_mod footprints (both legacy `module` and v6+ `footprint`)."""
from __future__ import annotations
from pathlib import Path

from . import sexpr
from .model import Footprint, Pad


def _at(node):
    a = sexpr.find(node, "at")
    if not a:
        return 0.0, 0.0, 0.0
    return float(a[1]), float(a[2]), float(a[3]) if len(a) > 3 else 0.0


def parse_footprint(text: str) -> Footprint:
    root = sexpr.parse(text)
    if not root or root[0] not in ("footprint", "module"):
        raise ValueError("not a KiCad footprint")
    name = str(root[1])
    pads: list[Pad] = []
    cx = []
    cy = []
    silk: list[tuple] = []
    for item in root[2:]:
        if not isinstance(item, list) or not item:
            continue
        h = item[0]
        if h == "pad":
            number, ptype, shape = str(item[1]), str(item[2]), str(item[3])
            x, y, rot = _at(item)
            sz = sexpr.find(item, "size")
            w, hh = float(sz[1]), float(sz[2])
            if int(round(rot)) % 180 == 90:
                w, hh = hh, w
            drill, slot, slot_y = 0.0, 0.0, True
            dr = sexpr.find(item, "drill")
            if dr:
                nums = []
                for t in dr[1:]:
                    try:
                        nums.append(float(t))
                    except (TypeError, ValueError):
                        pass
                if "oval" in dr and len(nums) >= 2 and abs(nums[0] - nums[1]) > 1e-6:
                    drill, slot = min(nums[:2]), abs(nums[0] - nums[1])
                    slot_y = nums[1] > nums[0]
                    if int(round(rot)) % 180 == 90:
                        slot_y = not slot_y
                else:
                    drill = max(nums) if nums else 0.0
            layers = sexpr.find(item, "layers")
            lnames = [str(t) for t in layers[1:]] if layers else []
            smd = ptype == "smd"
            if smd and not any(l.endswith(".Cu") or l == "*.Cu" for l in lnames):
                continue                      # paste-only / mask-only sub-aperture, not copper
            if shape in ("custom", "trapezoid"):
                shape = "rect"
            if ptype == "np_thru_hole":
                number = ""
            paste = smd and any("Paste" in l for l in lnames)
            pads.append(Pad(number, x, y, w, hh, shape if shape in ("rect", "circle", "oval", "roundrect") else "rect",
                            0.0 if smd else drill, ptype != "np_thru_hole", paste, 0.25, 0.0 if smd else slot, slot_y))
        elif h in ("fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc"):
            layer = sexpr.find(item, "layer")
            ln = str(layer[1]) if layer else ""
            if "CrtYd" in ln:
                pts = []
                if h == "fp_poly":
                    pts_node = sexpr.find(item, "pts")
                    pts = [(float(p[1]), float(p[2])) for p in pts_node[1:] if isinstance(p, list) and p[0] == "xy"]
                else:
                    for key in ("start", "end", "center"):
                        s = sexpr.find(item, key)
                        if s:
                            pts.append((float(s[1]), float(s[2])))
                for px, py in pts:
                    cx.append(px); cy.append(py)
            elif ln == "F.SilkS" and h == "fp_line":
                s, e = sexpr.find(item, "start"), sexpr.find(item, "end")
                silk.append(("line", float(s[1]), float(s[2]), float(e[1]), float(e[2])))
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
