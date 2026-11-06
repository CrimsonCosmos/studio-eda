"""RS-274X (Gerber X2) + Excellon writers for a routed Board."""
from __future__ import annotations

import math
import os
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from ..pcb.board import Board, PlacedPad
from ..pcb.rules import layer_names
from .font import text_strokes

SCALE = 1_000_000          # 4.6 format, mm


def _c(v: float) -> int:
    return int(round(v * SCALE))


class Gerber:
    def __init__(self, function: str, polarity: str = "Positive", name: str = "studioforge"):
        self.lines: list[str] = []
        self.ap: dict[tuple, int] = {}
        self.ap_defs: list[str] = []
        self.cur_ap: int | None = None
        self.function = function
        self.polarity = polarity
        self.body: list[str] = []
        self._pos = (None, None)

    # ---- apertures ---------------------------------------------------------------------------
    def aperture(self, kind: str, *dims: float) -> int:
        key = (kind,) + tuple(round(d, 6) for d in dims)
        if key not in self.ap:
            n = 10 + len(self.ap)
            self.ap[key] = n
            params = "X".join(f"{d:.6f}" for d in dims)
            self.ap_defs.append(f"%ADD{n}{kind},{params}*%")
        return self.ap[key]

    def _select(self, n: int) -> None:
        if self.cur_ap != n:
            self.body.append(f"D{n}*")
            self.cur_ap = n

    # ---- primitives -------------------------------------------------------------------------------
    def _move(self, x, y):
        self.body.append(f"X{_c(x)}Y{_c(y)}D02*")

    def _draw(self, x, y):
        self.body.append(f"X{_c(x)}Y{_c(y)}D01*")

    def line(self, x1, y1, x2, y2, width: float):
        self._select(self.aperture("C", width))
        self._move(x1, y1)
        self._draw(x2, y2)

    def polyline(self, pts, width: float):
        self._select(self.aperture("C", width))
        self._move(*pts[0])
        for p in pts[1:]:
            self._draw(*p)

    def flash(self, shape: str, x: float, y: float, w: float, h: float):
        if shape == "circle":
            n = self.aperture("C", max(w, h))
        elif shape == "oval":
            n = self.aperture("C", w) if abs(w - h) < 1e-6 else self.aperture("O", w, h)
        else:
            n = self.aperture("R", w, h)
        self._select(n)
        self.body.append(f"X{_c(x)}Y{_c(y)}D03*")

    def region(self, ring, dark: bool = True):
        coords = list(ring.coords)
        self.body.append("%LPD*%" if dark else "%LPC*%")
        self.body.append("G36*")
        self._move(*coords[0])
        for p in coords[1:]:
            self._draw(*p)
        if coords[0] != coords[-1]:
            self._draw(*coords[0])
        self.body.append("G37*")
        self.body.append("%LPD*%")
        self.cur_ap = None

    def polygon(self, poly: Polygon):
        poly = poly.buffer(0)
        geoms = poly.geoms if hasattr(poly, "geoms") else [poly]
        for g in geoms:
            if not isinstance(g, Polygon) or g.is_empty:
                continue
            self.region(g.exterior, True)
            for hole in g.interiors:
                self.region(hole, False)

    def shape_geom(self, geom):
        """Arbitrary shapely geometry as dark regions."""
        geoms = geom.geoms if hasattr(geom, "geoms") else [geom]
        for g in geoms:
            if isinstance(g, Polygon):
                self.polygon(g)

    def text(self) -> str:
        head = [
            "%TF.GenerationSoftware,studioforge,forge,0.1*%",
            f"%TF.FileFunction,{self.function}*%",
            f"%TF.FilePolarity,{self.polarity}*%",
            "%FSLAX46Y46*%", "%MOMM*%", "G01*", "G75*", "%LPD*%",
        ]
        return "\n".join(head + self.ap_defs + self.body + ["M02*"]) + "\n"


# ---- flipping: Gerber is y-up, board is y-down ---------------------------------------------------------
class _Flip:
    def __init__(self, H):
        self.H = H

    def y(self, v):
        return self.H - v

    def pt(self, p):
        return (p[0], self.H - p[1])


