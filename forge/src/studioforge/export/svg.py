"""SVG board renderer for human review (top view, per-layer colours)."""
from __future__ import annotations

from ..pcb.board import Board
from .font import text_strokes

COL = {"F.Cu": "#c83434", "B.Cu": "#3a5fcd", "In1.Cu": "#2e9b57", "In2.Cu": "#c9a227", "In3.Cu": "#8e44ad", "In4.Cu": "#16a085"}


def _poly_path(poly) -> str:
    d = "M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in poly.exterior.coords) + " Z"
    for h in poly.interiors:
        d += " M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in h.coords) + " Z"
    return d


def board_svg(board: Board, layers: list[str] | None = None, scale: float = 12.0, silk: bool = True, labels: bool = True,
              zones: bool = True) -> str:
    W, H = board.width, board.height
    layers = layers or board.layers
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale:.0f}" height="{H * scale:.0f}" viewBox="0 0 {W:.3f} {H:.3f}">',
           f'<rect width="{W}" height="{H}" fill="#0d3b1f"/>']
    order = list(reversed(layers))
    for layer in order:
        col = COL.get(layer, "#888")
        op = 0.55 if layer != layers[0] else 0.9
        if zones:
            for z in board.zones:
                if z.layer == layer:
                    for poly in z.polygons:
                        out.append(f'<path d="{_poly_path(poly)}" fill="{col}" fill-opacity="{0.28 if layer != "F.Cu" else 0.35}" fill-rule="evenodd"/>')
        for t in board.tracks:
            if t.layer == layer:
                out.append(f'<line x1="{t.x1}" y1="{t.y1}" x2="{t.x2}" y2="{t.y2}" stroke="{col}" stroke-opacity="{op}" '
                           f'stroke-width="{t.width}" stroke-linecap="round"/>')
    for p in board.pads():
        if p.shape == "circle":
            out.append(f'<circle cx="{p.x}" cy="{p.y}" r="{max(p.w, p.h) / 2}" fill="#d9b44a"/>')
        elif p.shape in ("oval",):
            rr = min(p.w, p.h) / 2
            out.append(f'<rect x="{p.x - p.w / 2}" y="{p.y - p.h / 2}" width="{p.w}" height="{p.h}" rx="{rr}" fill="#d9b44a"/>')
        else:
            out.append(f'<rect x="{p.x - p.w / 2}" y="{p.y - p.h / 2}" width="{p.w}" height="{p.h}" fill="#d9b44a"/>')
        if p.drill:
            out.append(f'<circle cx="{p.x}" cy="{p.y}" r="{p.drill / 2}" fill="#111"/>')
    for v in board.vias:
        out.append(f'<circle cx="{v.x}" cy="{v.y}" r="{v.dia / 2}" fill="#9aa0a6"/><circle cx="{v.x}" cy="{v.y}" r="{v.drill / 2}" fill="#111"/>')
    if silk:
        from ..model import rot_point
        for ref, comp in board.circuit.components.items():
            pl = board.place[ref]
            for s in comp.footprint.silk:
                if s[0] == "line":
                    a = rot_point(s[1], s[2], pl.rot); b = rot_point(s[3], s[4], pl.rot)
                    out.append(f'<line x1="{pl.x + a[0]:.3f}" y1="{pl.y + a[1]:.3f}" x2="{pl.x + b[0]:.3f}" y2="{pl.y + b[1]:.3f}" stroke="#eee" stroke-width="0.15"/>')
                elif s[0] == "circle":
                    c = rot_point(s[1], s[2], pl.rot)
                    out.append(f'<circle cx="{pl.x + c[0]:.3f}" cy="{pl.y + c[1]:.3f}" r="{s[3]}" fill="none" stroke="#eee" stroke-width="0.15"/>')
            if labels:
                x0, y0, x1, y1 = board.courtyard(ref)
                h = 0.8 if (x1 - x0) < 4 else 1.0
                ty = y0 - h * 0.7
                if ty < 0.6:
                    ty = y1 + h * 0.7
                for st in text_strokes(ref, (x0 + x1) / 2, ty, h):
                    out.append('<polyline fill="none" stroke="#fff" stroke-width="0.12" points="' + " ".join(f"{x:.3f},{y:.3f}" for x, y in st) + '"/>')
    pts = " ".join(f"{x},{y}" for x, y in board.outline.exterior.coords)
    out.append(f'<polyline points="{pts}" fill="none" stroke="#f1c40f" stroke-width="0.2"/>')
    out.append("</svg>")
    return "\n".join(out)
