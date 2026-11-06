"""Core data model: pads, footprints, parts, components, nets, circuits.

Units: millimetres everywhere. PCB coordinates are y-down (KiCad convention).
Rotation is in degrees, positive = counter-clockwise *as displayed* on a y-down
canvas, which is KiCad's convention, so exported files agree with this model.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

PIN_TYPES = ("power_in", "power_out", "input", "output", "bidir", "passive",
             "nc", "open_collector", "tristate")


def rot_point(x: float, y: float, rot: float) -> tuple[float, float]:
    """Rotate a local point by `rot` degrees CCW-as-displayed (y-down)."""
    r = int(round(rot)) % 360
    if r == 0:
        return x, y
    if r == 90:
        return y, -x
    if r == 180:
        return -x, -y
    if r == 270:
        return -y, x
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    w: float
    h: float
    shape: str = "rect"          # rect | circle | oval | roundrect
    drill: float = 0.0           # 0 => SMD pad
    plated: bool = True
    paste: bool = True
    rratio: float = 0.25
    slot: float = 0.0            # extra drill length (mm) for an oval/slotted hole; 0 = round
    slot_y: bool = True          # slot runs along y (else x) before footprint rotation

    @property
    def thru(self) -> bool:
        return self.drill > 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Footprint:
    name: str
    pads: list[Pad]
    courtyard: tuple[float, float, float, float]       # xmin, ymin, xmax, ymax (local)
    silk: list[tuple] = field(default_factory=list)    # ('line',x1,y1,x2,y2) | ('circle',x,y,r)
    height: float = 1.0
    source: str = "generator:ipc-nominal"
    lib_id: str = ""             # 'Library:Name' in KiCad's footprint libraries ('' = not a KiCad library footprint)

    def to_dict(self) -> dict:
        return {"name": self.name, "pads": [p.to_dict() for p in self.pads],
                "courtyard": list(self.courtyard), "silk": [list(s) for s in self.silk],
                "height": self.height, "source": self.source, "lib_id": self.lib_id}

    @staticmethod
    def from_dict(d: dict) -> "Footprint":
        return Footprint(d["name"], [Pad(**p) for p in d["pads"]], tuple(d["courtyard"]),
                         [tuple(s) for s in d.get("silk", [])], d.get("height", 1.0),
                         d.get("source", "unknown"), d.get("lib_id", ""))

    def pad_numbers(self) -> set[str]:
        return {p.number for p in self.pads if p.number}

    def oriented_courtyard(self, rot: float) -> tuple[float, float, float, float]:
        xs, ys = [], []
        x0, y0, x1, y1 = self.courtyard
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            rx, ry = rot_point(px, py, rot)
            xs.append(rx); ys.append(ry)
        return min(xs), min(ys), max(xs), max(ys)


def oriented_pad(p: Pad, rot: float) -> tuple[float, float, float, float]:
    """(local x, y, w, h) of a pad after rotating its footprint by `rot`."""
    x, y = rot_point(p.x, p.y, rot)
    if int(round(rot)) % 180 == 90:
        return x, y, p.h, p.w
    return x, y, p.w, p.h


# --------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------
@dataclass
class PinDef:
    number: str
    name: str
    etype: str = "passive"
    alt: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["alt"] = list(self.alt)
        return d


@dataclass
class Offer:
    supplier: str                  # digikey | mouser | lcsc | arrow | ...
    sku: str = ""
    url: str = ""
    unit_price: float | None = None    # USD at qty 1 (or first price break)
    price_breaks: list[tuple[int, float]] = field(default_factory=list)
    moq: int = 1
    stock: int | None = None
    estimated: bool = False        # True => indicative number, not a live quote
    checked_at: str = ""

    def price_at(self, qty: int) -> float | None:
        if self.price_breaks:
            best = None
            for q, p in sorted(self.price_breaks):
                if qty >= q:
                    best = p
            if best is not None:
                return best
            return self.price_breaks[0][1]
        return self.unit_price

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price_breaks"] = [list(x) for x in self.price_breaks]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Offer":
        d = dict(d)
        d["price_breaks"] = [tuple(x) for x in d.get("price_breaks", [])]
        return Offer(**d)


@dataclass
class PartDef:
    mpn: str
    manufacturer: str = ""
    description: str = ""
    category: str = ""             # resistor, capacitor, ic.regulator, mosfet, connector, ...
    package: str = ""
    pins: list[PinDef] = field(default_factory=list)
    footprint: Any = ""            # footprint name (str) or Footprint
    params: dict[str, Any] = field(default_factory=dict)   # vmax, imax, rds_on, ...
    value: str = ""
    emi: str = "neutral"           # aggressor | victim | neutral
    heat: str = "cool"             # hot | warm | cool (qualitative)
    height: float = 0.0
    datasheet_url: str = ""
    docs: list[str] = field(default_factory=list)       # cached doc paths (relative to cache root)
    offers: list[Offer] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    provenance: str = "seed"       # seed | generic | researched | imported
    verified: bool = False         # pinout/footprint checked against the datasheet
    notes: str = ""

    def pin(self, ident: str) -> PinDef:
        for p in self.pins:
            if p.number == ident or p.name == ident:
                return p
        raise KeyError(f"{self.mpn}: no pin '{ident}'")

    def pins_named(self, name: str) -> list[PinDef]:
        return [p for p in self.pins if p.name == name]

    def best_offer(self, qty: int = 1) -> Offer | None:
        priced = [o for o in self.offers if o.price_at(qty) is not None]
        if not priced:
            return self.offers[0] if self.offers else None
        return min(priced, key=lambda o: o.price_at(qty))

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "mpn", "manufacturer", "description", "category", "package", "params", "value",
            "emi", "heat", "height", "datasheet_url", "docs", "aliases", "tags",
            "provenance", "verified", "notes")}
        d["pins"] = [p.to_dict() for p in self.pins]
        d["offers"] = [o.to_dict() for o in self.offers]
        d["footprint"] = self.footprint.to_dict() if isinstance(self.footprint, Footprint) else self.footprint
        return d

    @staticmethod
    def from_dict(d: dict) -> "PartDef":
        d = dict(d)
        pins = [PinDef(p["number"], p["name"], p.get("etype", "passive"), tuple(p.get("alt", ())))
                for p in d.pop("pins", [])]
        offers = [Offer.from_dict(o) for o in d.pop("offers", [])]
        fp = d.pop("footprint", "")
        if isinstance(fp, dict):
            fp = Footprint.from_dict(fp)
        return PartDef(pins=pins, offers=offers, footprint=fp, **d)


# --------------------------------------------------------------------------
# Circuit
# --------------------------------------------------------------------------
@dataclass
class Component:
    ref: str
    part: PartDef
    value: str = ""
    power_w: float = 0.0           # steady-state dissipation, for thermal placement
    block: str = ""                # which block created it (grouping / reporting)
    props: dict[str, Any] = field(default_factory=dict)
    fixed: tuple[float, float, float] | None = None   # x, y, rot
    dnp: bool = False

    _fp: Footprint | None = field(default=None, repr=False, compare=False)

    @property
    def footprint(self) -> Footprint:
        if self._fp is None:
            from .footprints import resolve_footprint
            self._fp = resolve_footprint(self.part.footprint)
        return self._fp


@dataclass
class NetAttr:
    klass: str = "default"
    current_a: float = 0.0         # expected max DC/RMS current, drives trace width
    role: str = "signal"           # signal | power | ground | highspeed
    noisy: bool = False            # switching node / fast edge => EMI aggressor net
    sensitive: bool = False        # analog / reference => keep away from aggressors
    voltage: float = 0.0           # nominal DC voltage (creepage / clearance)
    width: float | None = None     # explicit override


@dataclass
class Port:
    """An interface of a block/board. Single-net ports use `net`; bus-like ports carry `signals`
    (canonical signal name -> net name), e.g. a CAN logic port {"tx": "CAN_TX", "rx": "CAN_RX"}."""
    name: str
    net: str
    kind: str                       # power | can_logic | can_bus | uart | i2c | spi | pwm | analog | gpio | motor_phase ...
    direction: str = "bidir"        # in | out | bidir
    voltage: float = 0.0
    current_a: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, str] = field(default_factory=dict)


class Circuit:
    def __init__(self, name: str):
        self.name = name
        self.components: dict[str, Component] = {}
        self.nets: dict[str, list[str]] = {}          # net -> ["U1.3", ...]
        self.net_attrs: dict[str, NetAttr] = {}
        self.ports: dict[str, Port] = {}
        self.constraints: list[Any] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.calcs: dict[str, Any] = {}               # design calculations, for the report
        self._counters: dict[str, int] = {}

    # -- construction ------------------------------------------------------
    def next_ref(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        while f"{prefix}{n}" in self.components:
            n += 1
        self._counters[prefix] = n
        return f"{prefix}{n}"

    def add(self, part: PartDef, ref: str | None = None, prefix: str | None = None,
            value: str | None = None, block: str = "", power_w: float = 0.0, **props) -> Component:
        if ref is None:
            ref = self.next_ref(prefix or ref_prefix_for(part))
        if ref in self.components:
            raise ValueError(f"duplicate ref {ref}")
        comp = Component(ref=ref, part=part, value=value if value is not None else part.value,
                         power_w=power_w, block=block, props=props)
        self.components[ref] = comp
        return comp

    def connect(self, net: str, *pins: str) -> None:
        lst = self.nets.setdefault(net, [])
        for p in pins:
            ref, _, pin = p.partition(".")
            if ref not in self.components:
                raise KeyError(f"connect {net}: unknown component {ref}")
            comp = self.components[ref]
            try:
                comp.part.pin(pin)
            except KeyError:
                raise KeyError(f"connect {net}: {comp.part.mpn} has no pin {pin}") from None
            # a pin may be on only one net
            for other, members in self.nets.items():
                if other != net and p in members:
                    raise ValueError(f"pin {p} already on net {other}, cannot join {net}")
            if p not in lst:
                lst.append(p)

    def attr(self, net: str) -> NetAttr:
        return self.net_attrs.setdefault(net, NetAttr())

    def set_net(self, net: str, **kw) -> NetAttr:
        a = self.attr(net)
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def add_port(self, name: str, net: str, kind: str, direction: str = "bidir",
                 voltage: float = 0.0, current_a: float = 0.0, signals: dict[str, str] | None = None, **meta) -> Port:
        p = Port(name, net, kind, direction, voltage, current_a, meta, dict(signals or {}))
        self.ports[name] = p
        return p

    def constrain(self, c: Any) -> None:
        self.constraints.append(c)

    # -- queries -----------------------------------------------------------
    def net_of(self, pin: str) -> str | None:
        for n, members in self.nets.items():
            if pin in members:
                return n
        return None

    def pin_net_map(self) -> dict[str, str]:
        return {p: n for n, members in self.nets.items() for p in members}

    def rename_net(self, old: str, new: str) -> None:
        if old == new or old not in self.nets:
            return
        members = self.nets.pop(old)
        if new in self.nets:
            self.nets[new].extend(m for m in members if m not in self.nets[new])
        else:
            self.nets[new] = members
        if old in self.net_attrs:
            a = self.net_attrs.pop(old)
            cur = self.net_attrs.get(new)
            if cur is None:
                self.net_attrs[new] = a
            else:                                   # joining nets: keep the demanding attributes
                cur.current_a = max(cur.current_a, a.current_a)
                cur.noisy |= a.noisy
                cur.sensitive |= a.sensitive
                cur.voltage = max(cur.voltage, a.voltage)
                if a.role != "signal":
                    cur.role = a.role
        for p in self.ports.values():
            if p.net == old:
                p.net = new
            for k, v in list(p.signals.items()):
                if v == old:
                    p.signals[k] = new
        for c in self.constraints:
            if hasattr(c, "rename_net"):
                c.rename_net(old, new)

    def merge(self, other: "Circuit", prefix: str = "", net_map: dict[str, str] | None = None,
              block: str | None = None) -> dict[str, str]:
        """Merge `other` into self. Refs get `prefix`; nets in `net_map` are joined to
        existing nets (this is how block ports bind to board-level rails); the rest are
        namespaced with the prefix. Returns the ref map."""
        net_map = net_map or {}
        refmap: dict[str, str] = {}
        for ref, comp in other.components.items():
            new_ref = ref
            if new_ref in self.components or prefix:
                new_ref = self.next_ref(re.sub(r"\d+$", "", ref))
            refmap[ref] = new_ref
            c = copy.copy(comp)
            c.ref = new_ref
            c.block = block if block is not None else (comp.block or other.name)
            c._fp = comp._fp
            self.components[new_ref] = c

        def map_net(n: str) -> str:
            if n in net_map:
                return net_map[n]
            if n in GLOBAL_NETS or n.startswith("/") or n.startswith("+"):
                return n
            return f"{prefix}{n}" if prefix else n

        netmap_full: dict[str, str] = {}
        for n, members in other.nets.items():
            nn = map_net(n)
            netmap_full[n] = nn
            mapped = [f"{refmap[m.partition('.')[0]]}.{m.partition('.')[2]}" for m in members]
            self.nets.setdefault(nn, [])
            for m in mapped:
                if m not in self.nets[nn]:
                    self.nets[nn].append(m)
            if n in other.net_attrs:
                a = other.net_attrs[n]
                cur = self.net_attrs.get(nn)
                if cur is None:
                    self.net_attrs[nn] = copy.copy(a)
                else:   # merge: take the demanding value
                    cur.current_a = max(cur.current_a, a.current_a)
                    cur.noisy = cur.noisy or a.noisy
                    cur.sensitive = cur.sensitive or a.sensitive
                    cur.voltage = max(cur.voltage, a.voltage)
                    if a.role != "signal":
                        cur.role = a.role
        for c in other.constraints:
            nc = copy.deepcopy(c)
            if hasattr(nc, "remap"):
                nc.remap(refmap, netmap_full)
            self.constraints.append(nc)
        for pname, p in other.ports.items():
            q = copy.copy(p)
            q.net = netmap_full.get(p.net, p.net)
            q.signals = {k: netmap_full.get(v, v) for k, v in p.signals.items()}
            q.meta = dict(p.meta)
            self.ports[f"{prefix}{pname}" if prefix else pname] = q
        self.notes.extend(other.notes)
        self.warnings.extend(other.warnings)
        for k, v in other.calcs.items():
            self.calcs[f"{prefix}{k}" if prefix else k] = v
        return refmap

    # -- reports -----------------------------------------------------------
    def stats(self) -> dict[str, int]:
        pads = sum(len(c.footprint.pads) for c in self.components.values())
        return {"components": len(self.components), "nets": len(self.nets), "pads": pads}

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "components": {r: {"mpn": c.part.mpn, "value": c.value, "power_w": c.power_w,
                               "block": c.block, "props": c.props, "dnp": c.dnp,
                               "fixed": list(c.fixed) if c.fixed else None}
                           for r, c in self.components.items()},
            "nets": self.nets,
            "net_attrs": {n: asdict(a) for n, a in self.net_attrs.items()},
            "ports": {n: asdict(p) for n, p in self.ports.items()},
            "notes": self.notes, "warnings": self.warnings, "calcs": self.calcs,
        }


GLOBAL_NETS = {"GND", "AGND", "PGND", "VBAT", "VBUS"}

_PREFIX = {"resistor": "R", "capacitor": "C", "inductor": "L", "led": "D", "diode": "D",
           "mosfet": "Q", "transistor": "Q", "connector": "J", "crystal": "Y", "fuse": "F",
           "tvs": "D", "testpoint": "TP", "mounting": "H", "ferrite": "FB", "switch": "SW",
           "jumper": "JP"}


def ref_prefix_for(part: PartDef) -> str:
    cat = part.category.split(".")[0]
    return _PREFIX.get(cat, "U")


def fmt_si(v: float, unit: str = "") -> str:
    """Format 4700 -> '4.7k', 1e-7 -> '100n' (used for values and BOM)."""
    if v == 0:
        return f"0{unit}"
    for scale, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"),
                       (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= scale * 0.9999:
            s = f"{v / scale:.3g}"
            return f"{s}{suf}{unit}"
    return f"{v:.3g}{unit}"


def parse_si(s: str) -> float:
    """Parse '4.7k', '100n', '2u2', '10uF', '0.1' -> float."""
    s = s.strip().replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    s = re.sub(r"[FHVAWfhvaw]$", "", s) if re.search(r"\d[a-zA-Zµ]?[FHVAW]$", s) else s
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
            "M": 1e6, "G": 1e9, "R": 1.0}
    m = re.fullmatch(r"(\d+)([pnuµmkKMGR])(\d+)", s)           # 4k7 style
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * mult[m.group(2)]
    m = re.fullmatch(r"([\d.]+)\s*([pnuµmkKMG]?)", s)
    if not m:
        raise ValueError(f"cannot parse value '{s}'")
    return float(m.group(1)) * (mult[m.group(2)] if m.group(2) else 1.0)
