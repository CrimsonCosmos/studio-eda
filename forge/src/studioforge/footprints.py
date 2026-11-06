"""Deterministic footprint generators.

KiCad's local libraries (/usr/share/kicad/footprints, override KICAD_FOOTPRINT_DIR) are the source of
truth: `resolve_footprint` maps 'Library:Name' and our legacy short names to them. The generators below are
ONLY the offline fallback when no KiCad library is installed (Footprint.source = 'generator:...').
Footprints from datasheets can still be registered with `register()`.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .model import Footprint, Pad

_REGISTRY: dict[str, Footprint] = {}


def register(fp: Footprint, name: str | None = None) -> None:
    _REGISTRY[name or fp.name] = fp


def _marker(x: float, y: float, r: float = 0.25) -> tuple:
    return ("circle", x, y, r)


def _rect_silk(x0, y0, x1, y1) -> list[tuple]:
    return [("line", x0, y0, x1, y0), ("line", x1, y0, x1, y1),
            ("line", x1, y1, x0, y1), ("line", x0, y1, x0, y0)]


# ---- chips ------------------------------------------------------------------
_CHIP = {  # code: (pad_x, pad_w, pad_h, court_hx, court_hy, body_l, body_w)
    "0402": (0.51, 0.54, 0.64, 0.93, 0.47, 1.0, 0.5),
    "0603": (0.825, 0.80, 0.95, 1.48, 0.73, 1.6, 0.8),
    "0805": (0.9125, 1.025, 1.40, 1.68, 0.95, 2.0, 1.25),
    "1206": (1.475, 1.125, 1.75, 2.28, 1.13, 3.2, 1.6),
    "1210": (1.475, 1.125, 2.70, 2.28, 1.80, 3.2, 2.5),
    "2512": (2.9625, 1.225, 3.35, 3.83, 1.93, 6.3, 3.2),
}


def chip(code: str, prefix: str = "R") -> Footprint:
    px, pw, ph, chx, chy, bl, bw = _CHIP[code]
    pads = [Pad("1", -px, 0, pw, ph, "roundrect"), Pad("2", px, 0, pw, ph, "roundrect")]
    sy = chy - 0.05
    silk = [("line", -px + pw / 2 + 0.1, -sy + 0.0, px - pw / 2 - 0.1, -sy),
            ("line", -px + pw / 2 + 0.1, sy, px - pw / 2 - 0.1, sy)] if code != "0402" else []
    return Footprint(f"{prefix}_{code}", pads, (-chx, -chy, chx, chy), silk, 0.6 if code in ("0402", "0603") else 0.8)


# ---- dual-row gull wing ------------------------------------------------------
def dual_row(name: str, n: int, pitch: float, x: float, pw: float, ph: float,
             court_hx: float, court_pad_y: float, height: float, ep: tuple[float, float] | None = None) -> Footprint:
    half = n // 2
    y0 = -(half - 1) * pitch / 2
    pads = []
    for i in range(half):
        pads.append(Pad(str(i + 1), -x, y0 + i * pitch, pw, ph, "roundrect"))
    for i in range(half):
        pads.append(Pad(str(half + i + 1), x, -y0 - i * pitch, pw, ph, "roundrect"))
    if ep:
        pads.append(Pad(str(n + 1), 0, 0, ep[0], ep[1], "rect"))
    cy = -y0 + court_pad_y
    return Footprint(name, pads, (-court_hx, -cy, court_hx, cy),
                     [_marker(-x - pw / 2 - 0.35, y0)], height)


def soic(n: int, ep: bool = False) -> Footprint:
    fp = dual_row(f"SOIC-{n}" + ("-EP" if ep else ""), n, 1.27, 2.475, 1.95, 0.6, 3.7, 0.78, 1.75,
                  (2.41, 3.81) if ep else None)
    return fp


def tssop(n: int, ep: bool = False) -> Footprint:
    return dual_row(f"TSSOP-{n}" + ("-EP" if ep else ""), n, 0.65, 2.875, 1.55, 0.4, 3.9, 0.8, 1.2,
                    (3.0, 3.0) if ep else None)


def sot23(n: int) -> Footprint:
    if n == 3:
        pads = [Pad("1", -0.9375, -0.95, 1.475, 0.6, "roundrect"), Pad("2", -0.9375, 0.95, 1.475, 0.6, "roundrect"),
                Pad("3", 0.9375, 0.0, 1.475, 0.6, "roundrect")]
        return Footprint("SOT-23", pads, (-1.93, -1.7, 1.93, 1.7), [_marker(-1.9, -0.95)], 1.1)
    if n == 5:
        ys = {1: -0.95, 2: 0.0, 3: 0.95, 4: 0.95, 5: -0.95}
    elif n == 6:
        ys = {1: -0.95, 2: 0.0, 3: 0.95, 4: 0.95, 5: 0.0, 6: -0.95}
    else:
        raise ValueError(n)
    pads = [Pad(str(i), -1.1375 if i <= 3 else 1.1375, ys[i], 1.325, 0.6, "roundrect") for i in ys]
    return Footprint(f"SOT-23-{n}", pads, (-2.05, -1.7, 2.05, 1.7), [_marker(-2.0, -0.95)], 1.1)


def sot223() -> Footprint:
    pads = [Pad("1", -3.15, -2.3, 2.0, 1.5, "roundrect"), Pad("2", -3.15, 0, 2.0, 1.5, "roundrect"),
            Pad("3", -3.15, 2.3, 2.0, 1.5, "roundrect"), Pad("2", 3.15, 0, 2.0, 3.8, "roundrect")]
    return Footprint("SOT-223", pads, (-4.4, -3.6, 4.4, 3.6), [_marker(-4.3, -2.3)], 1.8)


def to252() -> Footprint:
    pads = [Pad("1", -5.04, -2.28, 2.2, 1.2, "roundrect"), Pad("3", -5.04, 2.28, 2.2, 1.2, "roundrect"),
            Pad("2", 1.26, 0, 6.4, 5.8, "roundrect")]
    return Footprint("TO-252", pads, (-6.39, -3.5, 4.71, 3.5), [_marker(-6.2, -3.2)], 2.4)


# ---- quad ------------------------------------------------------------------
def quad(name: str, n: int, pitch: float, off: float, plen: float, pw: float, court: float,
         ep: float | None = None, height: float = 1.6) -> Footprint:
    per = n // 4
    pads: list[Pad] = []
    start = -(per - 1) * pitch / 2
    for i in range(per):                               # left, top->bottom
        pads.append(Pad(str(i + 1), -off, start + i * pitch, plen, pw, "roundrect"))
    for i in range(per):                               # bottom, left->right
        pads.append(Pad(str(per + i + 1), start + i * pitch, off, pw, plen, "roundrect"))
    for i in range(per):                               # right, bottom->top
        pads.append(Pad(str(2 * per + i + 1), off, -start - i * pitch, plen, pw, "roundrect"))
    for i in range(per):                               # top, right->left
        pads.append(Pad(str(3 * per + i + 1), -start - i * pitch, -off, pw, plen, "roundrect"))
    if ep:
        pads.append(Pad(str(n + 1), 0, 0, ep, ep, "rect"))
    return Footprint(name, pads, (-court, -court, court, court), [_marker(-off - plen / 2 - 0.4, start)], height)


def lqfp(n: int) -> Footprint:
    body = {32: 7.0, 44: 10.0, 48: 7.0, 64: 10.0, 100: 14.0, 144: 20.0}[n]
    pitch = 0.8 if n in (32, 44) else 0.5
    if n == 144:
        pitch = 0.5
    off = body / 2 + 0.662
    return quad(f"LQFP-{n}", n, pitch, off, 1.475, 0.3 if pitch == 0.5 else 0.55, body / 2 + 1.65)


def qfn(n: int, body: float, ep: float | None, pitch: float = 0.5) -> Footprint:
    off = body / 2 - 0.0625
    court = body / 2 + 0.63
    return quad(f"QFN-{n}-{body:g}x{body:g}" + (f"-EP{ep:g}" if ep else ""), n, pitch, off, 0.875, 0.25, court, ep, 0.9)


# ---- through-hole ---------------------------------------------------------------
def header(rows: int, cols: int, pitch: float = 2.54) -> Footprint:
    pads = []
    d = 1.0
    n = 0
    for c in range(cols):
        for r in range(rows):
            n += 1
            pads.append(Pad(str(n), r * pitch, c * pitch, 1.7, 1.7, "rect" if n == 1 else "oval", drill=d))
    # note: pins run along +x for rows; mirrors KiCad vertical header rotated, kept compact
    w = (rows - 1) * pitch
    h = (cols - 1) * pitch
    return Footprint(f"HDR-{cols}x{rows:02d}", pads, (-1.77, -1.77, w + 1.77, h + 1.77),
                     [_marker(-1.4, -1.4, 0.2)], 8.5)


def header_vertical(n: int, rows: int = 1, pitch: float = 2.54) -> Footprint:
    """KiCad-compatible vertical header: pins run along +y; 2-row numbering is row-wise."""
    pads = []
    if rows == 1:
        for i in range(n):
            pads.append(Pad(str(i + 1), 0, i * pitch, 1.7, 1.7, "rect" if i == 0 else "oval", drill=1.0))
        w, h = 0, (n - 1) * pitch
    else:
        k = 0
        for i in range(n):
            for c in range(2):
                k += 1
                pads.append(Pad(str(k), c * pitch, i * pitch, 1.7, 1.7, "rect" if k == 1 else "oval", drill=1.0))
        w, h = pitch, (n - 1) * pitch
    return Footprint(f"HDR-{rows}x{n:02d}", pads, (-1.77, -1.77, w + 1.77, h + 1.77),
                     [_marker(-1.45, -1.45, 0.2)], 8.5)


def to220() -> Footprint:
    pads = [Pad(str(i + 1), i * 2.54, 0, 1.905, 2.0, "rect" if i == 0 else "oval", drill=1.1) for i in range(3)]
    return Footprint("TO-220-3", pads, (-2.71, -3.4, 7.79, 1.5), [], 4.5)


def jst_xh(n: int) -> Footprint:
    pads = [Pad(str(i + 1), i * 2.5, 0, 1.7, 2.0, "rect" if i == 0 else "oval", drill=1.0) for i in range(n)]
    return Footprint(f"JST-XH-{n}", pads, (-2.95, -2.85, (n - 1) * 2.5 + 2.95, 3.9), [], 7.0)


def screw_terminal(n: int, pitch: float = 5.08) -> Footprint:
    pads = [Pad(str(i + 1), i * pitch, 0, 2.6, 2.6, "rect" if i == 0 else "circle", drill=1.3) for i in range(n)]
    return Footprint(f"TB-{pitch:g}-{n}", pads, (-3.0, -3.8, (n - 1) * pitch + 3.0, 3.8), [], 10.0)


def xt60pw() -> Footprint:
    pads = [Pad("1", -3.6, 0, 4.4, 4.4, "circle", drill=2.0), Pad("2", 3.6, 0, 4.4, 4.4, "circle", drill=2.0)]
    return Footprint("XT60PW", pads, (-6.4, -5.5, 6.4, 5.5), [], 10.0)


def radial_cap(d: float) -> Footprint:
    p = {5.0: 2.0, 6.3: 2.5, 8.0: 3.5, 10.0: 5.0, 12.5: 5.0}[d]
    dr = 0.8 if d <= 6.3 else 1.0
    pads = [Pad("1", 0, 0, 1.6 if d <= 6.3 else 2.0, 1.6 if d <= 6.3 else 2.0, "rect", drill=dr),
            Pad("2", p, 0, 1.6 if d <= 6.3 else 2.0, 1.6 if d <= 6.3 else 2.0, "circle", drill=dr)]
    cx = p / 2
    return Footprint(f"CP-D{d:g}", pads, (cx - d / 2 - 0.25, -d / 2 - 0.25, cx + d / 2 + 0.25, d / 2 + 0.25),
                     [("circle", cx, 0, d / 2)], d + 2)


# ---- misc SMD ----------------------------------------------------------------------
def diode_sm(kind: str) -> Footprint:
    dims = {"SMA": (2.0, 2.5, 1.8, 3.5, 1.75), "SMB": (2.15, 2.5, 2.3, 3.65, 2.25),
            "SMC": (3.4, 2.5, 3.3, 4.9, 3.35)}
    x, w, h, chx, chy = dims[kind]
    return Footprint(f"D_{kind}", [Pad("1", -x, 0, w, h, "roundrect"), Pad("2", x, 0, w, h, "roundrect")],
                     (-chx, -chy, chx, chy), [("line", -x - w / 2 - 0.3, -h / 2, -x - w / 2 - 0.3, h / 2)], 2.4)


def crystal_3225() -> Footprint:
    pads = [Pad("1", -1.1, 0.85, 1.4, 1.2, "roundrect"), Pad("2", 1.1, 0.85, 1.4, 1.2, "roundrect"),
            Pad("3", 1.1, -0.85, 1.4, 1.2, "roundrect"), Pad("4", -1.1, -0.85, 1.4, 1.2, "roundrect")]
    return Footprint("XTAL-3225", pads, (-2.1, -1.75, 2.1, 1.75), [_marker(-2.0, 1.4, 0.2)], 0.8)


_IND = {  # body: (pad_x, pad_w, pad_h, court)
    "4x4": (1.75, 1.7, 3.3, 3.0), "6x6": (2.6, 2.4, 5.0, 4.0), "8x8": (3.4, 3.0, 7.0, 5.0),
    "10x10": (4.5, 3.2, 9.0, 6.5), "12x12": (5.2, 4.0, 11.0, 7.5)}


def inductor(body: str) -> Footprint:
    x, w, h, cy = _IND[body]
    return Footprint(f"IND-{body}", [Pad("1", -x, 0, w, h, "roundrect"), Pad("2", x, 0, w, h, "roundrect")],
                     (-(x + w / 2 + 0.25), -cy, x + w / 2 + 0.25, cy), [], float(body.split("x")[0]) * 0.5 + 1)


def mount_hole(d: float = 3.2) -> Footprint:
    return Footprint(f"MH-M{d:g}", [Pad("", 0, 0, d + 2.0, d + 2.0, "circle", drill=d, plated=False, paste=False)],
                     (-d / 2 - 1.2, -d / 2 - 1.2, d / 2 + 1.2, d / 2 + 1.2), [], 0.0)


def testpoint(d: float = 1.5) -> Footprint:
    return Footprint(f"TP-{d:g}", [Pad("1", 0, 0, d, d, "circle", paste=False)],
                     (-d / 2 - 0.3, -d / 2 - 0.3, d / 2 + 0.3, d / 2 + 0.3), [], 0.1)


# ---- KiCad library access (source of truth) ------------------------------------------
_CHIPM = {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225", "2512": "6332"}
_DIRS_CACHE: dict = {}


def kicad_dir() -> Path | None:
    """Root of KiCad's *.pretty libraries: $KICAD_FOOTPRINT_DIR, $KICAD_FOOTPRINTS, then the usual install paths."""
    cands = [os.environ.get("KICAD_FOOTPRINT_DIR"), os.environ.get("KICAD_FOOTPRINTS"),
             os.environ.get("KICAD7_FOOTPRINT_DIR"), os.environ.get("KICAD8_FOOTPRINT_DIR"),
             "/usr/share/kicad/footprints", "/usr/local/share/kicad/footprints",
             "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"]
    for c in cands:
        if c and Path(c).is_dir() and any(Path(c).glob("*.pretty")):
            return Path(c)
    return None


