"""Independent design-rule check using exact geometry (shapely), plus electrical connectivity.

It deliberately shares no code with the router: a bug in the router's raster logic shows up
here as a violation rather than silently shipping.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .board import Board
from .planes import _layer_items
from .rules import current_for_width


@dataclass
class Violation:
    kind: str
    msg: str
    where: tuple[float, float] | None = None
    severity: str = "error"


@dataclass
class DRCReport:
    violations: list[Violation] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def clean(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for v in self.violations:
            kinds[f"{v.severity}:{v.kind}"] = kinds.get(f"{v.severity}:{v.kind}", 0) + 1
        return ", ".join(f"{k}={n}" for k, n in sorted(kinds.items())) or "clean"


def run_drc(board: Board, check_connectivity: bool = True) -> DRCReport:
    rep = DRCReport()
    r = board.rules
    V = rep.violations
    pads = board.pads()

    # --- per-layer clearance / shorts --------------------------------------------------------
    n_items = 0
    zones_by_layer: dict[str, list] = {}
    for z in board.zones:
        zones_by_layer.setdefault(z.layer, []).extend((z.net, g) for g in z.polygons)
    for layer in board.layers:
        items = []
        for (net, geom, kind, obj) in _layer_items(board, layer):
            if layer in board.plane_nets and kind in ("pad", "via") and net != board.plane_nets[layer]:
                continue           # antipad on plane layers
            items.append((net, geom, kind, obj))
        for (net, geom) in zones_by_layer.get(layer, []):
            items.append((net, geom, "zone", None))
        n_items += len(items)
        geoms = [g for _, g, _, _ in items]
        if not geoms:
            continue
        tree = STRtree(geoms)
        for i, (net, g, kind, obj) in enumerate(items):
            for j in tree.query(g.buffer(r.clearance)):
                if j <= i:
                    continue
                net2, g2, kind2, obj2 = items[j]
                if net == net2:
                    continue
                d = g.distance(g2)
                if d >= r.clearance - 1e-4:
                    continue
                c = g.centroid
                if kind == "pad" and kind2 == "pad":
                    # footprint-inherent spacing (fine-pitch parts) is a warning unless pads touch
                    sev = "error" if d < 1e-6 else "warning"
                    if obj.ref == obj2.ref and sev == "warning" and d >= 0.09:
                        continue
                    V.append(Violation("pad-clearance", f"{layer}: {obj.ref}.{obj.number} to {obj2.ref}.{obj2.number} {d:.3f} mm", (c.x, c.y), sev))
                else:
                    kindname = "short" if d < 1e-6 else "clearance"
                    V.append(Violation(kindname, f"{layer}: {net} vs {net2} ({kind}/{kind2}) {d:.3f} mm < {r.clearance} mm", (c.x, c.y)))
    rep.stats["copper_items"] = n_items

    # --- widths, vias, drills ----------------------------------------------------------------------
    for t in board.tracks:
        if t.width < r.min_track - 1e-6:
            V.append(Violation("min-width", f"track {t.net} width {t.width} < {r.min_track}", (t.x1, t.y1)))
    for v in board.vias:
        if (v.dia - v.drill) / 2 < r.min_annular - 1e-6:
            V.append(Violation("annular-ring", f"via at ({v.x:.2f},{v.y:.2f}) ring {(v.dia - v.drill) / 2:.3f}", (v.x, v.y)))
        if v.drill < r.min_drill - 1e-6:
            V.append(Violation("min-drill", f"via drill {v.drill} < {r.min_drill}", (v.x, v.y)))
    for p in pads:
        if p.thru and p.plated and (min(p.w, p.h) - p.drill) / 2 < r.min_annular - 1e-6:
            V.append(Violation("annular-ring", f"pad {p.ref}.{p.number} ring {(min(p.w, p.h) - p.drill) / 2:.3f}", (p.x, p.y)))
    holes = board.holes()
    if holes:
        hg = [Point(x, y).buffer(d / 2, 8) for x, y, d, _, _ in holes]
        tree = STRtree(hg)
        for i, g in enumerate(hg):
            for j in tree.query(g.buffer(r.hole_to_hole)):
                if j <= i:
                    continue
                d = g.distance(hg[j])
                same_pad = holes[i][4] == "pad" and holes[j][4] == "pad" and abs(holes[i][0] - holes[j][0]) < 1e-6 and abs(holes[i][1] - holes[j][1]) < 1e-6
                if d < r.hole_to_hole - 1e-4 and not same_pad:
                    V.append(Violation("hole-to-hole", f"holes at ({holes[i][0]:.2f},{holes[i][1]:.2f}) and ({holes[j][0]:.2f},{holes[j][1]:.2f}) {d:.3f} mm", (holes[i][0], holes[i][1])))

    # --- board edge --------------------------------------------------------------------------------------
    edge = board.outline.boundary
    for t in board.tracks:
        if t.geom().distance(edge) < r.edge_clearance - 1e-4 or not board.outline.contains(t.geom()):
            V.append(Violation("edge-clearance", f"track {t.net} too close to board edge", (t.x1, t.y1)))
    for v in board.vias:
        if v.geom().distance(edge) < r.edge_clearance - 1e-4 or not board.outline.contains(v.geom()):
            V.append(Violation("edge-clearance", f"via {v.net} too close to board edge", (v.x, v.y)))
    for p in pads:
        if not board.outline.contains(p.geom()):
            V.append(Violation("pad-outside", f"pad {p.ref}.{p.number} outside board", (p.x, p.y)))
        elif p.geom().distance(edge) < 0.2:
            V.append(Violation("edge-clearance", f"pad {p.ref}.{p.number} within 0.2 mm of board edge", (p.x, p.y), "warning"))

    # --- courtyard overlap -----------------------------------------------------------------------------------
    cy = {}
    for ref in board.place:
        x0, y0, x1, y1 = board.courtyard(ref)
        cy[ref] = box(x0, y0, x1, y1)
    refs = list(cy)
    tree = STRtree([cy[k] for k in refs])
    for i, ref in enumerate(refs):
        for j in tree.query(cy[ref]):
            if j <= i:
                continue
            ov = cy[ref].intersection(cy[refs[j]]).area
            if ov > 0.05:
                V.append(Violation("courtyard", f"{ref} overlaps {refs[j]} ({ov:.2f} mm^2)", (cy[ref].centroid.x, cy[ref].centroid.y), "warning"))

    # --- current capacity ---------------------------------------------------------------------------------------
    nets_w: dict[str, float] = {}
    for t in board.tracks:
        nets_w[t.net] = min(nets_w.get(t.net, 99.0), t.width)
    for net, w in nets_w.items():
        need = board.net_current(net)
        if need > 0 and net not in board.pour_nets and net not in board.plane_nets.values():
            cap = current_for_width(w, r.temp_rise_c, r.copper_oz_outer)
            if cap < need * 0.98:
                V.append(Violation("ampacity", f"net {net}: narrowest track {w:g} mm carries ~{cap:.1f} A (< {need:g} A) at {r.temp_rise_c:g} C rise", None, "warning"))

    if check_connectivity:
        _connectivity(board, rep)
    return rep


def _connectivity(board: Board, rep: DRCReport) -> None:
    """Union-find over actual copper: every net must form one connected piece."""
    pads = board.pads()
    zones_by_layer: dict[str, list] = {}
    for z in board.zones:
        zones_by_layer.setdefault(z.layer, []).extend((z.net, g) for g in z.polygons)
    nets: dict[str, list] = {}
    for p in pads:
        if p.net:
            nets.setdefault(p.net, []).append(p)
    bad = 0
    for net, plist in nets.items():
        if len(plist) < 2:
            continue
        nodes: list[tuple[str, object, str]] = []        # (layer, geom, id)
        for layer in board.layers:
            for (inet, geom, kind, obj) in _layer_items(board, layer):
                if inet != net:
                    continue
                if layer in board.plane_nets and kind in ("pad", "via") and net != board.plane_nets[layer]:
                    continue
                key = f"pad:{obj.ref}.{obj.number}" if kind == "pad" else (f"via:{obj.x:.3f},{obj.y:.3f}" if kind == "via" else f"trk:{id(obj)}")
                nodes.append((layer, geom, key))
            for (znet, g) in zones_by_layer.get(layer, []):
                if znet == net:
                    nodes.append((layer, g, f"zone:{layer}:{id(g)}"))
        parent: dict[str, str] = {}

        def find(a):
            while parent.setdefault(a, a) != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        # same-layer touching
        for layer in board.layers:
            ln = [(g, k) for (l, g, k) in nodes if l == layer]
            if not ln:
                continue
            tree = STRtree([g for g, _ in ln])
            for i, (g, k) in enumerate(ln):
                find(k)
                for j in tree.query(g):
                    if j > i and g.intersects(ln[j][0]):
                        union(k, ln[j][1])
        # through elements join all layers
        by_key: dict[str, list[str]] = {}
        for (l, g, k) in nodes:
            if k.startswith("via:") or (k.startswith("pad:") and _is_thru(pads, k)):
                by_key.setdefault(k, []).append(k)
        # (a through pad/via has the same key on every layer, so layers are already merged)
        groups = {}
        for p in plist:
            groups.setdefault(find(f"pad:{p.ref}.{p.number}"), []).append(p)
        if len(groups) > 1:
            bad += 1
            sizes = sorted((len(v) for v in groups.values()), reverse=True)
            first = next(iter(groups.values()))[0]
            rep.violations.append(Violation("unconnected", f"net {net}: {len(groups)} separate copper islands (pads per island {sizes})", (first.x, first.y)))
    rep.stats["nets_checked"] = len(nets)
    rep.stats["nets_disconnected"] = bad


def _is_thru(pads, key: str) -> bool:
    ref, _, num = key[4:].partition(".")
    return any(p.ref == ref and p.number == num and p.thru for p in pads)
