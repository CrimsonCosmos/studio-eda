"""Deterministic component placement.

Pipeline: quadratic (connectivity-driven) initial placement -> rank spreading ->
simulated annealing -> overlap legalisation -> auto-sizing. All randomness is seeded
so the same circuit always yields the same board.

Cost terms (mm-equivalents):
  wirelength     half-perimeter of each net's pads (power/ground nets down-weighted: they ride planes)
  overlap        courtyard overlap incl. assembly clearance
  decoupling     bypass-cap pad to IC power pin distance
  loops          bounding box of declared high-di/dt loops (buck hot loop, half-bridge)
  thermal        hot parts repel each other and heat-sensitive parts (1/d kernel on dissipated watts)
  EMI            switching (aggressor) parts repel sensitive (victim) parts
  edge/cluster   connectors to board edge, crystals next to MCU, etc.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np

from ..constraints import Cluster, Decouple, Edge, Loop, Separate
from ..model import Circuit, rot_point, oriented_pad
from .board import Board, Placement
from .rules import Rules

ROTS = (0, 90, 180, 270)


def breakout_margin(npads: int) -> float:
    """Extra courtyard (per side) so fine-pitch / many-pin parts keep escape channels for the router."""
    return 1.0 if npads >= 20 else 0.35 if npads >= 6 else 0.0


@dataclass
class PlaceOptions:
    width: float | None = None
    height: float | None = None
    utilization: float = 0.38
    aspect: float = 1.35
    seed: int = 1
    iterations: int | None = None
    courtyard_margin: float = 0.2
    edge_margin: float = 1.0
    grid: float = 0.05
    w_wire: float = 1.0
    w_overlap: float = 10.0
    w_thermal: float = 3.0
    w_emi: float = 6.0
    allow_rotate: bool = True
    fixed_outline: bool = False       # True => never grow the board


class _Placer:
    def __init__(self, circuit: Circuit, rules: Rules, W: float, H: float, opt: PlaceOptions):
        self.c, self.r, self.W, self.H, self.o = circuit, rules, W, H, opt
        self.refs = list(circuit.components)
        self.idx = {r: i for i, r in enumerate(self.refs)}
        n = self.n = len(self.refs)
        self.rng = random.Random(opt.seed)
        self.np_rng = np.random.default_rng(opt.seed)
        comps = [circuit.components[r] for r in self.refs]
        self.rot = [0] * n
        self.x = [0.0] * n
        self.y = [0.0] * n
        self.fixed = [False] * n
        # per-comp, per-rotation geometry
        self.court = []                      # court[i][rot] = (x0,y0,x1,y1) relative to origin
        self.padoff = []                     # padoff[i][rot] = {padnumber: (dx,dy)}
        for comp in comps:
            fp = comp.footprint
            npads = len(fp.pads)
            # breakout room: fine-pitch / many-pin parts need escape channels around them
            cm = self.o.courtyard_margin / 2 + breakout_margin(npads)
            per_c, per_p = {}, {}
            for rt in ROTS:
                x0, y0, x1, y1 = fp.oriented_courtyard(rt)
                per_c[rt] = (x0 - cm, y0 - cm, x1 + cm, y1 + cm)
                d = {}
                for p in fp.pads:
                    px, py = rot_point(p.x, p.y, rt)
                    d.setdefault(p.number, (px, py))
                per_p[rt] = d
            self.court.append(per_c)
            self.padoff.append(per_p)
        self.area = [(c[0][2] - c[0][0]) * (c[0][3] - c[0][1]) for c in self.court]
        self.power = np.array([max(0.0, c.power_w) for c in comps])
        self.aggr = np.array([1.0 if c.part.emi == "aggressor" else 0.0 for c in comps])
        self.vict = np.array([1.0 if (c.part.emi == "victim" or c.part.category.startswith("capacitor.electrolytic")) else 0.0
                              for c in comps])
        # nets as pin lists
        self.nets: list[tuple[str, list[tuple[int, str]], float]] = []
        self.comp_nets: list[list[int]] = [[] for _ in range(n)]
        for name, pins in circuit.nets.items():
            lst = []
            for p in pins:
                ref, _, num = p.partition(".")
                lst.append((self.idx[ref], num))
            if len(lst) < 2:
                continue
            a = circuit.net_attrs.get(name)
            w = opt.w_wire
            if a is not None:
                if a.role == "ground" or name in ("GND", "AGND", "PGND"):
                    w *= 0.15
                elif a.role == "power":
                    w *= 0.6 if a.current_a < 3 else 1.2
                if a.noisy:
                    w *= 2.5
            ni = len(self.nets)
            self.nets.append((name, lst, w))
            for ci in {c for c, _ in lst}:
                self.comp_nets[ci].append(ni)
        # constraints
        self.dec: list[list[Decouple]] = [[] for _ in range(n)]
        self.loops: list[list[Loop]] = [[] for _ in range(n)]
        self.clusters: list[list[Cluster]] = [[] for _ in range(n)]
        self.edges: list[list[Edge]] = [[] for _ in range(n)]
        self.seps: list[list[Separate]] = [[] for _ in range(n)]
        for k in circuit.constraints:
            try:
                if isinstance(k, Decouple):
                    for r in (k.cap, k.ic):
                        self.dec[self.idx[r]].append(k)
                elif isinstance(k, Loop):
                    for r in k.refs:
                        self.loops[self.idx[r]].append(k)
                elif isinstance(k, Cluster):
                    for r in k.refs:
                        self.clusters[self.idx[r]].append(k)
                elif isinstance(k, Edge):
                    self.edges[self.idx[k.ref]].append(k)
                elif isinstance(k, Separate):
                    for r in k.group_a + k.group_b:
                        self.seps[self.idx[r]].append(k)
            except KeyError:
                continue
        # extents arrays (incl. margin)
        self.ex = np.zeros((4, n))
        self.cx = np.zeros(n)
        self.cy = np.zeros(n)
        for i in range(n):
            self._refresh(i)

    # ---- geometry helpers --------------------------------------------------------------
    def _refresh(self, i: int) -> None:
        x0, y0, x1, y1 = self.court[i][self.rot[i]]
        self.ex[0, i] = self.x[i] + x0
        self.ex[1, i] = self.y[i] + y0
        self.ex[2, i] = self.x[i] + x1
        self.ex[3, i] = self.y[i] + y1
        self.cx[i] = (self.ex[0, i] + self.ex[2, i]) / 2
        self.cy[i] = (self.ex[1, i] + self.ex[3, i]) / 2

    def pad_xy(self, i: int, num: str) -> tuple[float, float]:
        d = self.padoff[i][self.rot[i]].get(num)
        if d is None:
            return self.x[i], self.y[i]
        return self.x[i] + d[0], self.y[i] + d[1]

    def hpwl(self, ni: int) -> float:
        _, pins, w = self.nets[ni]
        xs, ys = [], []
        for ci, num in pins:
            px, py = self.pad_xy(ci, num)
            xs.append(px); ys.append(py)
        return w * ((max(xs) - min(xs)) + (max(ys) - min(ys)))

    # ---- cost ----------------------------------------------------------------------------
    def local_cost(self, i: int, ov_w: float) -> float:
        o = self.o
        c = 0.0
        for ni in self.comp_nets[i]:
            c += self.hpwl(ni)
        # overlap / pairwise terms
        ex = self.ex
        dx = np.minimum(ex[2], ex[2, i]) - np.maximum(ex[0], ex[0, i])
        dy = np.minimum(ex[3], ex[3, i]) - np.maximum(ex[1], ex[1, i])
        ov = np.clip(dx, 0, None) * np.clip(dy, 0, None)
        ov[i] = 0.0
        c += ov_w * float(ov.sum())
        # board bounds
        m = o.edge_margin
        c += 25.0 * (max(0.0, m - ex[0, i]) + max(0.0, m - ex[1, i]) + max(0.0, ex[2, i] - (self.W - m)) + max(0.0, ex[3, i] - (self.H - m)))
        # distance vector
        if self.power[i] > 0 or self.aggr[i] > 0 or self.vict[i] > 0:
            d = np.hypot(self.cx - self.cx[i], self.cy - self.cy[i])
            d[i] = 1e9
            if self.power[i] > 0:
                heat = float((self.power[i] * (self.power + 2.0 * self.vict * 1.0)) @ (1.0 / (d + 4.0))) * 0 + \
                    float(self.power[i] * np.sum((self.power > 0) * self.power / (d + 4.0))) + \
                    float(self.power[i] * np.sum(self.vict / (d + 4.0)) * 1.5)
                # prefer hot parts near the edge (cooling / airflow)
                edge_d = min(self.cx[i], self.cy[i], self.W - self.cx[i], self.H - self.cy[i])
                heat += 0.15 * self.power[i] * edge_d
                c += o.w_thermal * heat
            if self.vict[i] > 0:
                c += o.w_thermal * float(np.sum(self.power / (d + 4.0))) * 1.5
            if self.aggr[i] > 0 or self.vict[i] > 0:
                partner = self.vict if self.aggr[i] > 0 else self.aggr
                c += o.w_emi * float(np.sum(partner / (d + 3.0)))
        for k in self.dec[i]:
            ci, ic = self.idx[k.cap], self.idx[k.ic]
            pn = k.pin.partition(".")[2]
            ax, ay = self.pad_xy(ci, self._cap_power_pad(ci, k))
            bx, by = self.pad_xy(ic, pn)
            dist = math.hypot(ax - bx, ay - by)
            c += k.weight * (dist + 3.0 * max(0.0, dist - k.max_mm))
        for k in self.loops[i]:
            xs, ys = [], []
            for r in k.refs:
                j = self.idx[r]
                xs.append(self.cx[j]); ys.append(self.cy[j])
            c += k.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
        for k in self.clusters[i]:
            xs = [self.cx[self.idx[r]] for r in k.refs]
            ys = [self.cy[self.idx[r]] for r in k.refs]
            span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            c += k.weight * (0.3 * span + 2.0 * max(0.0, span - k.max_mm))
        for k in self.edges[i]:
            x0, y0, x1, y1 = ex[0, i], ex[1, i], ex[2, i], ex[3, i]
            dl, dr, dt, db = x0, self.W - x1, y0, self.H - y1
            dd = {"left": dl, "right": dr, "top": dt, "bottom": db}.get(k.side, min(dl, dr, dt, db))
            c += k.weight * max(0.0, dd - self.o.edge_margin)
        for k in self.seps[i]:
            ga = {self.idx[r] for r in k.group_a}
            gb = {self.idx[r] for r in k.group_b}
            mine, other = (ga, gb) if i in ga else (gb, ga)
            for j in other:
                dist = math.hypot(self.cx[i] - self.cx[j], self.cy[i] - self.cy[j])
                if dist < k.min_mm:
                    c += k.weight * (k.min_mm - dist) ** 1.5
        return c

    def _cap_power_pad(self, ci: int, k: Decouple) -> str:
        comp = self.c.components[self.refs[ci]]
        ic = self.c.components[k.ic]
        net = self.c.net_of(k.pin)
        for p in comp.part.pins:
            if net and self.c.net_of(f"{comp.ref}.{p.number}") == net:
                return p.number
        return comp.part.pins[0].number if comp.part.pins else "1"

    # ---- initial placement -----------------------------------------------------------------
    def initial(self) -> None:
        n, W, H = self.n, self.W, self.H
        m = self.o.edge_margin + 2
        # corner mounting holes + fixed parts
        for i, ref in enumerate(self.refs):
            comp = self.c.components[ref]
            corner = comp.props.get("corner")
            if comp.fixed:
                self.x[i], self.y[i], self.rot[i] = comp.fixed
                self.fixed[i] = True
            elif corner:
                inset = 4.0
                self.x[i] = inset if "l" in corner else W - inset
                self.y[i] = inset if "t" in corner else H - inset
                self.fixed[i] = True
        # Laplacian solve
        L = np.zeros((n, n))
        bx = np.zeros(n)
        by = np.zeros(n)
        for name, pins, w in self.nets:
            members = sorted({ci for ci, _ in pins})
            k = len(members)
            if k < 2:
                continue
            ww = w / (k - 1) if k <= 5 else w / k
            if k <= 5:
                for a in range(k):
                    for b in range(a + 1, k):
                        i, j = members[a], members[b]
                        L[i, i] += ww; L[j, j] += ww; L[i, j] -= ww; L[j, i] -= ww
            else:                                  # star through virtual centroid ~ pull to mean
                for i in members:
                    L[i, i] += ww
                    for j in members:
                        if i != j:
                            L[i, j] -= ww / (k - 1)
        # edge anchors: pull towards chosen edge position
        edge_pos = {}
        eidx = [i for i in range(n) if self.edges[i]]
        for t, i in enumerate(eidx):
            side = self.edges[i][0].side
            frac = (t + 1) / (len(eidx) + 1)
            if side == "left":
                edge_pos[i] = (m, H * frac)
            elif side == "right":
                edge_pos[i] = (W - m, H * frac)
            elif side == "top":
                edge_pos[i] = (W * frac, m)
            elif side == "bottom":
                edge_pos[i] = (W * frac, H - m)
            else:
                perim = 2 * (W + H) * frac
                if perim < W:
                    edge_pos[i] = (perim, m)
                elif perim < W + H:
                    edge_pos[i] = (W - m, perim - W)
                elif perim < 2 * W + H:
                    edge_pos[i] = (W - (perim - W - H), H - m)
                else:
                    edge_pos[i] = (m, H - (perim - 2 * W - H))
        for i in range(n):
            L[i, i] += 0.02                           # weak centre spring keeps system non-singular
            bx[i] += 0.02 * W / 2
            by[i] += 0.02 * H / 2
        for i, (ex_, ey_) in edge_pos.items():
            if not self.fixed[i]:
                L[i, i] += 3.0; bx[i] += 3.0 * ex_; by[i] += 3.0 * ey_
        for i in range(n):
            if self.fixed[i]:
                L[i, :] = 0; L[i, i] = 1.0; bx[i] = self.x[i]; by[i] = self.y[i]
        # fixed columns affect others
        for j in range(n):
            if self.fixed[j]:
                pass
        sol_x = np.linalg.solve(L, bx)
        sol_y = np.linalg.solve(L, by)
        # spread via rank-quantiles (blend keeps relative closeness)
        free = [i for i in range(n) if not self.fixed[i]]
        if free:
            for arr, size in ((sol_x, W), (sol_y, H)):
                vals = np.array([arr[i] for i in free])
                order = np.argsort(np.argsort(vals))
                q = (order + 0.5) / len(free)
                spread = m + q * (size - 2 * m)
                for t, i in enumerate(free):
                    arr[i] = 0.35 * arr[i] + 0.65 * spread[t]
        for i in range(n):
            if not self.fixed[i]:
                self.x[i] = float(min(max(sol_x[i], m), W - m))
                self.y[i] = float(min(max(sol_y[i], m), H - m))
        for i, (ex_, ey_) in edge_pos.items():
            if not self.fixed[i]:
                self.x[i] = 0.5 * self.x[i] + 0.5 * ex_
                self.y[i] = 0.5 * self.y[i] + 0.5 * ey_
        for i in range(n):
            self._refresh(i)

    # ---- annealing ----------------------------------------------------------------------------
    def anneal(self) -> None:
        n = self.n
        free = [i for i in range(n) if not self.fixed[i]]
        if not free:
            return
        iters = self.o.iterations or int(min(120000, max(15000, 700 * len(free))))
        rng = self.rng
        # estimate T0 from random moves
        deltas = []
        for _ in range(120):
            i = rng.choice(free)
            ox, oy = self.x[i], self.y[i]
            before = self.local_cost(i, self.o.w_overlap * 0.3)
            self.x[i] = min(max(ox + rng.gauss(0, 6), 1), self.W - 1)
            self.y[i] = min(max(oy + rng.gauss(0, 6), 1), self.H - 1)
            self._refresh(i)
            deltas.append(abs(self.local_cost(i, self.o.w_overlap * 0.3) - before))
            self.x[i], self.y[i] = ox, oy
            self._refresh(i)
        T0 = max(0.5, float(np.mean(deltas)) * 0.8)
        Tend = T0 * 0.002
        alpha = (Tend / T0) ** (1.0 / iters)
        T = T0
        span = max(self.W, self.H)
        rot_ok = self.o.allow_rotate
        for it in range(iters):
            prog = it / iters
            ov_w = self.o.w_overlap * (0.3 + 1.7 * prog)
            i = free[rng.randrange(len(free))]
            r = rng.random()
            old = (self.x[i], self.y[i], self.rot[i])
            j = -1
            before = self.local_cost(i, ov_w)
            if r < 0.50:
                s = max(0.4, span * 0.25 * (1 - prog) ** 2)
                self.x[i] = min(max(self.x[i] + rng.gauss(0, s), 0.5), self.W - 0.5)
                self.y[i] = min(max(self.y[i] + rng.gauss(0, s), 0.5), self.H - 0.5)
            elif r < 0.65 and rot_ok:
                self.rot[i] = rng.choice([q for q in ROTS if q != self.rot[i]])
            elif r < 0.85 and len(free) > 1:
                j = free[rng.randrange(len(free))]
                if j == i:
                    continue
                old_j = (self.x[j], self.y[j], self.rot[j])
                before += self.local_cost(j, ov_w)
                self.x[i], self.y[i], self.x[j], self.y[j] = self.x[j], self.y[j], self.x[i], self.y[i]
                self._refresh(j)
            else:                                      # hop next to a connected component
                if not self.comp_nets[i]:
                    continue
                ni = rng.choice(self.comp_nets[i])
                others = [ci for ci, _ in self.nets[ni][1] if ci != i]
                if not others:
                    continue
                t = rng.choice(others)
                self.x[i] = min(max(self.x[t] + rng.gauss(0, 3.5), 0.5), self.W - 0.5)
                self.y[i] = min(max(self.y[t] + rng.gauss(0, 3.5), 0.5), self.H - 0.5)
            self._refresh(i)
            after = self.local_cost(i, ov_w) + (self.local_cost(j, ov_w) if j >= 0 else 0.0)
            d = after - before
            if d <= 0 or rng.random() < math.exp(-d / T):
                pass
            else:
                self.x[i], self.y[i], self.rot[i] = old
                self._refresh(i)
                if j >= 0:
                    self.x[j], self.y[j], self.rot[j] = old_j
                    self._refresh(j)
            T *= alpha

    # ---- legalisation -----------------------------------------------------------------------------
    def total_overlap(self) -> float:
        ex = self.ex
        tot = 0.0
        for i in range(self.n):
            dx = np.minimum(ex[2], ex[2, i]) - np.maximum(ex[0], ex[0, i])
            dy = np.minimum(ex[3], ex[3, i]) - np.maximum(ex[1], ex[1, i])
            ov = np.clip(dx, 0, None) * np.clip(dy, 0, None)
            ov[i] = 0
            tot += ov.sum()
        return tot / 2

    def legalize(self, passes: int = 400) -> bool:
        ex = self.ex
        n = self.n
        m = self.o.edge_margin
        for _ in range(passes):
            moved = False
            for i in range(n):
                for j in range(i + 1, n):
                    dx = min(ex[2, i], ex[2, j]) - max(ex[0, i], ex[0, j])
                    dy = min(ex[3, i], ex[3, j]) - max(ex[1, i], ex[1, j])
                    if dx > 1e-4 and dy > 1e-4:
                        moved = True
                        # push along the axis of least penetration, split between movers
                        a, b = (i, j)
                        fa, fb = (not self.fixed[a]), (not self.fixed[b])
                        if not fa and not fb:
                            continue
                        share_a = 0.5 if (fa and fb) else (1.0 if fa else 0.0)
                        share_b = 1.0 - share_a
                        gq = self.o.grid
                        q = lambda v: math.ceil((v + 0.01) / gq) * gq
                        if dx < dy:
                            s = 1 if self.cx[a] >= self.cx[b] else -1
                            if abs(self.cx[a] - self.cx[b]) < 1e-9:
                                s = 1 if a > b else -1
                            self.x[a] += s * q(dx * share_a)
                            self.x[b] -= s * q(dx * share_b) if share_b else 0
                        else:
                            s = 1 if self.cy[a] >= self.cy[b] else -1
                            if abs(self.cy[a] - self.cy[b]) < 1e-9:
                                s = 1 if a > b else -1
                            self.y[a] += s * q(dy * share_a)
                            self.y[b] -= s * q(dy * share_b) if share_b else 0
                        for t in (a, b):
                            if not self.fixed[t]:
                                x0, y0, x1, y1 = self.court[t][self.rot[t]]
                                self.x[t] = min(max(self.x[t], m - x0), self.W - m - x1)
                                self.y[t] = min(max(self.y[t], m - y0), self.H - m - y1)
                            self._refresh(t)
            if not moved:
                return True
        return self.total_overlap() < 1e-3

    def snap(self) -> None:
        g = self.o.grid
        for i in range(self.n):
            if not self.fixed[i]:
                self.x[i] = round(self.x[i] / g) * g
                self.y[i] = round(self.y[i] / g) * g
            self._refresh(i)


def _auto_size(circuit: Circuit, opt: PlaceOptions) -> tuple[float, float]:
    area = 0.0
    big = 0.0
    for comp in circuit.components.values():
        x0, y0, x1, y1 = comp.footprint.courtyard
        m = 0.4 + 2 * breakout_margin(len(comp.footprint.pads))
        area += (x1 - x0 + m) * (y1 - y0 + m)
        big = max(big, x1 - x0 + m, y1 - y0 + m)
    A = area / opt.utilization
    W = max(math.sqrt(A * opt.aspect), big + 2 * opt.edge_margin + 4)
    H = max(A / W, big + 2 * opt.edge_margin + 4)
    return round(W, 1), round(H, 1)


def place(circuit: Circuit, rules: Rules, opt: PlaceOptions | None = None) -> Board:
    opt = opt or PlaceOptions()
    if opt.width and opt.height:
        W, H = opt.width, opt.height
    else:
        W, H = _auto_size(circuit, opt)
    attempts = 0
    while True:
        attempts += 1
        pl = _Placer(circuit, rules, W, H, opt)
        pl.initial()
        pl.anneal()
        pl.snap()
        ok = pl.legalize(800)
        if ok or opt.fixed_outline or attempts >= 5:
            break
        W, H = round(W * 1.12, 1), round(H * 1.12, 1)
    board = Board(circuit, rules, W, H)
    for i, ref in enumerate(pl.refs):
        board.place[ref] = Placement(round(pl.x[i], 3), round(pl.y[i], 3), pl.rot[i])
    if not ok:
        board.warnings.append("placement could not remove all courtyard overlaps; enlarge the board")
    board.stats["place_attempts"] = attempts
    board.stats["place_overlap_mm2"] = round(pl.total_overlap(), 4)
    return board


def shrink_to_fit(board: Board, margin: float = 1.5) -> None:
    """Crop the (auto-sized) outline to the used area and move the origin."""
    x0, y0, x1, y1 = board.bbox_used()
    # mounting holes are inside bbox already; keep their keepout
    dx, dy = margin - x0, margin - y0
    for pl in board.place.values():
        pl.x = round(pl.x + dx, 3)
        pl.y = round(pl.y + dy, 3)
    board.width = round(x1 - x0 + 2 * margin, 2)
    board.height = round(y1 - y0 + 2 * margin, 2)
    from shapely.geometry import box
    board.outline = box(0, 0, board.width, board.height)