def kicad_available() -> bool:
    return kicad_dir() is not None


def kicad_mod_path(lib_id: str) -> Path | None:
    """'Library:Name' -> .kicad_mod path (None if absent)."""
    root = kicad_dir()
    if root is None or ":" not in lib_id:
        return None
    lib, name = lib_id.split(":", 1)
    p = root / f"{lib}.pretty" / f"{name}.kicad_mod"
    return p if p.is_file() else None


def _names(lib: str) -> list[str]:
    root = kicad_dir()
    key = (str(root), lib)
    if key not in _DIRS_CACHE:
        d = root / f"{lib}.pretty" if root else None
        _DIRS_CACHE[key] = sorted(f.stem for f in d.glob("*.kicad_mod")) if d and d.is_dir() else []
    return _DIRS_CACHE[key]


def _first(lib: str, prefix: str, exclude=("Handsolder", "HandSolder", "Pad", "Mask", "Drawing")) -> str | None:
    """Shortest footprint name in `lib` starting with `prefix` (plain variants first)."""
    c = [n for n in _names(lib) if n.startswith(prefix) and not any(x in n[len(prefix):] for x in exclude)]
    return f"{lib}:{min(c, key=lambda n: (len(n), n))}" if c else None


def legacy_to_kicad(name: str) -> str | None:
    """Our short footprint names -> 'Library:Name' in KiCad's libraries (None if there is no mapping or it is absent)."""
    cand: str | None = None
    if (m := re.fullmatch(r"(R|C|L|D|LED|FB|F)_(0402|0603|0805|1206|1210|2512)", name)):
        k, code = m.groups()
        lib = {"R": "Resistor_SMD", "C": "Capacitor_SMD", "L": "Inductor_SMD", "FB": "Inductor_SMD", "D": "Diode_SMD",
               "LED": "LED_SMD", "F": "Fuse"}[k]
        base = {"FB": "L", "F": "Fuse"}.get(k, k)
        cand = f"{lib}:{base}_{code}_{_CHIPM[code]}Metric"
    elif (m := re.fullmatch(r"SOIC-(\d+)", name)):
        cand = _first("Package_SO", f"SOIC-{m.group(1)}_")
    elif name == "SOIC-8-EP":
        cand = "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.81mm"
    elif (m := re.fullmatch(r"TSSOP-(\d+)", name)):
        cand = _first("Package_SO", f"TSSOP-{m.group(1)}_")
    elif name == "SOT-23":
        cand = "Package_TO_SOT_SMD:SOT-23"
    elif (m := re.fullmatch(r"SOT-23-([56])", name)):
        cand = f"Package_TO_SOT_SMD:SOT-23-{m.group(1)}"
    elif name == "SOT-223":
        cand = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    elif name in ("TO-252", "DPAK"):
        cand = "Package_TO_SOT_SMD:TO-252-2"
    elif name == "TO-220-3":
        cand = "Package_TO_SOT_THT:TO-220-3_Vertical"
    elif (m := re.fullmatch(r"LQFP-(\d+)", name)):
        cand = _first("Package_QFP", f"LQFP-{m.group(1)}_")
    elif (m := re.fullmatch(r"QFN-(\d+)-(\d+(?:\.\d+)?)x\d+(?:\.\d+)?(?:-EP(\d+(?:\.\d+)?))?(?:-P(\d+(?:\.\d+)?))?", name)):
        n, b, ep, pitch = m.groups()
        pitch = pitch or "0.5"
        cand = (f"Package_DFN_QFN:QFN-{n}-1EP_{b}x{b}mm_P{pitch}mm_EP{ep}x{ep}mm" if ep
                else _first("Package_DFN_QFN", f"QFN-{n}_{b}x{b}mm_P{pitch}mm"))
        if cand and kicad_mod_path(cand) is None and ep:
            cand = _first("Package_DFN_QFN", f"QFN-{n}-1EP_{b}x{b}mm_P{pitch}mm")
    elif (m := re.fullmatch(r"D_(SMA|SMB|SMC)", name)):
        cand = f"Diode_SMD:D_{m.group(1)}"
    elif name == "XTAL-3225":
        cand = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"
    elif (m := re.fullmatch(r"HDR-([12])x(\d+)", name)):
        cand = f"Connector_PinHeader_2.54mm:PinHeader_{m.group(1)}x{int(m.group(2)):02d}_P2.54mm_Vertical"
    elif (m := re.fullmatch(r"JST-XH-(\d+)", name)):
        n = int(m.group(1))
        cand = f"Connector_JST:JST_XH_B{n}B-XH-A_1x{n:02d}_P2.50mm_Vertical"
    elif (m := re.fullmatch(r"TB-5\.08-(\d+)", name)):
        n = int(m.group(1))
        cand = f"TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal"
    elif name == "XT60PW":
        cand = "Connector_AMASS:AMASS_XT60PW-M_1x02_P7.20mm_Horizontal"
    elif (m := re.fullmatch(r"CP-D(\d+(?:\.\d+)?)", name)):
        d = float(m.group(1))
        pitch = {5.0: "2.00", 6.3: "2.50", 8.0: "3.50", 10.0: "5.00", 12.5: "5.00"}.get(d)
        cand = f"Capacitor_THT:CP_Radial_D{d:.1f}mm_P{pitch}mm" if pitch else None
    elif (m := re.fullmatch(r"MH-M(\d+(?:\.\d+)?)", name)):
        d = m.group(1)
        cand = f"MountingHole:MountingHole_{d}mm_M{int(float(d))}" if float(d) in (2.2, 2.7, 3.2, 4.3, 5.3, 6.4) else None
    elif (m := re.fullmatch(r"TP-(\d+(?:\.\d+)?)", name)):
        cand = f"TestPoint:TestPoint_Pad_D{m.group(1)}mm"
    elif (m := re.fullmatch(r"IND-(\d+)x\d+", name)):
        cand = _first("Inductor_SMD", f"L_{m.group(1)}x{m.group(1)}mm_H") if int(m.group(1)) == 12 else None
    return cand if cand and kicad_mod_path(cand) else None


