"""Deterministic generic parts (resistors, capacitors, inductors, LEDs, headers...).

Passives are synthesised on demand from (value, package, rating) - the "large parts
list" for commodity components is therefore unbounded without storing millions of
rows. Prices are order-of-magnitude estimates flagged `estimated`; the purchase step
refreshes them from live suppliers when possible.
"""
from __future__ import annotations

import math

from ..model import PartDef, PinDef, fmt_si
from .suppliers import search_offers

E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40, 1.43, 1.47, 1.50, 1.54,
       1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43,
       2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83,
       3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90, 6.04,
       6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53,
       9.76]


def nearest_e(value: float, series: list[float] = E96) -> float:
    if value <= 0:
        raise ValueError("value must be positive")
    dec = math.floor(math.log10(value))
    best, err = None, 1e9
    for d in (dec - 1, dec, dec + 1):
        for m in series:
            v = m * 10 ** d
            e = abs(math.log(v / value))
            if e < err:
                best, err = v, e
    return best


_P2 = (PinDef("1", "1"), PinDef("2", "2"))
_RES_POWER = {"0402": 0.063, "0603": 0.1, "0805": 0.125, "1206": 0.25, "1210": 0.5, "2512": 1.0}


def _round_sig(v: float) -> float:
    return float(f"{v:.4g}")


def resistor(ohms: float, package: str = "0402", tol: float = 1.0, power_w: float | None = None) -> PartDef:
    ohms = _round_sig(ohms)
    pw = power_w or _RES_POWER[package]
    val = fmt_si(ohms, "")
    mpn = f"RES-{val}-{package}-{tol:g}%"
    price = {"0402": 0.002, "0603": 0.003, "0805": 0.004, "1206": 0.008, "1210": 0.02, "2512": 0.10}[package]
    if tol < 1:
        price *= 3
    return PartDef(mpn, "generic", f"Resistor {val} ohm {tol:g}% {pw:g}W {package}", "resistor", package, list(_P2),
                   f"R_{package}", {"ohms": ohms, "tol": tol, "power_w": pw}, val, height=0.5,
                   offers=search_offers(f"{val} ohm {package} {tol:g}% resistor", price, breaks=[(100, price * 0.5)]),
                   aliases=[f"R:{val}:{package}"], tags=["passive", "generic"], provenance="generic")


def shunt(milliohms: float, power_w: float = 1.0, package: str = "2512") -> PartDef:
    v = milliohms / 1000.0
    val = f"{milliohms:g}m"
    mpn = f"SHUNT-{val}-{package}-{power_w:g}W"
    return PartDef(mpn, "generic", f"Current-sense resistor {val} ohm {power_w:g}W {package} (4-wire/Kelvin preferred)",
                   "resistor.shunt", package, list(_P2), f"R_{package}",
                   {"ohms": v, "tol": 1.0, "power_w": power_w}, val, height=0.7,
                   offers=search_offers(f"{val} ohm {package} {power_w:g}W current sense resistor", 0.12),
                   tags=["passive", "generic", "sense"], provenance="generic")


def capacitor(farads: float, package: str = "0603", vrated: float = 25.0, dielectric: str = "X7R") -> PartDef:
    farads = _round_sig(farads)
    val = fmt_si(farads, "F")
    mpn = f"CAP-{val}-{package}-{vrated:g}V-{dielectric}"
    base = {"0402": 0.004, "0603": 0.008, "0805": 0.02, "1206": 0.05, "1210": 0.15}[package]
    price = base * (1 + farads / 1e-6 * 0.6) * (1 + vrated / 50)
    return PartDef(mpn, "generic", f"MLCC {val} {vrated:g}V {dielectric} {package}", "capacitor.mlcc", package, list(_P2),
                   f"C_{package}", {"farads": farads, "vrated": vrated, "dielectric": dielectric}, val, height=0.9,
                   offers=search_offers(f"{val} {vrated:g}V {dielectric} {package} capacitor", round(price, 4)),
                   aliases=[f"C:{val}:{package}"], tags=["passive", "generic"], provenance="generic")


