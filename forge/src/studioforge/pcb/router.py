"""Multi-layer grid autorouter (A* maze router with rip-up and reroute).

* Nodes are (layer, x, y, heading); 8 headings => 45-degree routing. Bends, vias and
  non-preferred layer direction cost extra, so routes come out straight and tidy.
* Copper widths come from net classes (IPC-2221 current sizing). A net that cannot be routed
  at its target width falls back along a ladder (neck-down near fine-pitch pads) and the
  reduced current capacity is reported as a warning for review.
* Nets assigned to inner planes (GND, a power rail) only need a via stub from each SMD pad.
* Congestion: when a connection fails, a *soft* search may pass through already-routed
  nets at a penalty; those nets are ripped up and requeued (bounded).
* All legality comes from clearance-dilated occupancy rasters, so a legal grid path is DRC-clean
  by construction; `drc.py` re-verifies with exact geometry independently.
"""
from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .board import Board, PlacedPad, Track, Via
from .rules import trace_width_for_current

def _inside_pad(p, x: float, y: float, margin: float = 0.0) -> bool:
    """True if (x, y) lies on the pad's real copper (rounded corners respected), eroded by `margin`."""
    dx, dy = abs(x - p.x), abs(y - p.y)
    hw, hh = p.w / 2 - margin, p.h / 2 - margin
    if hw < 0 or hh < 0:
        return dx <= p.w / 2 and dy <= p.h / 2
    if p.shape == "circle":
        return math.hypot(dx, dy) <= max(p.w, p.h) / 2 - margin
    if p.shape in ("oval", "roundrect"):
        rr = (min(p.w, p.h) / 2 if p.shape == "oval" else 0.25 * min(p.w, p.h)) - margin
        rr = max(rr, 0.0)
        ex, ey = max(dx - (hw - rr), 0.0), max(dy - (hh - rr), 0.0)
        return math.hypot(ex, ey) <= rr + 1e-9 and dx <= hw + 1e-9 and dy <= hh + 1e-9
    return dx <= hw + 1e-9 and dy <= hh + 1e-9


def auto_pitch(board: Board, track: float, clearance: float, max_cells: float = 3.2e6) -> tuple[float, str]:
    """Largest routing-grid pitch that still guarantees a legal track line inside every pad's escape corridor.

    For two neighbouring pads whose centres are `pitch` apart, a track running along one pad's centre line must stay
    (clearance + track/2 + margin) away from the neighbour's edge, leaving a corridor of width
    W = 2 * (pitch - neighbour_half_width - clearance - track/2 - 0.35*g). A grid line is guaranteed inside iff W >= g."""
    seen: dict[str, list[tuple[float, float]]] = {}
    for comp in board.circuit.components.values():
        fp = comp.footprint
        if fp.name in seen:
            continue
        pairs: list[tuple[float, float]] = []                    # (centre pitch across, neighbour across-size)
        pads = fp.pads
        for i, a in enumerate(pads):
            for b in pads[i + 1:]:
                if a.number == b.number or a.drill or b.drill:
                    continue
                dx, dy = abs(a.x - b.x), abs(a.y - b.y)
                if min(dx, dy) < 0.12 and max(dx, dy) < 1.6:       # neighbours in a row
                    across = b.h if dx < dy else b.w
                    pairs.append((max(dx, dy), across))
        seen[fp.name] = pairs
    worst = [(p, a) for pr in seen.values() for (p, a) in pr]
    area = board.width * board.height
    for g in (0.25, 0.2, 0.15, 0.125, 0.1, 0.075, 0.05):
        if area / (g * g) > max_cells:
            continue
        if all(2 * (p - a / 2 - clearance - track / 2 - 0.35 * g) >= g for p, a in worst):
            return g, f"escape corridor ok at {g} mm"
    g = max(0.05, math.sqrt(area / max_cells))
    return g, f"WARNING: fine-pitch pads need a finer grid than {g:.3f} mm allows - some pads may be unreachable"


DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]   # index = heading
SQ2 = math.sqrt(2.0)


@dataclass
class RouteOptions:
    pitch: float | None = None
    via_cost: float = 14.0
    bend_cost: float = 1.2
    wrong_way: float = 1.6
    soft_penalty: float = 25.0
    max_ripups: int = 6
    max_expansions: int = 400_000
    time_limit_s: float = 240.0
    width_classes: tuple = (0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 4.0)
    plane_stub: bool = True
    verbose: bool = False
    per_net_time_s: float = 45.0