@lru_cache(maxsize=2048)
def _load_kicad(lib_id: str, root: str) -> Footprint | None:
    p = kicad_mod_path(lib_id)
    if p is None:
        return None
    from .kicad_mod import parse_footprint
    fp = parse_footprint(p.read_text())
    fp.name = lib_id.split(":", 1)[1]
    fp.source = f"kicad:{lib_id}"
    fp.lib_id = lib_id
    return fp


def kicad_footprint(lib_id: str) -> Footprint | None:
    """Parse a footprint straight from the KiCad library ('Library:Name'); None if the library/file is missing."""
    root = kicad_dir()
    return _load_kicad(lib_id, str(root)) if root else None


def is_kicad_id(spec: str) -> bool:
    return ":" in spec and not spec.startswith("studioforge:")


# ---- resolver --------------------------------------------------------------------
def resolve_footprint(spec) -> Footprint:
    """Footprint object | 'Library:Name' (KiCad) | legacy short name -> Footprint. KiCad's library wins; our
    generators are only the offline fallback (Footprint.source says which was used)."""
    if isinstance(spec, Footprint):
        return spec
    name = str(spec)
    if name in _REGISTRY:
        return _REGISTRY[name]
    fp: Footprint | None = None
    if is_kicad_id(name):
        lib_id = name[len("studioforge:"):] if name.startswith("studioforge:") else name
        fp = kicad_footprint(lib_id)
        if fp is None:
            raise KeyError(f"KiCad footprint '{name}' not found under {kicad_dir()}")
        _REGISTRY[name] = fp
        return fp
    lib_id = legacy_to_kicad(name)
    if lib_id:
        k = kicad_footprint(lib_id)
        if k is not None:
            fp = Footprint(name, k.pads, k.courtyard, k.silk, k.height, k.source, k.lib_id)
    if fp is None:
        fp = _make(name)
        fp.source = f"generator:{'offline-fallback' if lib_id is None and kicad_available() is False else 'ipc-nominal'}"
    _REGISTRY[name] = fp
    return fp


