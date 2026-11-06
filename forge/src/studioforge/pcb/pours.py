"""High-current copper pours.

Nets carrying more than a few amps cannot be gridded as wide traces between small pads
(pad-to-pad clearance forbids it). Like a human designer, we pour them as polygons: pads of
the net are bridged by strips of the IPC-2221 width, the result is clipped against every other
net's copper, and kept only if all power pads stay in one connected piece. The router then
connects the remaining small pads of the net to the pour.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from .board import Board, Zone
from .rules import trace_width_for_current


def _mst_edges(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    n = len(points)
    if n < 2:
        return []
    used = {0}
    edges = []
    while len(used) < n:
        best = None
        for i in used:
            for j in range(n):
                if j in used:
                    continue
                d = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
                if best is None or d < best[0]:
                    best = (d, i, j)
        edges.append((best[1], best[2]))
        used.add(best[2])
    return edges


def plan_power_pours(board: Board, min_current: float = 3.0, layer: str = "F.Cu") -> list[str]:
    r = board.rules
    clr = r.clearance + 0.01
    circ = board.circuit
    pads_by_net: dict[str, list] = {}
    for p in board.pads():
        if p.net:
            pads_by_net.setdefault(p.net, []).append(p)
    skip = set(board.plane_nets.values()) | set(board.pour_nets)
    nets = [n for n, a in circ.net_attrs.items() if a.current_a >= min_current and n in pads_by_net and n not in skip
            and a.role != "ground"]
    nets.sort(key=lambda n: -circ.net_attrs[n].current_a)
    edge_guard = board.outline.boundary.buffer(r.edge_clearance + 0.01)
    board.power_pours = []
    done: list[str] = []
    for net in nets:
        plist = pads_by_net[net]
        power_pads = [p for p in plist if (p.thru or p.w * p.h >= 2.0) and (p.thru or layer == "F.Cu")]
        if len(power_pads) < 1:
            continue
        w = max(1.0, trace_width_for_current(circ.net_attrs[net].current_a, r.temp_rise_c, r.copper_oz_outer))
        pad_geoms = [p.geom() for p in power_pads]
        centers = [(p.x, p.y) for p in power_pads]
        result = None
        for small_halo in (0.9, 0.5, 0.15):
            obs = [edge_guard]
            for p in board.pads():
                if p.net != net and (p.thru or layer == "F.Cu"):
                    # small SMD pads of other nets keep a routing halo (room for a via/escape track); never wall them in
                    halo = clr + (small_halo if (not p.thru and p.w * p.h < 6.0) else min(0.35, small_halo))
                    obs.append(p.geom(halo))
            for z in board.power_pours:
                if z.net != net:
                    for g in z.polygons:
                        obs.append(g.buffer(clr, 8))
            for h in board.holes():
                if h[3] is False:                      # NPTH
                    obs.append(Point(h[0], h[1]).buffer(h[2] / 2 + clr, 12))
            obstacles = unary_union(obs)
            strips = []
            for i, j in _mst_edges(centers):
                a, b = centers[i], centers[j]
                variants = [[a, b], [a, (b[0], a[1]), b], [a, (a[0], b[1]), b]]
                chosen = None
                for path in variants:
                    strip = LineString(path).buffer(w / 2, cap_style=2, join_style=2)
                    cand = unary_union([pad_geoms[i], pad_geoms[j], strip]).difference(obstacles)
                    comps = list(cand.geoms) if hasattr(cand, "geoms") else [cand]
                    if any(c.buffer(0.01).contains(Point(*a)) and c.buffer(0.01).contains(Point(*b)) for c in comps):
                        chosen = strip
                        break
                if chosen is None:
                    chosen = LineString([a, b]).buffer(w / 2, cap_style=2, join_style=2)
                strips.append(chosen)
            body = unary_union(pad_geoms + strips)
            body = body.buffer(1.0, 6).buffer(-1.0, 6)          # close small gaps between strips and pads
            poly = unary_union([body, unary_union(pad_geoms)]).difference(obstacles)
            poly = poly.buffer(-0.05, 4).buffer(0.05, 4)
            comps = [g for g in (poly.geoms if hasattr(poly, "geoms") else [poly]) if isinstance(g, Polygon) and not g.is_empty]
            keep = [g for g in comps if any(g.intersects(pg) for pg in pad_geoms)]
            joined = max((sum(1 for pg in pad_geoms if g.buffer(0.01).contains(pg.centroid)) for g in keep), default=0)
            result = (keep, joined, small_halo)
            if keep and joined == len(pad_geoms):
                break
        keep, joined, used_halo = result
        if not keep:
            board.warnings.append(f"power pour for {net}: no copper survived clipping; falling back to traces")
            continue
        if joined < len(pad_geoms):
            board.warnings.append(f"power pour for {net}: only {joined} of {len(pad_geoms)} power pads joined; remainder routed as traces - review ampacity")
        z = Zone(layer, net, "power", 10, keep)
        board.power_pours.append(z)
        done.append(net)
        board.notes.append(f"{net}: {circ.net_attrs[net].current_a:g} A poured on {layer} (strip width {w:.1f} mm, {sum(g.area for g in keep):.0f} mm^2)")
    return done
