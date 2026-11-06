"""Design rules, fab profiles, stackups and IPC-2221 current-capacity trace sizing."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Rules:
    layers: int = 2
    copper_oz_outer: float = 1.0
    copper_oz_inner: float = 0.5
    thickness_mm: float = 1.6
    track: float = 0.2             # default signal width
    min_track: float = 0.127
    clearance: float = 0.2
    via_dia: float = 0.6
    via_drill: float = 0.3
    min_drill: float = 0.3
    min_annular: float = 0.13
    hole_to_hole: float = 0.35
    edge_clearance: float = 0.4
    mask_expansion: float = 0.05
    mask_bridge: float = 0.1
    silk_width: float = 0.15
    temp_rise_c: float = 20.0
    plane_thermal_gap: float = 0.3
    plane_spoke: float = 0.35
    profile: str = "generic"

    @property
    def min_via_pitch(self) -> float:
        return self.via_dia + self.clearance


FAB_PROFILES = {
    # conservative numbers that standard prototype services accept without surcharge
    "generic": dict(),
    "jlcpcb-2l": dict(layers=2, track=0.2, min_track=0.127, clearance=0.2, via_dia=0.6, via_drill=0.3, min_annular=0.13, edge_clearance=0.4),
    "jlcpcb-4l": dict(layers=4, track=0.15, min_track=0.1, clearance=0.15, via_dia=0.5, via_drill=0.25, min_drill=0.2, min_annular=0.125, edge_clearance=0.3),
    "jlcpcb-6l": dict(layers=6, track=0.15, min_track=0.09, clearance=0.15, via_dia=0.5, via_drill=0.25, min_drill=0.2, min_annular=0.125, edge_clearance=0.3),
}


def rules_for(layers: int = 2, profile: str | None = None, **over) -> Rules:
    name = profile or {2: "jlcpcb-2l", 4: "jlcpcb-4l", 6: "jlcpcb-6l"}.get(layers, "generic")
    kw = dict(FAB_PROFILES.get(name, {}))
    kw.update(over)
    kw["layers"] = layers
    r = Rules(**kw)
    r.profile = name
    return r


def layer_names(n: int) -> list[str]:
    if n == 2:
        return ["F.Cu", "B.Cu"]
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]


def trace_width_for_current(amps: float, rises_c: float = 20.0, oz: float = 1.0, external: bool = True) -> float:
    """IPC-2221 width (mm) for `amps` at `rises_c` temperature rise."""
    if amps <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    area_mil2 = (amps / (k * rises_c ** 0.44)) ** (1 / 0.725)
    thick_mil = 1.378 * oz
    return area_mil2 / thick_mil * 0.0254


def current_for_width(width_mm: float, rises_c: float = 20.0, oz: float = 1.0, external: bool = True) -> float:
    k = 0.048 if external else 0.024
    area_mil2 = (width_mm / 0.0254) * 1.378 * oz
    return k * rises_c ** 0.44 * area_mil2 ** 0.725


def spacing_for_voltage(volts: float) -> float:
    """IPC-2221 B2 minimum conductor spacing (mm), uncoated external, sea level - conservative steps."""
    table = [(15, 0.1), (30, 0.1), (50, 0.6), (100, 0.6), (150, 0.6), (170, 1.25), (250, 1.25), (300, 1.25), (500, 2.5)]
    for v, s in table:
        if volts <= v:
            return s
    return 2.5 + (volts - 500) * 0.005