def pick_mlcc(farads: float, vrated: float) -> PartDef:
    """Pick a package whose capacitance survives DC-bias derating (rule of thumb)."""
    table = [  # (max farads, max volts) -> package, in increasing size
        ("0402", 1e-6, 16), ("0603", 4.7e-6, 25), ("0603", 1e-6, 50), ("0805", 10e-6, 25),
        ("0805", 2.2e-6, 50), ("1206", 22e-6, 25), ("1206", 4.7e-6, 100), ("1210", 47e-6, 25), ("1210", 10e-6, 100)]
    for pkg, fmax, vmax in table:
        if farads <= fmax and vrated <= vmax:
            return capacitor(farads, pkg, vrated if vrated > 6.3 else 6.3, "X7R" if farads <= 1e-6 or vrated > 25 else "X5R")
    return capacitor(farads, "1210", vrated, "X7R")


def electrolytic(farads: float, vrated: float) -> PartDef:
    """Radial aluminium electrolytic sized by energy (rough, typical catalog dims)."""
    # capacitance*voltage -> can diameter
    cv = farads * vrated
    if cv <= 2.2e-3:
        d = 6.3
    elif cv <= 6.4e-3:
        d = 8.0
    elif cv <= 22e-3:
        d = 10.0
    else:
        d = 12.5 if False else 10.0
    val = fmt_si(farads, "F")
    mpn = f"ECAP-{val}-{vrated:g}V-D{d:g}"
    price = 0.08 + cv * 12
    return PartDef(mpn, "generic", f"Aluminium electrolytic {val} {vrated:g}V radial D{d:g}mm low-ESR", "capacitor.electrolytic",
                   f"radial-D{d:g}", [PinDef("1", "+", "passive"), PinDef("2", "-", "passive")], f"CP-D{d:g}",
                   {"farads": farads, "vrated": vrated, "diameter": d, "ripple_a": 0.9 * (d / 8)}, val, height=d + 4,
                   offers=search_offers(f"{val} {vrated:g}V radial electrolytic low ESR", round(price, 3)),
                   tags=["generic", "bulk"], provenance="generic")


_L_BODY = [("4x4", 2.0), ("6x6", 4.0), ("8x8", 6.0), ("10x10", 10.0), ("12x12", 15.0)]


def inductor(henries: float, isat_min: float) -> PartDef:
    """Shielded SMD power inductor; body chosen from saturation current (typical catalog values)."""
    for body, imax in _L_BODY:
        if imax >= isat_min:
            break
    else:
        body, imax = _L_BODY[-1]
    val = fmt_si(henries, "H")
    mpn = f"IND-{val}-{body}-{imax:g}A"
    size = float(body.split("x")[0])
    price = 0.25 + size * size * 0.012
    dcr = 0.45 / imax / max(1.0, (henries / 1e-6) ** -0.2)
    return PartDef(mpn, "generic", f"Shielded power inductor {val} Isat>={imax:g}A {body}mm", "inductor", f"{body}mm",
                   [PinDef("1", "1"), PinDef("2", "2")], f"IND-{body}",
                   {"henries": henries, "isat": imax, "dcr": round(dcr, 4)}, val, emi="aggressor", heat="warm",
                   height=size * 0.5 + 1, offers=search_offers(f"{val} {imax:g}A shielded power inductor {body}mm", round(price, 2)),
                   tags=["generic", "magnetics"], provenance="generic")


def led(color: str = "green", package: str = "0603") -> PartDef:
    return PartDef(f"LED-{color}-{package}", "generic", f"LED {color} {package}", "led", package,
                   [PinDef("1", "K", "passive"), PinDef("2", "A", "passive")], f"LED_{package}",
                   {"vf": {"red": 2.0, "green": 2.2, "blue": 3.0, "white": 3.0, "yellow": 2.1}.get(color, 2.2), "if_max": 0.02},
                   color, height=0.6, offers=search_offers(f"{color} LED {package}", 0.03), tags=["generic"], provenance="generic")


def ferrite(package: str = "0603", ohms100: int = 600, amps: float = 0.5) -> PartDef:
    return PartDef(f"FB-{ohms100}R-{package}-{amps:g}A", "generic", f"Ferrite bead {ohms100}ohm@100MHz {amps:g}A {package}",
                   "ferrite", package, list(_P2), f"FB_{package}", {"ohms100": ohms100, "imax": amps}, f"{ohms100}R",
                   offers=search_offers(f"ferrite bead {ohms100} ohm 100MHz {package}", 0.02), tags=["generic"], provenance="generic")


