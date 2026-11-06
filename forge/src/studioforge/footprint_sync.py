"""Official-library footprint verification + sync.

For every footprint name we can map to KiCad's official library, the official geometry is fetched once
(cached under $FORGE_HOME/footprints), compared with our generator, and - when present - USED instead of the
generator. Anything without an official source stays `generator:ipc-nominal` and is listed in reports.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import footprints as fpmod
from .kicad_mod import parse_footprint
from .model import Footprint

BASE = "https://gitlab.com/kicad/libraries/kicad-footprints/-/raw/master"
_CHIP = {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225", "2512": "6332"}


def kicad_path(name: str) -> str | None:
    """Our footprint name -> 'Library.pretty/File' in KiCad's repository (None if no official equivalent)."""
    if (m := re.fullmatch(r"R_(0402|0603|0805|1206|1210|2512)", name)):
        return f"Resistor_SMD.pretty/R_{m.group(1)}_{_CHIP[m.group(1)]}Metric"
    if (m := re.fullmatch(r"C_(0402|0603|0805|1206|1210)", name)):
        return f"Capacitor_SMD.pretty/C_{m.group(1)}_{_CHIP[m.group(1)]}Metric"
    if (m := re.fullmatch(r"LED_(0402|0603|0805|1206)", name)):
        return f"LED_SMD.pretty/LED_{m.group(1)}_{_CHIP[m.group(1)]}Metric"
    if (m := re.fullmatch(r"SOIC-(8|14|16)", name)):
        n = int(m.group(1)); body = {8: "3.9x4.9", 14: "3.9x8.7", 16: "3.9x9.9"}[n]
        return f"Package_SO.pretty/SOIC-{n}_{body}mm_P1.27mm"
    if name == "SOIC-8-EP":
        return "Package_SO.pretty/SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.81mm"
    if name == "TSSOP-14":
        return "Package_SO.pretty/TSSOP-14_4.4x5mm_P0.65mm"
    if name == "SOT-23":
        return "Package_TO_SOT_SMD.pretty/SOT-23"
    if (m := re.fullmatch(r"SOT-23-([56])", name)):
        return f"Package_TO_SOT_SMD.pretty/SOT-23-{m.group(1)}"
    if name == "SOT-223":
        return "Package_TO_SOT_SMD.pretty/SOT-223-3_TabPin2"
    if name in ("TO-252", "DPAK"):
        return "Package_TO_SOT_SMD.pretty/TO-252-2"
    if name == "TO-220-3":
        return "Package_TO_SOT_THT.pretty/TO-220-3_Vertical"
    if name == "LQFP-48":
        return "Package_QFP.pretty/LQFP-48_7x7mm_P0.5mm"
    if (m := re.fullmatch(r"D_(SMA|SMB|SMC)", name)):
        return f"Diode_SMD.pretty/D_{m.group(1)}"
    if name == "XTAL-3225":
        return "Crystal.pretty/Crystal_SMD_3225-4Pin_3.2x2.5mm"
    if (m := re.fullmatch(r"HDR-1x(\d+)", name)):
        return f"Connector_PinHeader_2.54mm.pretty/PinHeader_1x{int(m.group(1)):02d}_P2.54mm_Vertical"
    if (m := re.fullmatch(r"HDR-2x(\d+)", name)):
        return f"Connector_PinHeader_2.54mm.pretty/PinHeader_2x{int(m.group(1)):02d}_P2.54mm_Vertical"
    if (m := re.fullmatch(r"JST-XH-(\d+)", name)):
        n = int(m.group(1))
        return f"Connector_JST.pretty/JST_XH_B{n}B-XH-A_1x{n:02d}_P2.50mm_Vertical"
    if (m := re.fullmatch(r"TB-5\.08-(\d+)", name)):
        n = int(m.group(1))
        return f"TerminalBlock_Phoenix.pretty/TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal"
    if name == "XT60PW":
        return "Connector_AMASS.pretty/AMASS_XT60PW-M_1x02_P7.20mm_Horizontal"
    if (m := re.fullmatch(r"CP-D(5|6\.3|8|10)", name)):
        d = m.group(1); p = {"5": "2.00", "6.3": "2.50", "8": "3.50", "10": "5.00"}[d]
        return f"Capacitor_THT.pretty/CP_Radial_D{float(d):.1f}mm_P{p}mm"
    if (m := re.fullmatch(r"TP-1\.5", name)):
        return "TestPoint.pretty/TestPoint_Pad_D1.5mm"
    return None