def search_footprints(query: str = "", pins: int | None = None, lib: str | None = None, limit: int = 20) -> list[dict]:
    """Search KiCad's footprint libraries by keyword(s) (all must occur, case-insensitive, '-'/'_' equivalent)
    and optionally by number of distinct pad numbers. Best match first. Rows: {lib_id, lib, name, pins}."""
    root = kicad_dir()
    if root is None:
        return []
    toks = [t for t in re.split(r"[\s,]+", re.sub(r"[-_]", " ", query.lower())) if t]
    libs = [lib] if lib else sorted(p.name[:-7] for p in root.glob("*.pretty"))
    rows = []
    for lb in libs:
        for n in _names(lb):
            norm = re.sub(r"[-_]", " ", (lb + " " + n).lower())
            if all(t in norm for t in toks):
                rows.append((lb, n))
    pref = ("Package_", "Resistor_SMD", "Capacitor_SMD", "Connector_", "LED_SMD", "Diode_SMD", "Inductor_SMD")
    bad = ("handsolder", "mask", "drawing", "thermalvias", "pad_", "longpads", "wave", "reflow")

    def score(r):
        lb, n = r
        nl = n.lower()
        starts = 0 if (toks and re.sub(r"[-_]", " ", nl).startswith(" ".join(toks))) else 1
        return (starts, sum(b in nl for b in bad if b not in query.lower()), not lb.startswith(pref), len(n), n)
    rows.sort(key=score)
    out = []
    for lb, n in rows:
        npads = _pin_count(lb, n)
        if pins is not None and npads not in (pins, pins + 1):          # +1: exposed pad / tab not in the pin list
            continue
        out.append({"lib_id": f"{lb}:{n}", "lib": lb, "name": n, "pins": npads})
        if len(out) >= limit:
            break
    return out