def fuse(amps: float, volts: float = 63.0, package: str = "1206") -> PartDef:
    return PartDef(f"FUSE-{amps:g}A-{volts:g}V-{package}", "generic", f"Fast SMD fuse {amps:g}A {volts:g}V {package}",
                   "fuse", package, list(_P2), f"F_{package}", {"imax": amps, "vmax": volts}, f"{amps:g}A", heat="warm",
                   offers=search_offers(f"SMD fuse {amps:g}A {volts:g}V {package}", 0.35), tags=["generic", "protection"], provenance="generic")


def crystal(mhz: float = 8.0, load_pf: float = 12.0) -> PartDef:
    return PartDef(f"XTAL-{mhz:g}MHz-{load_pf:g}pF-3225", "generic", f"Crystal {mhz:g}MHz {load_pf:g}pF 3.2x2.5 +-20ppm",
                   "crystal", "3225", [PinDef("1", "X1"), PinDef("2", "GND", "passive"), PinDef("3", "X2"), PinDef("4", "GND", "passive")],
                   "XTAL-3225", {"mhz": mhz, "load_pf": load_pf}, f"{mhz:g}MHz", emi="victim", height=0.8,
                   offers=search_offers(f"{mhz:g}MHz crystal {load_pf:g}pF 3225", 0.35), tags=["generic"], provenance="generic")


def pin_header(n: int, rows: int = 1, desc: str = "") -> PartDef:
    total = n * rows
    return PartDef(f"HDR-{rows}x{n:02d}-2.54", "generic", desc or f"{rows}x{n} pin header 2.54mm", "connector.header",
                   f"{rows}x{n}", [PinDef(str(i + 1), str(i + 1), "passive") for i in range(total)], f"HDR-{rows}x{n:02d}",
                   {"pitch": 2.54, "imax": 3.0}, f"{rows}x{n}", height=8.5,
                   offers=search_offers(f"{rows}x{n} pin header 2.54mm", round(0.03 * total + 0.05, 2)), tags=["generic", "connector"], provenance="generic")


def jst_xh(n: int) -> PartDef:
    return PartDef(f"JST-XH-{n}", "JST", f"JST XH {n}-pos 2.5mm wire-to-board header (B{n}B-XH-A)", "connector.wire",
                   f"XH-{n}", [PinDef(str(i + 1), str(i + 1), "passive") for i in range(n)], f"JST-XH-{n}",
                   {"pitch": 2.5, "imax": 3.0, "vmax": 250}, f"XH{n}", height=7.0, aliases=[f"B{n}B-XH-A"],
                   offers=search_offers(f"B{n}B-XH-A", round(0.08 * n + 0.1, 2)), tags=["connector"], provenance="generic")


def screw_terminal(n: int) -> PartDef:
    return PartDef(f"TB-5.08-{n}", "generic", f"{n}-pos screw terminal 5.08mm 15A", "connector.terminal", f"TB-{n}",
                   [PinDef(str(i + 1), str(i + 1), "passive") for i in range(n)], f"TB-5.08-{n}",
                   {"pitch": 5.08, "imax": 15.0, "vmax": 300}, f"TB{n}", height=10.0,
                   offers=search_offers(f"{n} position screw terminal block 5.08mm PCB", round(0.45 * n, 2)), tags=["connector"], provenance="generic")


def testpoint() -> PartDef:
    return PartDef("TP-1.5", "generic", "Test point pad 1.5mm", "testpoint", "TP", [PinDef("1", "TP", "passive")],
                   "TP-1.5", {}, "TP", offers=[], tags=["generic", "dnp"], provenance="generic")


def mounting_hole(d: float = 3.2) -> PartDef:
    return PartDef(f"MH-M{d:g}", "generic", f"Mounting hole {d:g}mm (M3 screw)", "mounting", f"M{d:g}", [],
                   f"MH-M{d:g}", {}, f"M{d:g}", offers=[], tags=["generic", "mechanical"], provenance="generic")