def cache_dir(home: Path | None = None) -> Path:
    from .parts.db import default_home
    d = (home or default_home()) / "footprints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(name: str, home: Path | None = None, timeout: int = 20) -> Path | None:
    kp = kicad_path(name)
    if not kp:
        return None
    dest = cache_dir(home) / (re.sub(r"[^A-Za-z0-9._+-]", "_", name) + ".kicad_mod")
    if dest.exists() and dest.stat().st_size > 100:
        return dest
    import requests
    try:
        r = requests.get(f"{BASE}/{kp.replace(',', '%2C')}.kicad_mod", timeout=timeout)
    except Exception:
        return None
    if r.status_code != 200 or not r.text.lstrip().startswith("("):
        return None
    dest.write_text(r.text)
    return dest


def official(name: str, home: Path | None = None) -> Footprint | None:
    """Cached official footprint under our name (pad numbers preserved), or None."""
    p = fetch(name, home)
    if not p:
        return None
    fp = parse_footprint(p.read_text())
    fp.name = name
    fp.source = f"kicad:{kicad_path(name)}"
    return fp


def compare(a: Footprint, b: Footprint, tol: float = 0.02) -> list[str]:
    """Human-readable differences between our generator footprint `a` and the official `b`."""
    diffs: list[str] = []
    def grp(fp):
        d: dict[str, list] = {}
        for p in fp.pads:
            d.setdefault(p.number, []).append(p)
        return d
    ga, gb = grp(a), grp(b)
    if set(ga) != set(gb):
        diffs.append(f"pad numbers differ: only ours {sorted(set(ga) - set(gb))}, only official {sorted(set(gb) - set(ga))}")
    for k in set(ga) & set(gb):
        for pa in ga[k]:
            pb = min(gb[k], key=lambda q: abs(pa.x - q.x) + abs(pa.y - q.y))
            for field, va, vb in (("x", pa.x, pb.x), ("y", pa.y, pb.y), ("w", pa.w, pb.w), ("h", pa.h, pb.h), ("drill", pa.drill, pb.drill)):
                if abs(va - vb) > tol:
                    diffs.append(f"pad '{k}' {field}: ours {va:g} vs official {vb:g}")
                    break
    return diffs


def verify_all(home: Path | None = None) -> list[dict]:
    """Compare every mappable generator footprint with the official one; returns rows for CLI/tests."""
    names = ["R_0402", "R_0603", "R_0805", "R_1206", "R_1210", "R_2512", "C_0402", "C_0603", "C_0805", "C_1206", "C_1210", "LED_0603",
             "SOIC-8", "SOIC-16", "SOIC-8-EP", "TSSOP-14", "SOT-23", "SOT-23-5", "SOT-23-6", "SOT-223", "TO-252", "TO-220-3", "LQFP-48",
             "D_SMA", "D_SMB", "D_SMC", "XTAL-3225", "HDR-1x04", "HDR-1x05", "HDR-1x08", "HDR-2x05", "JST-XH-2", "JST-XH-3", "JST-XH-4",
             "TB-5.08-2", "TB-5.08-3", "XT60PW", "CP-D6.3", "CP-D8", "CP-D10", "TP-1.5"]
    rows = []
    for n in names:
        gen = fpmod._make(n)
        off = official(n, home)
        if off is None:
            rows.append({"name": n, "status": "no-official-source" if not kicad_path(n) else "fetch-failed", "diffs": []})
            continue
        d = compare(gen, off)
        rows.append({"name": n, "status": "match" if not d else "DIFFERS", "diffs": d[:4]})
    return rows


def apply_official(home: Path | None = None) -> int:
    """Register official geometry over generators for everything cached; returns count. (resolve_footprint also does this lazily.)"""
    n = 0
    for p in cache_dir(home).glob("*.kicad_mod"):
        name = p.stem
        fp = official(name, home)
        if fp:
            fpmod.register(fp, name)
            n += 1
    return n


def sync_circuit(circuit, home: Path | None = None) -> dict[str, str]:
    """Fetch (once, cached) and adopt official geometry for every footprint used by `circuit`.
    Returns {footprint name: source} so reports can state exactly what was verified."""
    info: dict[str, str] = {}
    for comp in circuit.components.values():
        spec = comp.part.footprint
        name = spec if isinstance(spec, str) else spec.name
        if name in info:
            continue
        if isinstance(spec, str) and kicad_path(name):
            off = official(name, home)
            if off is not None and comp.footprint.pad_numbers() <= off.pad_numbers() | {""}:
                fpmod.register(off, name)
            for c in circuit.components.values():
                if c.part.footprint == spec:
                    c._fp = None
        src = comp.footprint.source
        info[name] = src
    circuit.calcs["footprint_sources"] = info
    return info
