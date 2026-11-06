"""Placement/EMI/thermal constraints that blocks attach to a Circuit.

Blocks know *why* a part must be somewhere (a decoupling cap belongs next to its IC
pin; a buck converter's hot loop must be tiny). The placer simply optimises them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class _Remap:
    refs: tuple[str, ...] = ()

    def remap(self, refmap: dict[str, str], netmap: dict[str, str]) -> None:
        for f in ("cap", "ic", "ref", "a", "b"):
            if hasattr(self, f) and isinstance(getattr(self, f), str):
                setattr(self, f, refmap.get(getattr(self, f), getattr(self, f)))
        for f in ("refs", "group_a", "group_b"):
            if hasattr(self, f):
                setattr(self, f, [refmap.get(r, r) for r in getattr(self, f)])
        if hasattr(self, "pin") and isinstance(getattr(self, "pin"), str) and "." in self.pin:
            r, _, p = self.pin.partition(".")
            self.pin = f"{refmap.get(r, r)}.{p}"
        if hasattr(self, "net") and isinstance(self.net, str):
            self.net = netmap.get(self.net, self.net)

    def rename_net(self, old: str, new: str) -> None:
        if getattr(self, "net", None) == old:
            self.net = new


@dataclass
class Decouple(_Remap):
    """Bypass capacitor `cap` must sit within `max_mm` of power pin `pin` ('U1.24') of `ic`."""
    cap: str
    ic: str
    pin: str
    max_mm: float = 3.0
    weight: float = 6.0


@dataclass
class Loop(_Remap):
    """Components forming a high-di/dt current loop; minimise the area they span."""
    refs: list[str]
    name: str = "loop"
    weight: float = 4.0


@dataclass
class Cluster(_Remap):
    """Keep these components together (e.g. crystal + load caps next to the MCU)."""
    refs: list[str]
    weight: float = 1.5
    max_mm: float = 12.0


@dataclass
class Edge(_Remap):
    """Connector / interface part that should sit at the board edge (side: any|left|right|top|bottom)."""
    ref: str
    side: str = "any"
    weight: float = 3.0


@dataclass
class Separate(_Remap):
    """EMI/thermal keep-apart between two groups."""
    group_a: list[str]
    group_b: list[str]
    min_mm: float = 15.0
    weight: float = 2.0
    reason: str = "emi"
