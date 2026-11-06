"""Shared helpers for block builders."""
from __future__ import annotations

import math
import re

from ..model import Circuit, PartDef
from ..parts.generic import E24, E96, nearest_e

E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]

_ROLE_PATTERNS = [
    ("vin", r"^(VIN|IN|PVIN|AVIN|VCC|VDD|V\+|VS)$"),
    ("vout", r"^(VOUT|OUT)$"),
    ("sw", r"^(SW|LX|PH|SWITCH)$"),
    ("boot", r"^(BOOT|VBST|BST|CB|BOOTSTRAP)$"),
    ("fb", r"^(FB|VFB|VSENSE|ADJ)$"),
    ("en", r"^(EN|ENABLE|ON/OFF|~?SHDN)$"),
    ("gnd", r"^(GND|PGND|AGND|VSS|V-|COM)$"),
    ("comp", r"^COMP$"),
    ("rt", r"^(RT|RT/CLK|FSW|RT/SYNC)$"),
    ("pad", r"^(PAD|EP|THERMAL|TAB)$"),
]


def infer_roles(part: PartDef) -> dict[str, list[str]]:
    """role -> [pin numbers] from pin names (works for researched parts that lack explicit roles)."""
    explicit = part.params.get("roles")
    roles: dict[str, list[str]] = {}
    if explicit:
        for role, name in explicit.items():
            roles[role] = [p.number for p in part.pins if p.name == name or p.number == name]
        return roles
    for p in part.pins:
        for role, pat in _ROLE_PATTERNS:
            if re.match(pat, p.name, re.I):
                roles.setdefault(role, []).append(p.number)
                break
    return roles


def e96(x: float) -> float:
    return nearest_e(x, E96)


def e24(x: float) -> float:
    return nearest_e(x, E24)


def e12(x: float) -> float:
    return nearest_e(x, E12)


def std_inductor(l_h: float, series=(1.0, 1.5, 2.2, 3.3, 4.7, 6.8)) -> float:
    """Round *up* to the next E6 inductor value."""
    dec = math.floor(math.log10(l_h))
    for d in (dec, dec + 1):
        for m in series:
            v = m * 10 ** d
            if v >= l_h * 0.999:
                return v
    return l_h


def pull(c: Circuit, net: str, *pins: str) -> None:
    c.connect(net, *pins)


def pins_of(c: Circuit, ref: str, role_pins: list[str]) -> list[str]:
    return [f"{ref}.{n}" for n in role_pins]