@dataclass
class _Net:
    name: str
    nid: int
    pads: list[PlacedPad]
    width_ladder: list[float]
    plane: bool = False


@dataclass
class _Routed:
    pour: bool = False
    segs: list = field(default_factory=list)     # (li, x1, y1, x2, y2, width)
    vias: list = field(default_factory=list)     # (x, y)
    cells: set = field(default_factory=set)      # (li, idx) nodes of the tree
    width: float = 0.2


class GridRouter:
    def __init__(self, board: Board, opt: RouteOptions | None = None):
        self.b = board
        self.o = opt or RouteOptions()
        r = board.rules
        self.layers = board.signal_layers
        self.nl = len(self.layers)
        W, H = board.width, board.height
        area = W * H
        if self.o.pitch:
            g, why = self.o.pitch, "fixed"
        else:
            g, why = auto_pitch(board, r.track, r.clearance)
        self.pitch_note = why
        self.g = g
        self.nx = int(math.ceil(W / g)) + 1
        self.ny = int(math.ceil(H / g)) + 1
        self.sx = self.nx + 2                    # border of blocked cells
        self.sy = self.ny + 2
        self.margin = 0.35 * g
        self.via_r = r.via_dia / 2
        self.clr = r.clearance
        self.deadline = time.time() + self.o.time_limit_s
        self.nets: dict[str, _Net] = {}
        self.netid: dict[str, int] = {}
        self.routed: dict[str, _Routed] = {}
        self.failed: list[str] = []
        self.warnings: list[str] = []
        self.stats = {"expansions": 0, "ripups": 0, "neckdowns": 0}

    # ---- coordinates ----------------------------------------------------------------------
    def to_idx(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x / self.g)) + 1, int(round(y / self.g)) + 1

    def xy(self, idx: int) -> tuple[float, float]:
        iy, ix = divmod(idx, self.sx)
        return (ix - 1) * self.g, (iy - 1) * self.g

    # ---- width classes & arrays ---------------------------------------------------------------
    def _snap_w(self, w: float) -> float:
        for c in self.o.width_classes:
            if c >= w - 1e-9:
                return c
        return self.o.width_classes[-1]

    def _setup(self) -> None:
        b, r = self.b, self.b.rules
        circ = b.circuit
        self.o.width_classes = tuple(sorted({round(r.track, 3)} | {w for w in self.o.width_classes if w > r.track + 1e-9}))
        base = round(r.track, 3)
        pads = b.pads()
        by_net: dict[str, list[PlacedPad]] = {}
        for p in pads:
            if p.net:
                by_net.setdefault(p.net, []).append(p)
        plane_nets = set(b.plane_nets.values()) | set(b.pour_nets)
        nid = 0
        widths = {base}
        for name in sorted(by_net):
            plist = by_net[name]
            if len(plist) < 2 and name not in plane_nets:
                continue
            nid += 1
            a = circ.net_attrs.get(name)
            target = base
            if a is not None:
                if a.width:
                    target = self._snap_w(a.width)
                elif a.current_a > 0:
                    target = self._snap_w(max(base, trace_width_for_current(a.current_a, r.temp_rise_c, r.copper_oz_outer)))
            ladder = [target]
            if any(z.net == name for z in b.power_pours):
                ladder = [base]                    # pour carries the current; remaining pads are small attachments
            elif target > base:
                mid = self._snap_w(target / 2)
                if mid > base:
                    ladder.append(mid)
                ladder.append(base)
            for w in ladder:
                widths.add(w)
            self.nets[name] = _Net(name, nid, plist, ladder, name in plane_nets)
            self.netid[name] = nid
        self.widths = sorted(widths)
        self.wclass = {w: k for k, w in enumerate(self.widths)}
        self.nK = len(self.widths) + 1            # last class = via
        shape = (self.sy, self.sx)
        self.fixed = [[np.zeros(shape, np.int16) for _ in range(self.nl)] for _ in range(self.nK)]
        self.route = [[np.zeros(shape, np.int16) for _ in range(self.nl)] for _ in range(self.nK)]
        self.noviazone = np.zeros(shape, np.uint8)
        for k in range(self.nK):
            for li in range(self.nl):
                for arr in (self.fixed[k][li], self.route[k][li]):
                    arr[0, :] = -1; arr[-1, :] = -1; arr[:, 0] = -1; arr[:, -1] = -1
        self.fx = [[memoryview(a.reshape(-1)) for a in row] for row in self.fixed]
        self.rt = [[memoryview(a.reshape(-1)) for a in row] for row in self.route]
        self.nz = memoryview(self.noviazone.reshape(-1))
        self._stamp_static(pads)

    def _wk(self, k: int) -> float:
        return self.b.rules.via_dia if k == len(self.widths) else self.widths[k]

    # ---- rasterisation --------------------------------------------------------------------------
    def _window(self, x0, y0, x1, y1):
        g = self.g
        ix0 = max(0, int(math.floor(x0 / g)) + 1)
        ix1 = min(self.sx - 1, int(math.ceil(x1 / g)) + 2)
        iy0 = max(0, int(math.floor(y0 / g)) + 1)
        iy1 = min(self.sy - 1, int(math.ceil(y1 / g)) + 2)
        if ix1 <= ix0 or iy1 <= iy0:
            return None
        xs = (np.arange(ix0, ix1) - 1) * g
        ys = (np.arange(iy0, iy1) - 1) * g
        X, Y = np.meshgrid(xs, ys)
        return iy0, iy1, ix0, ix1, X, Y

    def _apply(self, arr, win, mask, nid: int) -> None:
        iy0, iy1, ix0, ix1, _, _ = win
        sub = arr[iy0:iy1, ix0:ix1]
        cur = sub[mask]
        sub[mask] = np.where((cur == 0) | (cur == nid), nid, -1).astype(np.int16)

    def _stamp_pad(self, arrs, p: PlacedPad, r_extra: float, nid: int) -> None:
        reach = max(p.w, p.h) / 2 + r_extra
        win = self._window(p.x - reach, p.y - reach, p.x + reach, p.y + reach)
        if win is None:
            return
        _, _, _, _, X, Y = win
        if p.shape == "circle":
            d = np.hypot(X - p.x, Y - p.y) - max(p.w, p.h) / 2
        else:
            dx = np.maximum(np.abs(X - p.x) - p.w / 2, 0)
            dy = np.maximum(np.abs(Y - p.y) - p.h / 2, 0)
            d = np.hypot(dx, dy)
        mask = d <= r_extra
        for arr in arrs:
            self._apply(arr, win, mask, nid)

    def _stamp_static(self, pads: list[PlacedPad]) -> None:
        b, r = self.b, self.b.rules
        for p in pads:
            nid = self.netid.get(p.net or "", -1) if p.net else -1
            if p.net and p.net not in self.netid:
                nid = -1                       # single-pin net: obstacle owned by nobody
            if p.thru:
                lis = range(self.nl)
            else:
                lis = [0]
            for k in range(self.nK):
                rad = self.clr + self._wk(k) / 2 + self.margin
                self._stamp_pad([self.fixed[k][li] for li in lis], p, rad, nid)
            # keep vias out of every pad (incl. own) - no via-in-pad
            self._stamp_pad_nozone(p)
            if p.thru and not p.plated:
                # NPTH: keep copper away from the drill wall
                for k in range(self.nK):
                    rad = self.clr + self._wk(k) / 2 + self.margin + p.drill / 2
                    self._stamp_pad([self.fixed[k][li] for li in range(self.nl)], p, rad, -1)
        # board edge
        W, H = b.width, b.height
        for k in range(self.nK):
            rad = r.edge_clearance + self._wk(k) / 2
            for li in range(self.nl):
                arr = self.fixed[k][li]
                n = int(math.ceil(rad / self.g))
                arr[: 1 + n, :] = -1; arr[-(1 + n):, :] = -1; arr[:, : 1 + n] = -1; arr[:, -(1 + n):] = -1
        # pre-routed power pours: fixed copper owned by their net
        for z in b.power_pours:
            if z.layer not in self.layers or z.net not in self.netid:
                continue
            li = self.layers.index(z.layer)
            nid = self.netid[z.net]
            for poly in z.polygons:
                for k in range(self.nK):
                    rad = self.clr + self._wk(k) / 2 + self.margin
                    self._stamp_poly(self.fixed[k][li], poly, rad, nid)
        # keepouts
        for (x0, y0, x1, y1) in b.keepouts:
            win = self._window(x0, y0, x1, y1)
            if win:
                _, _, _, _, X, Y = win
                m = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
                for k in range(self.nK):
                    for li in range(self.nl):
                        self._apply(self.fixed[k][li], win, m, -1)

    def _stamp_poly(self, arr, poly, r_extra: float, nid: int) -> None:
        minx, miny, maxx, maxy = poly.bounds
        win = self._window(minx - r_extra, miny - r_extra, maxx + r_extra, maxy + r_extra)
        if win is None:
            return
        _, _, _, _, X, Y = win
        mask = shapely.contains_xy(poly.buffer(r_extra, 6), X, Y)
        self._apply(arr, win, mask, nid)
        iy0, iy1, ix0, ix1, _, _ = win
        # no vias inside pours' own foot-print? vias in a pour are fine; nothing to do
        return

    def _pour_sources(self, z, net: _Net, k: int) -> list[tuple[int, int]]:
        li = self.layers.index(z.layer)
        out = []
        fxm, rtm = self.fx[k][li], self.rt[k][li]
        for poly in z.polygons:
            minx, miny, maxx, maxy = poly.bounds
            win = self._window(minx, miny, maxx, maxy)
            if win is None:
                continue
            iy0, iy1, ix0, ix1, X, Y = win
            inside = shapely.contains_xy(poly, X, Y)
            er = inside.copy()
            er[1:, :] &= inside[:-1, :]; er[:-1, :] &= inside[1:, :]; er[:, 1:] &= inside[:, :-1]; er[:, :-1] &= inside[:, 1:]
            er[0, :] = False; er[-1, :] = False; er[:, 0] = False; er[:, -1] = False
            front = inside & ~er
            ys, xs = np.nonzero(front)
            for yy, xx in zip(ys, xs):
                idx = (iy0 + int(yy)) * self.sx + (ix0 + int(xx))
                f, r_ = fxm[idx], rtm[idx]
                if (f == 0 or f == net.nid) and (r_ == 0 or r_ == net.nid):
                    out.append((li, idx))
        return out

    def _stamp_pad_nozone(self, p: PlacedPad) -> None:
        rad = self.via_r + 0.12
        reach = max(p.w, p.h) / 2 + rad
        win = self._window(p.x - reach, p.y - reach, p.x + reach, p.y + reach)
        if win is None:
            return
        iy0, iy1, ix0, ix1, X, Y = win
        dx = np.maximum(np.abs(X - p.x) - p.w / 2, 0)
        dy = np.maximum(np.abs(Y - p.y) - p.h / 2, 0)
        mask = np.hypot(dx, dy) <= rad
        self.noviazone[iy0:iy1, ix0:ix1][mask] = 1

    def _stamp_seg(self, li: int, x1, y1, x2, y2, width: float, nid: int) -> None:
        for k in range(self.nK):
            rad = self.clr + width / 2 + self._wk(k) / 2 + self.margin
            win = self._window(min(x1, x2) - rad, min(y1, y2) - rad, max(x1, x2) + rad, max(y1, y2) + rad)
            if win is None:
                continue
            _, _, _, _, X, Y = win
            vx, vy = x2 - x1, y2 - y1
            L2 = vx * vx + vy * vy
            if L2 < 1e-12:
                d = np.hypot(X - x1, Y - y1)
            else:
                t = np.clip(((X - x1) * vx + (Y - y1) * vy) / L2, 0, 1)
                d = np.hypot(X - (x1 + t * vx), Y - (y1 + t * vy))
            self._apply(self.route[k][li], win, d <= rad, nid)

    def _stamp_via(self, x, y, nid: int) -> None:
        for k in range(self.nK):
            rad = self.clr + self.via_r + self._wk(k) / 2 + self.margin
            win = self._window(x - rad, y - rad, x + rad, y + rad)
            if win is None:
                continue
            _, _, _, _, X, Y = win
            m = np.hypot(X - x, Y - y) <= rad
            for li in range(self.nl):
                self._apply(self.route[k][li], win, m, nid)
        # also keep other vias from stacking
        win = self._window(x - 1.2, y - 1.2, x + 1.2, y + 1.2)
        if win:
            iy0, iy1, ix0, ix1, X, Y = win
            self.noviazone[iy0:iy1, ix0:ix1][np.hypot(X - x, Y - y) <= max(self.via_r * 2 + self.clr * 0.5, self.b.rules.via_drill + self.b.rules.hole_to_hole + 0.06)] = 1

    def _rebuild_route(self) -> None:
        for k in range(self.nK):
            for li in range(self.nl):
                a = self.route[k][li]
                a[1:-1, 1:-1] = 0
        # via exclusion zone needs rebuild for routed vias only; static part is kept separately
        for name, rr in self.routed.items():
            nid = self.netid[name]
            for (li, x1, y1, x2, y2, w) in rr.segs:
                self._stamp_seg(li, x1, y1, x2, y2, w, nid)
            for (x, y) in rr.vias:
                self._stamp_via(x, y, nid)

    # ---- search ----------------------------------------------------------------------------------
    def _terminals(self, p: PlacedPad, net: _Net, k: int) -> list[tuple[int, int]]:
        """Grid nodes inside pad `p` that are legal starting points for class k."""
        nid = net.nid
        res = []
        ix0, iy0 = self.to_idx(p.x - p.w / 2, p.y - p.h / 2)
        ix1, iy1 = self.to_idx(p.x + p.w / 2, p.y + p.h / 2)
        lis = range(self.nl) if p.thru else [0]
        g = self.g
        for li in lis:
            fxm, rtm = self.fx[k][li], self.rt[k][li]
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    x, y = (ix - 1) * g, (iy - 1) * g
                    if not _inside_pad(p, x, y, 0.04):
                        continue
                    idx = iy * self.sx + ix
                    f, r_ = fxm[idx], rtm[idx]
                    if (f == 0 or f == nid) and (r_ == 0 or r_ == nid):
                        res.append((li, idx))
        if not res:                                         # tiny pad between grid nodes: nearest node
            ix, iy = self.to_idx(p.x, p.y)
            idx = iy * self.sx + ix
            for li in lis:
                f, r_ = self.fx[k][li][idx], self.rt[k][li][idx]
                if (f == 0 or f == nid) and (r_ == 0 or r_ == nid):
                    res.append((li, idx))
        return res

    def _via_ok(self, idx: int, nid: int, soft: bool = False) -> bool:
        if self.nz[idx]:
            return False
        kv = self.nK - 1
        for li in range(self.nl):
            f = self.fx[kv][li][idx]
            if f != 0 and f != nid:
                return False
            r_ = self.rt[kv][li][idx]
            if r_ != 0 and r_ != nid and not soft:
                return False
        return True

    def _search(self, sources: list[tuple[int, int]], targets: list[tuple[int, int]] | None, net: _Net, k: int,
                soft: bool = False, goal_via: bool = False) -> list[tuple[int, int]] | None:
        nid = net.nid
        sx = self.sx
        nl = self.nl
        ncell = self.sx * self.sy
        fx = [self.fx[k][li] for li in range(nl)]
        rt = [self.rt[k][li] for li in range(nl)]
        o = self.o
        step_cost = [1.0, SQ2, 1.0, SQ2, 1.0, SQ2, 1.0, SQ2]
        offs = [dy * sx + dx for dx, dy in DIRS]
        tset = set(targets) if targets else set()
        sset = set(sources)
        if targets:
            xs = [self.xy(i)[0] for _, i in targets]
            ys = [self.xy(i)[1] for _, i in targets]
            bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        g = self.g
        heap: list = []
        gscore: dict[int, float] = {}
        parent: dict[int, int] = {}
        cnt = 0
        D = 9

        def h(idx: int) -> float:
            if not targets:
                return 0.0
            iy, ix = divmod(idx, sx)
            x, y = (ix - 1) * g, (iy - 1) * g
            dx = max(bx0 - x, 0.0, x - bx1) / g
            dy = max(by0 - y, 0.0, y - by1) / g
            return (dx + dy) + (SQ2 - 2) * min(dx, dy)

        for (li, idx) in sources:
            key = (li * ncell + idx) * D + 8
            if key not in gscore:
                gscore[key] = 0.0
                cnt += 1
                heapq.heappush(heap, (h(idx), cnt, key))
        expansions = 0
        horiz_layer = [(li % 2 == 0) for li in range(nl)]
        while heap:
            f, _, key = heapq.heappop(heap)
            gcur = gscore[key]
            node, d = divmod(key, D)
            li, idx = divmod(node, ncell)
            if (targets and (li, idx) in tset) or (goal_via and (li, idx) not in sset and self._via_ok(idx, nid, soft)):
                path = [(li, idx)]
                while key in parent:
                    key = parent[key]
                    path.append(divmod(key // D, ncell))
                path.reverse()
                return path
            expansions += 1
            if expansions > o.max_expansions or (expansions & 0xFFF == 0 and (time.time() > self.deadline or time.time() > getattr(self, "_net_deadline", 1e18))):
                self.stats["expansions"] += expansions
                return None
            fxm, rtm = fx[li], rt[li]
            for nd in range(8):
                if d != 8:
                    diff = abs(nd - d)
                    diff = min(diff, 8 - diff)
                    if diff >= 3:
                        continue
                else:
                    diff = 0
                nidx = idx + offs[nd]
                a = fxm[nidx]
                if a != 0 and a != nid:
                    continue
                pen = 0.0
                b_ = rtm[nidx]
                if b_ != 0 and b_ != nid:
                    if not soft:
                        continue
                    pen = o.soft_penalty
                cost = step_cost[nd]
                if nd % 2 == 0:
                    cost *= 1.0 if ((nd % 4 == 0) == horiz_layer[li]) else o.wrong_way
                else:
                    cost *= 1.1
                ng = gcur + cost + diff * o.bend_cost + pen
                nkey = (li * ncell + nidx) * D + nd
                og = gscore.get(nkey)
                if og is None or ng < og - 1e-9:
                    gscore[nkey] = ng
                    parent[nkey] = key
                    cnt += 1
                    heapq.heappush(heap, (ng + h(nidx), cnt, nkey))
            # layer change
            if nl > 1 and self._via_ok(idx, nid, soft):
                for lj in range(nl):
                    if lj == li:
                        continue
                    nkey = (lj * ncell + idx) * D + 8
                    ng = gcur + o.via_cost
                    og = gscore.get(nkey)
                    if og is None or ng < og - 1e-9:
                        gscore[nkey] = ng
                        parent[nkey] = key
                        cnt += 1
                        heapq.heappush(heap, (ng + h(idx), cnt, nkey))
        self.stats["expansions"] += expansions
        return None

    # ---- commit ------------------------------------------------------------------------------------
    def _path_to_geometry(self, path, width: float):
        """cells -> merged segments + vias (positions in mm)."""
        segs, vias = [], []
        if not path:
            return segs, vias
        pts = [(li, *self.xy(idx)) for li, idx in path]
        i = 0
        start = pts[0]
        prev = pts[0]
        dirv = None
        for cur in pts[1:]:
            if cur[0] != prev[0]:                       # via
                if (prev[1], prev[2]) != (start[1], start[2]) or dirv is not None:
                    segs.append((start[0], start[1], start[2], prev[1], prev[2], width))
                vias.append((prev[1], prev[2]))
                start, dirv = cur, None
                prev = cur
                continue
            dv = (round(cur[1] - prev[1], 6), round(cur[2] - prev[2], 6))
            if dirv is None:
                dirv = dv
            elif dv != dirv:
                segs.append((start[0], start[1], start[2], prev[1], prev[2], width))
                start, dirv = prev, dv
            prev = cur
        if (prev[1], prev[2]) != (start[1], start[2]):
            segs.append((start[0], start[1], start[2], prev[1], prev[2], width))
        return segs, vias

    def _commit(self, net: _Net, path, width: float) -> None:
        rr = self.routed.setdefault(net.name, _Routed())
        rr.width = max(rr.width, width) if rr.segs else width
        segs, vias = self._path_to_geometry(path, width)
        for s in segs:
            rr.segs.append(s)
            self._stamp_seg(s[0], s[1], s[2], s[3], s[4], s[5], net.nid)
        for v in vias:
            rr.vias.append(v)
            self._stamp_via(v[0], v[1], net.nid)
        for node in path:
            rr.cells.add(node)

    def _uncommit(self, name: str) -> None:
        self.routed.pop(name, None)
        self._rebuild_route()

    # ---- per-net routing ----------------------------------------------------------------------------
    def _mst_order(self, pads: list[PlacedPad]) -> list[int]:
        n = len(pads)
        if n <= 1:
            return list(range(n))
        cx = sum(p.x for p in pads) / n
        cy = sum(p.y for p in pads) / n
        first = min(range(n), key=lambda i: (pads[i].x - cx) ** 2 + (pads[i].y - cy) ** 2)
        used = [first]
        order = [first]
        rest = set(range(n)) - {first}
        while rest:
            best = min(rest, key=lambda j: min(abs(pads[j].x - pads[i].x) + abs(pads[j].y - pads[i].y) for i in used))
            used.append(best)
            order.append(best)
            rest.remove(best)
        return order

    def _route_net(self, net: _Net, allow_rip: bool, queue: list[str], ripcount: dict[str, int]) -> bool:
        pads = net.pads
        if len(pads) < 2:
            return True
        order = self._mst_order(pads)
        for width in net.width_ladder:
            k = self.wclass[width]
            ok = self._try_width(net, pads, order, width, k, allow_rip and width == net.width_ladder[-1], queue, ripcount)
            if ok:
                if width < net.width_ladder[0]:
                    self.stats["neckdowns"] += 1
                    cur = self.b.net_current(net.name)
                    self.warnings.append(f"net {net.name}: routed at {width:g} mm instead of {net.width_ladder[0]:g} mm "
                                         f"(needs {cur:g} A) - review/widen or add a pour")
                return True
            if net.name in self.routed:
                self._uncommit(net.name)
        return False

    def _try_width(self, net, pads, order, width, k, allow_rip, queue, ripcount) -> bool:
        tree_cells: list[tuple[int, int]] = []
        pours = [z for z in self.b.power_pours if z.net == net.name and z.layer in self.layers]
        if pours:
            # attach to the MAIN piece only (most power pads, then area); other pieces are just copper
            allp = [(g, z) for z in pours for g in z.polygons]
            main_g, main_z = max(allp, key=lambda gz: (sum(1 for p in pads if p.geom().intersects(gz[0])), gz[0].area))
            from shapely.ops import unary_union as _uu
            src = self._pour_sources(type(main_z)(main_z.layer, main_z.net, main_z.kind, main_z.priority, [main_g]), net, k)
            pg = main_g.buffer(0.01)
            rest = [i for i in order if not pads[i].geom().intersects(pg)]
            self.routed.setdefault(net.name, _Routed(pour=True, width=width))
            if not rest:
                return True
            if not src:
                return False
            tree_cells.extend(src)
            order = [-1] + rest
        else:
            first_terms = self._terminals(pads[order[0]], net, k)
            if not first_terms:
                return False
            tree_cells.extend(first_terms)
            self.routed.setdefault(net.name, _Routed(width=width))
        for j in order[1:]:
            targets = self._terminals(pads[j], net, k)
            if not targets:
                return False
            path = self._search(tree_cells, targets, net, k)
            if path is None and allow_rip:
                spath = self._search(tree_cells, targets, net, k, soft=True)
                if spath is not None:
                    victims = self._victims(net, spath, width)
                    victims = [v for v in victims if ripcount.get(v, 0) < self.o.max_ripups]
                    if victims:
                        for v in victims:
                            ripcount[v] = ripcount.get(v, 0) + 1
                            self.routed.pop(v, None)
                            self.stats["ripups"] += 1
                            if v not in queue:
                                queue.append(v)
                        self._rebuild_route()
                        path = self._search(tree_cells, targets, net, k)
            if path is None:
                return False
            self._commit(net, path, width)
            tree_cells.extend(path)
        return True

    def _victims(self, net: _Net, path, width: float) -> list[str]:
        segs, vias = self._path_to_geometry(path, width)
        geoms = [LineString([(s[1], s[2]), (s[3], s[4])]).buffer(width / 2 + self.clr, 4) for s in segs]
        geoms += [Point(v).buffer(self.via_r + self.clr, 8) for v in vias]
        if not geoms:
            return []
        probe = unary_union(geoms)
        out = []
        for name, rr in self.routed.items():
            if name == net.name:
                continue
            for (li, x1, y1, x2, y2, w) in rr.segs:
                if LineString([(x1, y1), (x2, y2)]).buffer(w / 2).intersects(probe):
                    out.append(name)
                    break
            else:
                for (x, y) in rr.vias:
                    if Point(x, y).buffer(self.via_r).intersects(probe):
                        out.append(name)
                        break
        return out

    # ---- plane stubs ------------------------------------------------------------------------------------
    def _plane_stub(self, net: _Net, pad: PlacedPad) -> bool:
        if pad.thru:
            return True                                  # barrel touches the plane
        for width in (net.width_ladder[0], net.width_ladder[-1]):
            k = self.wclass[width]
            terms = self._terminals(pad, net, k)
            if not terms:
                continue
            path = self._search(terms, None, net, k, goal_via=True)
            if path is None:
                continue
            # the goal cell hosts the via: ensure a layer change at the end
            li, idx = path[-1]
            x, y = self.xy(idx)
            segs, vias = self._path_to_geometry(path, width)
            rr = self.routed.setdefault(net.name, _Routed(width=width))
            for s in segs:
                rr.segs.append(s)
                self._stamp_seg(s[0], s[1], s[2], s[3], s[4], s[5], net.nid)
            rr.vias.append((x, y))
            self._stamp_via(x, y, net.nid)
            rr.cells.update(path)
            return True
        return False

    # ---- driver -----------------------------------------------------------------------------------------------
    def run(self) -> Board:
        b = self.b
        self._setup()
        t0 = time.time()
        # 1) via stubs for plane nets
        plane_fail: set[str] = set()
        for name, net in sorted(self.nets.items()):
            if net.plane:
                for p in net.pads:
                    if not self._plane_stub(net, p):
                        plane_fail.add(name)
                        self.warnings.append(f"plane net {name}: no room for a via near {p.ref}.{p.number}")
        # 2) signal nets: wide/critical first, then short before long
        todo = [n for n in self.nets.values() if not n.plane and len(n.pads) >= 2]

        def hp(n: _Net) -> float:
            xs = [p.x for p in n.pads]; ys = [p.y for p in n.pads]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))
        # wide first; then long multi-pin nets (few alternatives); then short local nets, shortest first
        todo.sort(key=lambda n: (-n.width_ladder[0], not (len(n.pads) > 2 and hp(n) > 25), hp(n)))
        queue = [n.name for n in todo]
        ripcount: dict[str, int] = {}
        guard = 0
        while queue and guard < len(todo) * (self.o.max_ripups + 3):
            guard += 1
            name = queue.pop(0)
            if name in self.routed and self._net_done(name):
                continue
            net = self.nets[name]
            if time.time() > self.deadline:
                self.failed.append(name)
                continue
            t_net = time.time()
            self._net_deadline = t_net + self.o.per_net_time_s
            ok = self._route_net(net, True, queue, ripcount)
            if self.o.verbose:
                print(f"   route {name:<14} pads={len(net.pads):<3} ok={ok} {time.time() - t_net:6.1f}s exp={self.stats['expansions']}", flush=True)
            if not ok:
                self.failed.append(name)
                self.routed.pop(name, None)
        # final pass: retry what failed with a much larger search budget and rip-up
        retry = [n for n in dict.fromkeys(self.failed) if n in self.nets and not self.nets[n].plane and not self._net_done(n)]
        if len(retry) > max(3, int(0.06 * len(self.nets))):
            if self.o.verbose:
                print(f"   {len(retry)} nets failed: skipping the long retry pass (layout too dense; flow will relax it)", flush=True)
            retry = []
        if retry:
            old = self.o.max_expansions
            self.o.max_expansions = old * 5
            self.failed = [f for f in self.failed if f not in retry]
            q2 = list(retry)
            ripc: dict[str, int] = {}
            g2 = 0
            while q2 and g2 < len(retry) * 6 and time.time() < self.deadline:
                g2 += 1
                nm = q2.pop(0)
                if self._net_done(nm):
                    continue
                self._net_deadline = time.time() + self.o.per_net_time_s * 2
                if not self._route_net(self.nets[nm], True, q2, ripc):
                    self.failed.append(nm)
                    self.routed.pop(nm, None)
            self.o.max_expansions = old
        self.failed = sorted((set(self.failed) - {n for n in self.routed if self._net_done(n)}) | plane_fail)
        self._write_board()
        if self.pitch_note.startswith("WARNING"):
            b.warnings.append("router grid: " + self.pitch_note)
        b.stats.update({"route_time_s": round(time.time() - t0, 2), "grid_pitch": self.g, **self.stats,
                        "unrouted": list(self.failed)})
        b.warnings.extend(self.warnings)
        for n in self.failed:
            b.warnings.append(f"UNROUTED net {n}")
        return b

    def _net_done(self, name: str) -> bool:
        net = self.nets[name]
        return name in self.routed and (len(net.pads) < 2 or bool(self.routed[name].segs) or self.routed[name].pour)

    def _write_board(self) -> None:
        b = self.b
        for name, rr in self.routed.items():
            for (li, x1, y1, x2, y2, w) in rr.segs:
                b.tracks.append(Track(self.layers[li], name, round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4), w))
            for (x, y) in rr.vias:
                b.vias.append(Via(round(x, 4), round(y, 4), name, b.rules.via_dia, b.rules.via_drill))
        # connect final path nodes to exact pad centres (stubs inside pads)
        pn = {}
        for p in b.pads():
            if p.net:
                pn.setdefault(p.net, []).append(p)
        for name, rr in self.routed.items():
            for p in pn.get(name, []):
                # find a track end inside the pad
                best = None
                for t in [t for t in b.tracks if t.net == name]:
                    for (x, y) in ((t.x1, t.y1), (t.x2, t.y2)):
                        if abs(x - p.x) <= p.w / 2 + 1e-6 and abs(y - p.y) <= p.h / 2 + 1e-6:
                            best = (t.layer, x, y, t.width)
                            break
                    if best:
                        break
                if best and (abs(best[1] - p.x) > 1e-6 or abs(best[2] - p.y) > 1e-6):
                    # stub toward the pad centre, bounded by the pad extent
                    w = min(best[3], min(p.w, p.h))
                    b.tracks.append(Track(best[0], name, best[1], best[2], round(p.x, 4), round(p.y, 4), w))


def route(board: Board, opt: RouteOptions | None = None) -> Board:
    return GridRouter(board, opt).run()