find_footprints = search_footprints


@lru_cache(maxsize=20000)
def _pin_count(lib: str, name: str) -> int:
    root = kicad_dir()
    try:
        txt = (root / f"{lib}.pretty" / f"{name}.kicad_mod").read_text()
    except OSError:
        return 0
    return len({m for m in re.findall(r'\(pad "([^"]+)"', txt)})


def footprint_for_package(package: str, pins: int | None = None, pin_numbers: set[str] | None = None) -> Footprint | None:
    """Best real KiCad footprint for a datasheet package name ('SOIC-8', 'SOT-23-6', 'QFN-32-5x5', 'TSSOP-16' ...).
    If `pin_numbers` is given the footprint must provide a pad for every one of them."""
    if not kicad_available() or not package:
        return None
    pkg = package.strip()
    cands: list[str] = []
    try:
        if pkg in _REGISTRY or legacy_to_kicad(pkg):
            cands.append(resolve_footprint(pkg).lib_id)
    except KeyError:
        pass
    if is_kicad_id(pkg):
        cands.append(pkg)
    alias = re.sub(r"^(SO|SOP)[-_ ]?(\d+)$", r"SOIC-\2", pkg, flags=re.I)
    for q in dict.fromkeys([pkg, alias, re.sub(r"[-_ ]?(EP|PowerPAD|thermal).*$", "", pkg, flags=re.I)]):
        if q:
            cands += [r["lib_id"] for r in search_footprints(q, pins=pins, limit=8)]
    for lid in cands:
        fp = kicad_footprint(lid) if lid else None
        if fp is None:
            continue
        if pin_numbers is not None and not pin_numbers <= fp.pad_numbers():
            continue
        return fp
    return None


