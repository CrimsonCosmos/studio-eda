"""Post-layout engineering analysis: thermal field, EMI heuristics, decoupling check."""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import LineString, MultiPoint, Point
from shapely.ops import unary_union

from ..constraints import Decouple, Loop
from .board import Board

K_CU = 385.0     # W/mK
K_FR4 = 0.3


def thermal_map(board: Board, ambient: float = 25.0, h: float = 9.0, cell: float = 1.0) -> dict:
    """Steady-state 2-D board temperature rise from component dissipation.

    kt * laplacian(T) - 2*h*(T-Ta) + q = 0  (heat spreads through copper/FR4 sheet and leaves by
    natural convection+radiation from both faces; h ~ 9 W/m^2K, still air). Matrix-free CG, numpy only."""
    W, H = board.width, board.height
    nx, ny = max(3, int(math.ceil(W / cell))), max(3, int(math.ceil(H / cell)))
    r = board.rules
    kt = K_FR4 * r.thickness_mm * 1e-3
    for i, layer in enumerate(board.layers):
        oz = r.copper_oz_outer if layer in ("F.Cu", "B.Cu") else r.copper_oz_inner
        t = oz * 35e-6
        frac = 0.9 if (layer in board.plane_nets) else (0.7 if any(layer in v for v in board.pour_nets.values()) else 0.3)
        kt += frac * K_CU * t
    c = cell * 1e-3
    q = np.zeros((ny, nx))
    sources = []
    for ref, comp in board.circuit.components.items():
        if comp.power_w <= 0:
            continue
        x0, y0, x1, y1 = board.courtyard(ref)
        ix0, ix1 = max(0, int(x0 / cell)), min(nx, max(int(x0 / cell) + 1, int(math.ceil(x1 / cell))))
        iy0, iy1 = max(0, int(y0 / cell)), min(ny, max(int(y0 / cell) + 1, int(math.ceil(y1 / cell))))
        n = max(1, (ix1 - ix0) * (iy1 - iy0))
        q[iy0:iy1, ix0:ix1] += comp.power_w / n / (c * c)          # W/m^2
        sources.append((ref, ix0, ix1, iy0, iy1, comp.power_w))
    if not sources:
        return {"peak_rise_c": 0.0, "hot": [], "field": None, "cell": cell}
    gk = kt / (c * c)
    gh = 2 * h

    def A(T):
        out = -4 * T * gk
        out[1:, :] += T[:-1, :] * gk
        out[:-1, :] += T[1:, :] * gk
        out[:, 1:] += T[:, :-1] * gk
        out[:, :-1] += T[:, 1:] * gk
        # board edge: adiabatic (mirror) -> add back missing neighbours
        out[0, :] += T[0, :] * gk
        out[-1, :] += T[-1, :] * gk
        out[:, 0] += T[:, 0] * gk
        out[:, -1] += T[:, -1] * gk
        return -(out) + gh * T          # positive definite: (-L + gh) T = q

    b = q.copy()
    T = np.zeros_like(b)
    res = b - A(T)
    p = res.copy()
    rs = float((res * res).sum())
    for _ in range(2000):
        Ap = A(p)
        alpha = rs / float((p * Ap).sum())
        T += alpha * p
        res -= alpha * Ap
        rn = float((res * res).sum())
        if rn < 1e-14 * max(1.0, float((b * b).sum())):
            break
        p = res + (rn / rs) * p
        rs = rn
    hot = []
    for ref, ix0, ix1, iy0, iy1, pw in sources:
        hot.append({"ref": ref, "power_w": round(pw, 3), "rise_c": round(float(T[iy0:iy1, ix0:ix1].mean()), 1),
                    "peak_c": round(float(T[iy0:iy1, ix0:ix1].max()), 1)})
    hot.sort(key=lambda d: -d["rise_c"])
    return {"peak_rise_c": round(float(T.max()), 1), "ambient_c": ambient, "hot": hot, "field": T, "cell": cell,
            "sheet_conductance_w_per_k": round(kt, 4)}


def decoupling_report(board: Board) -> list[dict]:
    out = []
    for k in board.circuit.constraints:
        if isinstance(k, Decouple):
            try:
                ic_pin = board.pad_pos(k.pin)
                cap = board.circuit.components[k.cap]
                net = board.circuit.net_of(k.pin)
                best = None
                for p in cap.part.pins:
                    if board.circuit.net_of(f"{k.cap}.{p.number}") == net:
                        best = board.pad_pos(f"{k.cap}.{p.number}")
                if best:
                    d = math.hypot(best[0] - ic_pin[0], best[1] - ic_pin[1])
                    out.append({"cap": k.cap, "ic_pin": k.pin, "dist_mm": round(d, 2), "limit_mm": k.max_mm, "ok": d <= k.max_mm * 1.5})
            except Exception:
                continue
    return out


def loop_report(board: Board) -> list[dict]:
    out = []
    pins = board.circuit.pin_net_map()
    for k in board.circuit.constraints:
        if isinstance(k, Loop):
            pts = []
            for ref in k.refs:
                if ref in board.place:
                    pts.extend((p.x, p.y) for p in board.pads_of(ref))
            if len(pts) >= 3:
                hull = MultiPoint(pts).convex_hull
                out.append({"loop": k.name, "area_mm2": round(hull.area, 1), "members": list(k.refs)})
    return out


def crosstalk_report(board: Board, near: float = 1.0) -> list[dict]:
    noisy = {n for n, a in board.circuit.net_attrs.items() if a.noisy}
    sens = {n for n, a in board.circuit.net_attrs.items() if a.sensitive}
    out = []
    for layer in board.layers:
        nl = [LineString([(t.x1, t.y1), (t.x2, t.y2)]).buffer(t.width / 2) for t in board.tracks if t.layer == layer and t.net in noisy]
        if not nl:
            continue
        zone = unary_union(nl).buffer(near)
        for n in sens:
            ln = [LineString([(t.x1, t.y1), (t.x2, t.y2)]) for t in board.tracks if t.layer == layer and t.net == n]
            if not ln:
                continue
            length = unary_union(ln).intersection(zone).length
            if length > 0.5:
                out.append({"layer": layer, "victim": n, "coupled_length_mm": round(length, 1)})
    return out


def reference_plane_report(board: Board) -> dict:
    """Fraction of signal-track length that runs over a solid adjacent reference plane/pour."""
    total = covered = 0.0
    zones = {}
    for z in board.zones:
        zones.setdefault(z.layer, []).extend(z.polygons)
    for t in board.tracks:
        i = board.layers.index(t.layer)
        adj = []
        if i > 0:
            adj.append(board.layers[i - 1])
        if i < len(board.layers) - 1:
            adj.append(board.layers[i + 1])
        ref = [l for l in adj if l in zones and l in board.plane_nets]
        if len(board.layers) == 2:
            ref = [l for l in board.layers if l != t.layer and l in zones]
        L = math.hypot(t.x2 - t.x1, t.y2 - t.y1)
        total += L
        if ref:
            geom = unary_union(zones[ref[0]])
            covered += LineString([(t.x1, t.y1), (t.x2, t.y2)]).intersection(geom).length
    return {"track_mm": round(total, 1), "over_reference_pct": round(100 * covered / total, 1) if total else 100.0}


def analyze(board: Board) -> dict:
    th = thermal_map(board)
    th.pop("field", None)
    return {"thermal": th, "decoupling": decoupling_report(board), "loops": loop_report(board),
            "crosstalk": crosstalk_report(board), "reference_plane": reference_plane_report(board)}
