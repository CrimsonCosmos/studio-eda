"""Board state shared by placement, routing, DRC and export."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from ..model import Circuit, Component, oriented_pad, rot_point
from .rules import Rules, layer_names


@dataclass
class Placement:
    x: float
    y: float
    rot: float = 0.0


@dataclass
class PlacedPad:
    ref: str
    number: str
    net: str | None
    x: float
    y: float
    w: float
    h: float
    shape: str
    drill: float
    plated: bool
    paste: bool
    slot: float = 0.0
    slot_y: bool = True

    @property
    def thru(self) -> bool:
        return self.drill > 0

    def geom(self, grow: float = 0.0):
        if self.shape == "circle":
            return Point(self.x, self.y).buffer(max(self.w, self.h) / 2 + grow, 24)
        if self.shape in ("oval", "roundrect"):
            r = min(self.w, self.h) / 2 if self.shape == "oval" else min(self.w, self.h) * 0.25
            core = box(self.x - self.w / 2 + r, self.y - self.h / 2 + r, self.x + self.w / 2 - r, self.y + self.h / 2 - r)
            if core.is_empty:
                core = Point(self.x, self.y)
            return core.buffer(r + grow, 12)
        return box(self.x - self.w / 2 - grow, self.y - self.h / 2 - grow, self.x + self.w / 2 + grow, self.y + self.h / 2 + grow)


@dataclass
class Track:
    layer: str
    net: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float

    def geom(self, grow: float = 0.0):
        return LineString([(self.x1, self.y1), (self.x2, self.y2)]).buffer(self.width / 2 + grow, 8)


@dataclass
class Via:
    x: float
    y: float
    net: str
    dia: float
    drill: float

    def geom(self, grow: float = 0.0):
        return Point(self.x, self.y).buffer(self.dia / 2 + grow, 16)


@dataclass
class Zone:
    layer: str
    net: str
    kind: str = "pour"             # pour | plane
    priority: int = 0
    polygons: list = field(default_factory=list)      # filled result (shapely geometries)


class Board:
    def __init__(self, circuit: Circuit, rules: Rules, width: float, height: float):
        self.circuit = circuit
        self.rules = rules
        self.width = width
        self.height = height
        self.outline: Polygon = box(0, 0, width, height)
        self.place: dict[str, Placement] = {}
        self.tracks: list[Track] = []
        self.vias: list[Via] = []
        self.zones: list[Zone] = []
        self.layers = layer_names(rules.layers)
        self.plane_nets: dict[str, str] = {}          # layer -> net (solid plane layers)
        self.pour_nets: dict[str, list[str]] = {}     # net -> layers carrying a copper pour (layers stay routable)
        self.power_pours: list[Zone] = []             # high-current polygon pours (pre-routed copper)
        self.keepouts: list[tuple[float, float, float, float]] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.stats: dict = {}

    # ---- geometry -------------------------------------------------------------------------
    @property
    def signal_layers(self) -> list[str]:
        return [l for l in self.layers if l not in self.plane_nets]

    def pads(self) -> list[PlacedPad]:
        pn = self.circuit.pin_net_map()
        out: list[PlacedPad] = []
        for ref, comp in self.circuit.components.items():
            pl = self.place.get(ref)
            if pl is None:
                continue
            for p in comp.footprint.pads:
                x, y, w, h = oriented_pad(p, pl.rot)
                net = pn.get(f"{ref}.{p.number}") if p.number else None
                sy = p.slot_y if int(round(pl.rot)) % 180 == 0 else (not p.slot_y)
                out.append(PlacedPad(ref, p.number, net, pl.x + x, pl.y + y, w, h, p.shape, p.drill, p.plated, p.paste, p.slot, sy))
        return out

    def pads_of(self, ref: str) -> list[PlacedPad]:
        return [p for p in self.pads() if p.ref == ref]

    def pad_pos(self, pin: str) -> tuple[float, float]:
        ref, _, num = pin.partition(".")
        comp = self.circuit.components[ref]
        pl = self.place[ref]
        for p in comp.footprint.pads:
            if p.number == num:
                x, y = rot_point(p.x, p.y, pl.rot)
                return pl.x + x, pl.y + y
        raise KeyError(pin)

    def courtyard(self, ref: str) -> tuple[float, float, float, float]:
        comp = self.circuit.components[ref]
        pl = self.place[ref]
        x0, y0, x1, y1 = comp.footprint.oriented_courtyard(pl.rot)
        return pl.x + x0, pl.y + y0, pl.x + x1, pl.y + y1

    def holes(self) -> list[tuple[float, float, float, bool, str]]:
        """(x, y, drill, plated, kind) for pads and vias."""
        res = []
        for p in self.pads():
            if p.drill > 0:
                res.append((p.x, p.y, p.drill, p.plated, "pad"))
        for v in self.vias:
            res.append((v.x, v.y, v.drill, True, "via"))
        return res

    def bbox_used(self) -> tuple[float, float, float, float]:
        xs0, ys0, xs1, ys1 = [], [], [], []
        for ref in self.place:
            a, b, c, d = self.courtyard(ref)
            xs0.append(a); ys0.append(b); xs1.append(c); ys1.append(d)
        return min(xs0), min(ys0), max(xs1), max(ys1)

    def net_current(self, net: str) -> float:
        a = self.circuit.net_attrs.get(net)
        return a.current_a if a else 0.0