def _make(name: str) -> Footprint:
    m = re.fullmatch(r"(?:R|C|L|D|LED|FB|F|CHIP)_(0402|0603|0805|1206|1210|2512)", name)
    if m:
        fp = chip(m.group(1), name.split("_")[0])
        fp.name = name
        return fp
    if (m := re.fullmatch(r"SOIC-(\d+)(-EP)?", name)):
        return soic(int(m.group(1)), bool(m.group(2)))
    if (m := re.fullmatch(r"TSSOP-(\d+)(-EP)?", name)):
        return tssop(int(m.group(1)), bool(m.group(2)))
    if name == "SOT-23":
        return sot23(3)
    if (m := re.fullmatch(r"SOT-23-([56])", name)):
        return sot23(int(m.group(1)))
    if name == "SOT-223":
        return sot223()
    if name in ("TO-252", "DPAK"):
        return to252()
    if (m := re.fullmatch(r"LQFP-(\d+)", name)):
        return lqfp(int(m.group(1)))
    if (m := re.fullmatch(r"QFN-(\d+)-(\d+(?:\.\d+)?)x\d+(?:\.\d+)?(?:-EP(\d+(?:\.\d+)?))?(?:-P(\d+(?:\.\d+)?))?", name)):
        return qfn(int(m.group(1)), float(m.group(2)), float(m.group(3)) if m.group(3) else None,
                   float(m.group(4)) if m.group(4) else 0.5)
    if (m := re.fullmatch(r"HDR-1x(\d+)", name)):
        return header_vertical(int(m.group(1)), 1)
    if (m := re.fullmatch(r"HDR-2x(\d+)", name)):
        return header_vertical(int(m.group(1)), 2)
    if name == "TO-220-3":
        return to220()
    if (m := re.fullmatch(r"JST-XH-(\d+)", name)):
        return jst_xh(int(m.group(1)))
    if (m := re.fullmatch(r"TB-5\.08-(\d+)", name)):
        return screw_terminal(int(m.group(1)))
    if name == "XT60PW":
        return xt60pw()
    if (m := re.fullmatch(r"CP-D(\d+(?:\.\d+)?)", name)):
        return radial_cap(float(m.group(1)))
    if (m := re.fullmatch(r"D_(SMA|SMB|SMC)", name)):
        return diode_sm(m.group(1))
    if name == "XTAL-3225":
        return crystal_3225()
    if (m := re.fullmatch(r"IND-(\d+x\d+)", name)):
        return inductor(m.group(1))
    if (m := re.fullmatch(r"MH-M(\d+(?:\.\d+)?)", name)):
        return mount_hole(float(m.group(1)))
    if (m := re.fullmatch(r"TP-(\d+(?:\.\d+)?)", name)):
        return testpoint(float(m.group(1)))
    raise KeyError(f"unknown footprint '{name}' (register one with footprints.register or import a .kicad_mod)")


