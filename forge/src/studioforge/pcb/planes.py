"""Plane/pour assignment, copper fill (antipads, thermal reliefs, islands) and stitching vias."""
from __future__ import annotations

import math

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from .board import Board, Via, Zone


def _ground_net(board: Board) -> str | None:
    c = board.circuit
    best, score = None, -1
    for name, pins in c.nets.items():
        a = c.net_attrs.get(name)
        if (a and a.role == "ground") or name in ("GND", "PGND"):
            if len(pins) > score:
                best, score = name, len(pins)
    return best


def _power_net(board: Board, exclude: set[str]) -> str | None:
    c = board.circuit
    best, score = None, 0.0
    for name, pins in c.nets.items():
        a = c.net_attrs.get(name)
        if name in exclude or not a or a.role != "power":
            continue
        s = len(pins) + a.current_a * 4
        if len(pins) >= 4 and s > score:
            best, score = name, s
    return best


def assign_planes(board: Board, outer_pour: bool = True, power_plane: bool = True) -> None:
    n = len(board.layers)
    gnd = _ground_net(board)
    board.plane_nets.clear()
    board.pour_nets.clear()
    if gnd is None:
        board.warnings.append("no ground net found; no planes assigned")
        return
    if n == 2:
        board.pour_nets[gnd] = list(board.layers)
        return
    board.plane_nets[board.layers[1]] = gnd
    pwr = _power_net(board, {gnd}) if power_plane else None
    if pwr:
        board.plane_nets[board.layers[-2] if n > 4 else board.layers[2]] = pwr
    if outer_pour:
        board.pour_nets[gnd] = [board.layers[0], board.layers[-1]]


def _layer_items(board: Board, layer: str):
    """(net, geometry, kind, obj) for physical copper on `layer` excluding zones."""
    items = []
    for p in board.pads():
        if p.thru or layer == "F.Cu":
            if not p.thru and layer != "F.Cu":
                continue
            items.append((p.net or f"~{p.ref}.{p.number}", p.geom(), "pad", p))
    for t in board.tracks:
        if t.layer == layer:
            items.append((t.net, t.geom(), "track", t))
    for v in board.vias:
        items.append((v.net, v.geom(), "via", v))
    return items


def fill_zones(board: Board, min_neck: float = 0.08) -> None:
    r = board.rules
    clr = r.clearance
    board.zones = []
    zone_defs: list[tuple[str, str, str]] = []
    for layer, net in board.plane_nets.items():
        zone_defs.append((layer, net, "plane"))
    for net, layers in board.pour_nets.items():
        for layer in layers:
            if layer not in board.plane_nets:
                zone_defs.append((layer, net, "pour"))
    inner = board.outline.buffer(-r.edge_clearance)
    for layer, net, kind in zone_defs:
        obstacles = []
        same = []
        for (inet, geom, ikind, obj) in _layer_items(board, layer):
            if inet == net:
                same.append((geom, ikind, obj))
                continue
            if ikind == "pad" and not obj.thru and layer != "F.Cu":
                continue
            obstacles.append(geom.buffer(clr + 0.01, 16))
        for pz in board.power_pours:
            if pz.layer == layer and pz.net != net:
                for g in pz.polygons:
                    obstacles.append(g.buffer(clr + 0.01, 16))
        # unplated holes
        for p in board.pads():
            if p.thru and not p.plated:
                obstacles.append(Point(p.x, p.y).buffer(p.drill / 2 + clr + 0.01, 16))
        # thermal relief for same-net through-hole pads
        reliefs = []
        for geom, ikind, obj in same:
            if ikind == "pad" and obj.thru:
                ring = geom.buffer(r.plane_thermal_gap + 0.01, 16).difference(geom)
                spokes = []
                for ang in (0, 90, 180, 270):
                    dx, dy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                    spokes.append(LineString([(obj.x, obj.y), (obj.x + dx * (max(obj.w, obj.h) + 2), obj.y + dy * (max(obj.w, obj.h) + 2))]).buffer(r.plane_spoke / 2, cap_style=2))
                reliefs.append(ring.difference(unary_union(spokes)))
        blocked = unary_union(obstacles + reliefs) if (obstacles or reliefs) else None
        fill = inner if blocked is None else inner.difference(blocked)
        fill = fill.buffer(-min_neck / 2, 6).buffer(min_neck / 2, 6)          # drop hair-thin necks
        polys = [g for g in (fill.geoms if hasattr(fill, "geoms") else [fill]) if isinstance(g, Polygon) and not g.is_empty and g.area > 0.5]
        # keep only islands touching same-net copper
        anchors = unary_union([g for g, _, _ in same]) if same else None
        kept = []
        for g in polys:
            if kind == "plane" or (anchors is not None and g.intersects(anchors)):
                kept.append(g)
        board.zones.append(Zone(layer, net, kind, 0, kept))
    board.zones.extend(board.power_pours)


def add_stitching(board: Board, spacing: float = 6.0) -> int:
    """Via stitching between pours of a net so every layer's pour is tied together."""
    added = 0
    r = board.rules
    for net, layers in board.pour_nets.items():
        zones = {z.layer: unary_union(z.polygons) for z in board.zones if z.net == net and z.kind == "pour" and z.polygons}
        zl = [l for l in layers if l in zones]
        if len(zl) < 2:
            continue
        common = zones[zl[0]]
        for l in zl[1:]:
            common = common.intersection(zones[l])
        # other-net copper on all layers must stay clear of the new via
        others = []
        for layer in board.layers:
            for (inet, geom, ikind, obj) in _layer_items(board, layer):
                if inet != net or ikind in ("pad", "via"):        # never drop a via into any pad/hole, even our own net
                    others.append(geom.buffer(r.hole_to_hole * 0.6) if ikind in ("pad", "via") else geom)
        for pz in board.power_pours:
            others.extend(pz.polygons)
        occupied = unary_union(others).buffer(r.clearance + r.via_dia / 2 + 0.08) if others else None
        x = spacing / 2
        while x < board.width:
            y = spacing / 2
            while y < board.height:
                pt = Point(x, y)
                disk = pt.buffer(r.via_dia / 2 + 0.2)
                if common.contains(disk) and (occupied is None or not occupied.intersects(pt)):
                    board.vias.append(Via(round(x, 3), round(y, 3), net, r.via_dia, r.via_drill))
                    added += 1
                y += spacing
            x += spacing
    return added
