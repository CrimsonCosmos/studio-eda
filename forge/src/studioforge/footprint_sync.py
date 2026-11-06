"""KiCad-library footprint lookup, verification and per-circuit source reporting.

KiCad's installed libraries (/usr/share/kicad/footprints) are the source of truth; there is no network access.
Our generators are compared against them (`verify_all`) and only used when KiCad has no equivalent.
"""
from __future__ import annotations

from pathlib import Path

from . import footprints as fpmod
from .kicad_mod import parse_footprint
from .model import Footprint

_NAMES = ["R_0402", "R_0603", "R_0805", "R_1206", "R_1210", "R_2512", "C_0402", "C_0603", "C_0805", "C_1206", "C_1210", "LED_0603",
          "SOIC-8", "SOIC-16", "SOIC-8-EP", "TSSOP-14", "SOT-23", "SOT-23-5", "SOT-23-6", "SOT-223", "TO-252", "TO-220-3", "LQFP-48",
          "D_SMA", "D_SMB", "D_SMC", "XTAL-3225", "HDR-1x04", "HDR-1x05", "HDR-1x08", "HDR-2x05", "JST-XH-2", "JST-XH-3", "JST-XH-4",
          "TB-5.08-2", "TB-5.08-3", "XT60PW", "CP-D6.3", "CP-D8", "CP-D10", "TP-1.5"]


def kicad_path(name: str) -> str | None:
    """Our footprint name -> 'Library.pretty/File' in KiCad's library (None if there is no equivalent installed)."""
    lid = fpmod.legacy_to_kicad(name) if not fpmod.is_kicad_id(name) else (name if fpmod.kicad_mod_path(name) else None)
    if not lid:
        return None
    lib, n = lid.split(":", 1)
    return f"{lib}.pretty/{n}"


def fetch(name: str, home: Path | None = None, timeout: int = 20) -> Path | None:
    """Local path of the KiCad .kicad_mod for `name` (no network; args kept for API compatibility)."""
    lid = fpmod.legacy_to_kicad(name) if not fpmod.is_kicad_id(name) else name
    return fpmod.kicad_mod_path(lid) if lid else None


def official(name: str, home: Path | None = None) -> Footprint | None:
    """KiCad's footprint for `name` (our pad numbering = KiCad's), straight from the local library, or None."""
    lid = fpmod.legacy_to_kicad(name) if not fpmod.is_kicad_id(name) else name
    k = fpmod.kicad_footprint(lid) if lid else None
    if k is None:
        return None
    return Footprint(name, k.pads, k.courtyard, k.silk, k.height, k.source, k.lib_id)


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
    """Compare every mappable generator footprint with KiCad's; returns rows for CLI/tests."""
    rows = []
    for n in _NAMES:
        gen = fpmod._make(n)
        off = official(n, home)
        if off is None:
            rows.append({"name": n, "status": "no-official-source" if not fpmod.kicad_available() or not kicad_path(n) else "missing", "diffs": []})
            continue
        d = compare(gen, off)
        rows.append({"name": n, "status": "match" if not d else "DIFFERS", "diffs": d[:4]})
    return rows


def apply_official(home: Path | None = None) -> int:
    """Register KiCad geometry for every known legacy name; returns count. (resolve_footprint does this lazily anyway.)"""
    n = 0
    for name in _NAMES:
        fp = official(name, home)
        if fp:
            fpmod.register(fp, name)
            n += 1
    return n


def sync_circuit(circuit, home: Path | None = None) -> dict[str, str]:
    """Make every component use KiCad's footprint where one exists and covers the part's pins; records
    {footprint name: source} in circuit.calcs['footprint_sources'] so reports state what was used."""
    info: dict[str, str] = {}
    for comp in circuit.components.values():
        spec = comp.part.footprint
        name = spec if isinstance(spec, str) else spec.name
        if isinstance(spec, str) and name not in info:
            off = official(name, home)
            need = {p.number for p in comp.part.pins if p.etype != "nc"}
            if off is not None and need <= off.pad_numbers() | {""}:
                fpmod.register(off, name)
            elif off is not None:
                try:                       # KiCad's pad numbering does not cover this part's pins: keep our generator for it
                    g = fpmod._make(name)
                    g.source = f"generator:kicad-mismatch({off.lib_id})"
                    fpmod.register(g, name)
                except KeyError:
                    pass
            for c in circuit.components.values():
                if c.part.footprint == spec:
                    c._fp = None
        info[name] = comp.footprint.source
    circuit.calcs["footprint_sources"] = info
    return info
