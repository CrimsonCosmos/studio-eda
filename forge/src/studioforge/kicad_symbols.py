"""Index of KiCad's schematic symbol libraries (/usr/share/kicad/symbols, override $KICAD_SYMBOL_DIR).

`find_symbols` searches by name / keywords / description / footprint filter / pin count, `symbol_for_part`
picks the best symbol for a studioforge PartDef, and `symbol_node` returns a fully flattened (extends resolved)
symbol s-expression ready to embed in a schematic's lib_symbols.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from . import sexpr


def symbol_dir() -> Path | None:
    for c in (os.environ.get("KICAD_SYMBOL_DIR"), os.environ.get("KICAD_SYMBOLS"), "/usr/share/kicad/symbols",
              "/usr/local/share/kicad/symbols", "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"):
        if c and Path(c).is_dir() and any(Path(c).glob("*.kicad_sym")):
            return Path(c)
    return None


def symbols_available() -> bool:
    return symbol_dir() is not None


@dataclass
class SymInfo:
    lib: str
    name: str
    extends: str = ""
    keywords: str = ""
    description: str = ""
    fp_filters: list[str] = field(default_factory=list)
    footprint: str = ""
    pins: int = 0
    power: bool = False

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"


_SYM = re.compile(r'^  \(symbol "([^"]+)"( \(extends "([^"]+)"\))?', re.M)
_PROP = re.compile(r'^    \(property "(ki_keywords|ki_description|ki_fp_filters|Footprint)" "((?:[^"\\]|\\.)*)"', re.M)
_NUM = re.compile(r'\(number "([^"]+)"')


def _scan_lib(path: Path) -> list[SymInfo]:
    t = path.read_text(errors="ignore")
    ms = list(_SYM.finditer(t))
    out = []
    for i, m in enumerate(ms):
        block = t[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        info = SymInfo(path.stem, m.group(1), m.group(3) or "")
        for k, v in _PROP.findall(block):
            if k == "ki_keywords":
                info.keywords = v
            elif k == "ki_description":
                info.description = v
            elif k == "ki_fp_filters":
                info.fp_filters = v.split()
            else:
                info.footprint = v
        info.pins = len(set(_NUM.findall(block)))
        info.power = "(power)" in block[:600]
        out.append(info)
    return out


def _cache_file(root: Path) -> Path:
    base = os.environ.get("FORGE_HOME")
    d = Path(base) if base else Path(tempfile.gettempdir()) / "studioforge_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "kicad_symbol_index.json"


@lru_cache(maxsize=4)
def _index(root_s: str) -> tuple[SymInfo, ...]:
    root = Path(root_s)
    files = sorted(root.glob("*.kicad_sym"))
    stamp = f"{len(files)}:{max((f.stat().st_mtime_ns for f in files), default=0)}:{root_s}"
    cf = _cache_file(root)
    try:
        d = json.loads(cf.read_text())
        if d.get("stamp") == stamp:
            return tuple(SymInfo(**r) for r in d["rows"])
    except Exception:
        pass
    rows: list[SymInfo] = []
    for f in files:
        rows += _scan_lib(f)
    try:
        cf.write_text(json.dumps({"stamp": stamp, "rows": [asdict(r) for r in rows]}))
    except OSError:
        pass
    return tuple(rows)


def index() -> list[SymInfo]:
    root = symbol_dir()
    return list(_index(str(root))) if root else []


@lru_cache(maxsize=1)
def _by_id() -> dict[str, SymInfo]:
    return {s.lib_id: s for s in index()}


def info(lib_id: str) -> SymInfo | None:
    return _by_id().get(lib_id)


def _norm(s: str) -> str:
    return re.sub(r"[-_\s]+", " ", s.lower())


def find_symbols(query: str = "", footprint: str | None = None, pins: int | None = None, lib: str | None = None,
                 limit: int = 20) -> list[SymInfo]:
    """Symbols whose name/keywords/description contain every query word; `footprint` matches the library's footprint
    filters (fnmatch on e.g. 'R_0402_1005Metric'); `pins` = exact distinct pin-number count. Best match first."""
    from fnmatch import fnmatchcase
    toks = _norm(query).split()
    rows = []
    for s in index():
        if lib and s.lib != lib:
            continue
        hay = _norm(f"{s.name} {s.keywords} {s.description}")
        if not all(t in hay for t in toks):
            continue
        pn = s.pins
        if s.extends and not pn:
            par = info(f"{s.lib}:{s.extends}")
            pn = par.pins if par else 0
        if pins is not None and pn != pins:
            continue
        if footprint:
            fb = footprint.split(":")[-1]
            flt = s.fp_filters or (info(f"{s.lib}:{s.extends}").fp_filters if s.extends and info(f"{s.lib}:{s.extends}") else [])
            if not any(fnmatchcase(fb.lower(), f.lower()) for f in flt):
                continue
        nm = _norm(s.name)
        rank = (0 if nm == _norm(query) else 1 if toks and nm.startswith(toks[0]) else 2,
                0 if s.lib in ("Device", "Connector_Generic", "Connector") else 1, len(s.name), s.lib_id)
        rows.append((rank, s))
    rows.sort(key=lambda r: r[0])
    return [s for _, s in rows[:limit]]


# ---- symbol bodies ------------------------------------------------------------------
@lru_cache(maxsize=1024)
def _lib_tree(lib: str, root_s: str):
    return sexpr.parse((Path(root_s) / f"{lib}.kicad_sym").read_text())


def _find_top(lib: str, name: str):
    root = symbol_dir()
    tree = _lib_tree(lib, str(root))
    for c in tree:
        if isinstance(c, list) and c and c[0] == "symbol" and c[1] == name:
            return c
    return None


def symbol_node(lib_id: str) -> list | None:
    """Flattened symbol s-expression named 'Lib:Name' (extends resolved, sub-symbol names rewritten)."""
    if ":" not in lib_id or not symbol_dir():
        return None
    lib, name = lib_id.split(":", 1)
    node = _find_top(lib, name)
    if node is None:
        return None
    ext = sexpr.find(node, "extends")
    if ext:
        parent = _find_top(lib, str(ext[1]))
        if parent is None:
            return None
        pname = str(ext[1])
        own_props = {str(c[1]): c for c in node if isinstance(c, list) and c and c[0] == "property"}
        out: list = ["symbol", sexpr.Q(lib_id)]
        for c in parent[2:]:
            if isinstance(c, list) and c and c[0] == "property" and str(c[1]) in own_props:
                out.append(own_props.pop(str(c[1])))
            elif isinstance(c, list) and c and c[0] == "symbol":
                cc = list(c)
                cc[1] = sexpr.Q(str(c[1]).replace(pname, name, 1))
                out.append(cc)
            else:
                out.append(c)
        out += list(own_props.values())
        return out
    out = list(node)
    out[1] = sexpr.Q(lib_id)
    return out


def symbol_pins(lib_id: str) -> list[dict]:
    """[{number, name, type, x, y, angle, length}] for the symbol (all units / styles, deduplicated by number)."""
    node = symbol_node(lib_id)
    res, seen = [], set()
    if node is None:
        return res

    def walk(n):
        for c in n:
            if isinstance(c, list) and c:
                if c[0] == "pin":
                    at = sexpr.find(c, "at"); ln = sexpr.find(c, "length")
                    num = str(sexpr.find(c, "number")[1])
                    if num not in seen:
                        seen.add(num)
                        res.append({"number": num, "name": str(sexpr.find(c, "name")[1]), "type": str(c[1]),
                                    "x": float(at[1]), "y": float(at[2]), "angle": float(at[3]), "length": float(ln[1])})
                elif c[0] == "symbol":
                    walk(c)
    walk(node)
    return res


# ---- part -> symbol -----------------------------------------------------------------------
_CATEGORY = [("resistor", "Device:R"), ("capacitor.electrolytic", "Device:CP"), ("capacitor", "Device:C"), ("inductor", "Device:L"),
             ("ferrite", "Device:FerriteBead"), ("led", "Device:LED"), ("fuse", "Device:Fuse"), ("crystal", "Device:Crystal_GND24"),
             ("testpoint", "Connector:TestPoint")]


def symbol_for_part(part, footprint_name: str = "") -> str | None:
    """Best KiCad symbol lib_id for a PartDef, or None (caller then draws a box symbol from the part's pins)."""
    if not symbols_available():
        return None
    lid = _symbol_for_part(part)
    return lid if lid and (not part.pins or compatible(part, lid)) else None


def _symbol_for_part(part) -> str | None:
    cat = part.category or ""
    npins = len(part.pins)
    for pre, lid in _CATEGORY:
        if cat.startswith(pre) and info(lid):
            if lid == "Device:Crystal_GND24" and npins != 4:
                return "Device:Crystal" if info("Device:Crystal") else None
            return lid
    if cat.startswith("connector") and npins:
        m = re.search(r"(\d)x(\d+)", part.package or "")
        rows, cols = (int(m.group(1)), int(m.group(2))) if m else (1, npins)
        if rows == 2 and rows * cols == npins and info(f"Connector_Generic:Conn_02x{cols:02d}_Odd_Even"):
            return f"Connector_Generic:Conn_02x{cols:02d}_Odd_Even"
        if info(f"Connector_Generic:Conn_01x{npins:02d}"):
            return f"Connector_Generic:Conn_01x{npins:02d}"
        return None
    if cat.startswith("diode") or cat == "diode":
        return "Device:D"
    if cat.startswith("mounting"):
        return "Mechanical:MountingHole" if info("Mechanical:MountingHole") else None
    if cat.startswith("mosfet"):
        return None                       # pin order differs per package; box symbol keeps our (verified) pin numbers
    for q in (part.mpn, *getattr(part, "aliases", ())):
        r = [s for s in find_symbols(q, limit=10) if _norm(s.name) == _norm(q) and s.pins == npins]
        if r:
            return r[0].lib_id
    return None


def symbol_units(lib_id: str) -> int:
    node = symbol_node(lib_id)
    units = set()
    for c in (node or []):
        if isinstance(c, list) and c and c[0] == "symbol":
            m = re.search(r"_(\d+)_(\d+)$", str(c[1]))
            if m and m.group(1) != "0":
                units.add(m.group(1))
    return max(len(units), 1)


_GENERIC = re.compile(r"^(~|\d+|pin_?\d+|p\d+)?$", re.I)


def compatible(part, lib_id: str) -> bool:
    """True when `lib_id` can stand for `part`: single unit, every part pin number exists on the symbol, and
    meaningful pin names agree (so an A/K or +/- swap is never silently drawn)."""
    if not lib_id or symbol_units(lib_id) != 1:
        return False
    sp = {p["number"]: p for p in symbol_pins(lib_id)}
    if len(sp) != len([p for p in part.pins]) or any(p.number not in sp for p in part.pins):
        return False
    for p in part.pins:
        sn, pn = sp[p.number]["name"], p.name
        if _GENERIC.match(sn) or _GENERIC.match(pn):
            continue
        a, b = _norm(sn), _norm(pn)
        if a != b and a not in b and b not in a:
            return False
    return True