def _flipped_geom(g, H):
    from shapely.affinity import scale, translate
    return translate(scale(g, 1, -1, origin=(0, 0)), 0, H)


def _pad_flash(g: Gerber, p: PlacedPad, H: float, grow: float = 0.0):
    y = H - p.y
    if p.shape in ("rect",):
        g.flash("rect", p.x, y, p.w + 2 * grow, p.h + 2 * grow)
    elif p.shape == "circle":
        g.flash("circle", p.x, y, p.w + 2 * grow, p.h + 2 * grow)
    elif p.shape == "oval":
        g.flash("oval", p.x, y, p.w + 2 * grow, p.h + 2 * grow)
    else:                                              # roundrect -> exact polygon
        g.shape_geom(_flipped_geom(p.geom(grow), H))


def copper_layer(board: Board, layer: str, idx: int, total: int) -> str:
    H = board.height
    side = "Top" if layer == "F.Cu" else ("Bot" if layer == "B.Cu" else "Inr")
    g = Gerber(f"Copper,L{idx},{side}")
    # zone fills first (dark with clear holes), then explicit copper
    for z in board.zones:
        if z.layer == layer:
            for poly in z.polygons:
                g.polygon(_flipped_geom(poly, H))
    plane_net = board.plane_nets.get(layer)
    for t in board.tracks:
        if t.layer == layer:
            g.line(t.x1, H - t.y1, t.x2, H - t.y2, t.width)
    for p in board.pads():
        if layer == "F.Cu" or p.thru:
            if p.thru and not p.plated:
                continue
            if plane_net is not None and p.net != plane_net:
                continue                                  # antipad already cut in the fill
            if layer == "B.Cu" and not p.thru:
                continue
            _pad_flash(g, p, H)
    for v in board.vias:
        if plane_net is not None and v.net != plane_net:
            continue
        g.flash("circle", v.x, H - v.y, v.dia, v.dia)
    return g.text()


def mask_layer(board: Board, side: str) -> str:
    """Solder-mask openings (negative layer: dark = opening). Vias are tented."""
    H = board.height
    g = Gerber(f"Soldermask,{'Top' if side == 'F' else 'Bot'}", "Negative")
    e = board.rules.mask_expansion
    for p in board.pads():
        if p.thru and not p.plated:
            g.flash("circle", p.x, H - p.y, p.drill + 0.3, p.drill + 0.3)      # NPTH keep-out opening
        elif side == "F" or p.thru:
            _pad_flash(g, p, H, e)
    return g.text()


def paste_layer(board: Board) -> str:
    H = board.height
    g = Gerber("Paste,Top", "Positive")
    for p in board.pads():
        if not p.thru and p.paste:
            _pad_flash(g, p, H, -0.0)
    return g.text()


def silk_layer(board: Board) -> str:
    H = board.height
    g = Gerber("Legend,Top", "Positive")
    sw = board.rules.silk_width
    pad_union = unary_union([p.geom(board.rules.mask_expansion + 0.1) for p in board.pads()]) if board.place else None
    strokes: list = []
    for ref, comp in board.circuit.components.items():
        pl = board.place.get(ref)
        if pl is None:
            continue
        from ..model import rot_point
        fp = comp.footprint
        for s in fp.silk:
            if s[0] == "line":
                a = rot_point(s[1], s[2], pl.rot); b = rot_point(s[3], s[4], pl.rot)
                strokes.append([(pl.x + a[0], pl.y + a[1]), (pl.x + b[0], pl.y + b[1])])
            elif s[0] == "circle":
                c = rot_point(s[1], s[2], pl.rot)
                cx, cy, r = pl.x + c[0], pl.y + c[1], s[3]
                pts = [(cx + r * math.cos(t / 12 * 2 * math.pi), cy + r * math.sin(t / 12 * 2 * math.pi)) for t in range(13)]
                strokes.append(pts)
        # reference designator above the courtyard
        x0, y0, x1, y1 = board.courtyard(ref)
        h = 0.8 if (x1 - x0) < 4 else 1.0
        tx, ty = (x0 + x1) / 2, y0 - h * 0.7
        if ty < 0.6:
            ty = y1 + h * 0.7
        for st in text_strokes(ref, tx, ty, h):
            strokes.append(st)
    for st in strokes:
        line = LineString(st)
        if pad_union is not None:
            line = line.difference(pad_union)
        lines = line.geoms if hasattr(line, "geoms") else [line]
        for ln in lines:
            if ln.is_empty or ln.length < 0.05:
                continue
            pts = [(x, H - y) for x, y in ln.coords]
            g.polyline(pts, sw)
    return g.text()