def validate_footprint(fp: Footprint, pin_numbers: set[str] | None = None) -> list[str]:
    """Sanity checks for land patterns that came from an LLM / import (not generators)."""
    from shapely.geometry import box
    probs: list[str] = []
    if not fp.pads:
        probs.append("no pads")
    boxes = [(p, box(p.x - p.w / 2, p.y - p.h / 2, p.x + p.w / 2, p.y + p.h / 2)) for p in fp.pads]
    for i, (a, ba) in enumerate(boxes):
        if a.w <= 0 or a.h <= 0:
            probs.append(f"pad {a.number} has non-positive size")
        if a.thru and a.drill >= min(a.w, a.h) and not a.slot:
            probs.append(f"pad {a.number}: drill {a.drill} >= pad size (no annular ring)")
        for b, bb in boxes[i + 1:]:
            if a.number != b.number and ba.intersection(bb).area > 1e-6:
                probs.append(f"pads {a.number} and {b.number} overlap")
    if pin_numbers is not None:
        missing = pin_numbers - fp.pad_numbers()
        if missing:
            probs.append(f"pins without pads: {sorted(missing)}")
    x0, y0, x1, y1 = fp.courtyard
    for p in fp.pads:
        if p.x - p.w / 2 < x0 - 1e-6 or p.x + p.w / 2 > x1 + 1e-6 or p.y - p.h / 2 < y0 - 1e-6 or p.y + p.h / 2 > y1 + 1e-6:
            probs.append(f"pad {p.number} outside courtyard")
            break
    return probs