def outline_layer(board: Board) -> str:
    H = board.height
    g = Gerber("Profile,NP", "Positive")
    coords = list(board.outline.exterior.coords)
    pts = [(x, H - y) for x, y in coords]
    g.polyline(pts, 0.1)
    return g.text()


def excellon(board: Board, plated: bool) -> str:
    H = board.height
    holes: dict[float, list[tuple[float, float]]] = {}
    slots: dict[float, list[tuple[float, float, float, float]]] = {}
    for p in board.pads():
        if p.drill > 0 and p.plated == plated:
            if p.slot > 0:                                   # routed slot (G85): centre-line endpoints, tool = slot width
                h = p.slot / 2
                if p.slot_y:
                    slots.setdefault(round(p.drill, 3), []).append((p.x, H - p.y - h, p.x, H - p.y + h))
                else:
                    slots.setdefault(round(p.drill, 3), []).append((p.x - h, H - p.y, p.x + h, H - p.y))
            else:
                holes.setdefault(round(p.drill, 3), []).append((p.x, H - p.y))
    if plated:
        for v in board.vias:
            holes.setdefault(round(v.drill, 3), []).append((v.x, H - v.y))
    if not holes and not slots:
        return ""
    func = "Plated,1,%d,PTH" % board.rules.layers if plated else "NonPlated,1,%d,NPTH" % board.rules.layers
    out = ["M48", "; DRILL file {studioforge} date unspecified", "; FORMAT={-:-/ absolute / metric / decimal}",
           f"; #@! TF.FileFunction,{func}", "FMAT,2", "METRIC,TZ"]
    order = sorted(set(holes) | set(slots))
    for i, d in enumerate(order, 1):
        out.append(f"T{i}C{d:.3f}")
    out += ["G90", "%", "G05"]
    for i, d in enumerate(order, 1):
        out.append(f"T{i}")
        seen = set()
        for (x, y) in holes.get(d, []):
            key = (round(x, 3), round(y, 3))
            if key in seen:
                continue
            seen.add(key)
            out.append(f"X{x:.3f}Y{y:.3f}")
        for (x1, y1, x2, y2) in slots.get(d, []):
            out.append(f"X{x1:.3f}Y{y1:.3f}G85X{x2:.3f}Y{y2:.3f}")
    out += ["T0", "M30"]
    return "\n".join(out) + "\n"


def write_gerbers(board: Board, outdir: str | Path, project: str) -> dict[str, Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    n = len(board.layers)
    for i, layer in enumerate(board.layers, 1):
        fn = out / f"{project}-{layer.replace('.', '_')}.gbr"
        fn.write_text(copper_layer(board, layer, i, n))
        files[layer] = fn
    spec = {
        "F.Mask": (f"{project}-F_Mask.gbr", mask_layer(board, "F")),
        "B.Mask": (f"{project}-B_Mask.gbr", mask_layer(board, "B")),
        "F.Paste": (f"{project}-F_Paste.gbr", paste_layer(board)),
        "F.Silk": (f"{project}-F_Silkscreen.gbr", silk_layer(board)),
        "Edge": (f"{project}-Edge_Cuts.gbr", outline_layer(board)),
    }
    for k, (name, text) in spec.items():
        (out / name).write_text(text)
        files[k] = out / name
    pth = excellon(board, True)
    if pth:
        (out / f"{project}-PTH.drl").write_text(pth)
        files["PTH"] = out / f"{project}-PTH.drl"
    npth = excellon(board, False)
    if npth:
        (out / f"{project}-NPTH.drl").write_text(npth)
        files["NPTH"] = out / f"{project}-NPTH.drl"
    return files
