"""Parts library: SQLite store + seed catalog + synthesised generics + research fallback."""
from __future__ import annotations

import json
import operator
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from ..model import PartDef, Footprint
from . import generic, seed
from .docs import safe_name

SEED_VERSION = 7
_OPS: dict[str, Callable[[Any, Any], bool]] = {">=": operator.ge, "<=": operator.le, "==": operator.eq,
                                              ">": operator.gt, "<": operator.lt, "!=": operator.ne}


class PartNotFound(LookupError):
    pass


def default_home() -> Path:
    return Path(os.environ.get("FORGE_HOME") or Path.home() / ".studioforge")


class PartsLibrary:
    def __init__(self, root: str | Path | None = None, llm=None, auto_research: bool = True):
        self.root = Path(root) if root else default_home()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "docs").mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(self.root / "parts.db"))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS parts(mpn TEXT PRIMARY KEY, category TEXT, package TEXT,
                provenance TEXT, json TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, mpn TEXT);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS misses(query TEXT, ts REAL, resolved INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_cat ON parts(category);
        """)
        self.llm = llm
        self.auto_research = auto_research
        self.research_log: list[str] = []
        self._cache: dict[str, PartDef] = {}
        self._ensure_seed()

    # ---- storage -------------------------------------------------------------------
    def _ensure_seed(self) -> None:
        row = self.conn.execute("SELECT v FROM meta WHERE k='seed_version'").fetchone()
        if row and int(row[0]) == SEED_VERSION:
            return
        for p in seed.seed_parts():
            existing = self.get(p.mpn)
            if existing and existing.provenance != "seed":
                continue                  # never clobber researched/verified data
            if existing and existing.verified:
                continue
            self.put(p)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES('seed_version', ?)", (str(SEED_VERSION),))
        self.conn.commit()

    def put(self, part: PartDef) -> None:
        self.conn.execute("INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?,?)",
                          (part.mpn, part.category, part.package, part.provenance, json.dumps(part.to_dict()), time.time()))
        for a in part.aliases:
            self.conn.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (a.lower(), part.mpn))
        self.conn.commit()
        self._cache[part.mpn] = part

    def get(self, ident: str) -> PartDef | None:
        if ident in self._cache:
            return self._cache[ident]
        row = self.conn.execute("SELECT json FROM parts WHERE mpn=? COLLATE NOCASE", (ident,)).fetchone()
        if not row:
            a = self.conn.execute("SELECT mpn FROM aliases WHERE alias=?", (ident.lower(),)).fetchone()
            if a:
                row = self.conn.execute("SELECT json FROM parts WHERE mpn=?", (a[0],)).fetchone()
        if not row:
            return None
        part = PartDef.from_dict(json.loads(row[0]))
        self._cache[part.mpn] = part
        self._cache[ident] = part
        return part

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]

    def all(self, category: str | None = None) -> list[PartDef]:
        q, args = "SELECT json FROM parts", ()
        if category:
            q += " WHERE category=? OR category LIKE ?"
            args = (category, category + ".%")
        return [PartDef.from_dict(json.loads(r[0])) for r in self.conn.execute(q, args)]

    # ---- generics (stored on first use so a design's BOM is reproducible) --------------
    def _g(self, part: PartDef) -> PartDef:
        have = self.get(part.mpn)
        if have:
            return have
        self.put(part)
        return part

    def resistor(self, ohms, package="0402", tol=1.0, power_w=None): return self._g(generic.resistor(ohms, package, tol, power_w))
    def shunt(self, milliohms, power_w=1.0, package="2512"): return self._g(generic.shunt(milliohms, power_w, package))
    def capacitor(self, farads, package="0603", vrated=25.0, dielectric="X7R"): return self._g(generic.capacitor(farads, package, vrated, dielectric))
    def mlcc(self, farads, vrated=25.0): return self._g(generic.pick_mlcc(farads, vrated))
    def electrolytic(self, farads, vrated): return self._g(generic.electrolytic(farads, vrated))
    def inductor(self, henries, isat_min): return self._g(generic.inductor(henries, isat_min))
    def led(self, color="green", package="0603"): return self._g(generic.led(color, package))
    def ferrite(self, package="0603", ohms100=600, amps=0.5): return self._g(generic.ferrite(package, ohms100, amps))
    def fuse(self, amps, volts=63.0, package="1206"): return self._g(generic.fuse(amps, volts, package))
    def crystal(self, mhz=8.0, load_pf=12.0): return self._g(generic.crystal(mhz, load_pf))
    def header(self, n, rows=1, desc=""): return self._g(generic.pin_header(n, rows, desc))
    def jst_xh(self, n): return self._g(generic.jst_xh(n))
    def terminal(self, n): return self._g(generic.screw_terminal(n))
    def testpoint(self): return self._g(generic.testpoint())
    def mounting_hole(self, d=3.2): return self._g(generic.mounting_hole(d))

    # ---- search ------------------------------------------------------------------------
    def search(self, text: str, limit: int = 20) -> list[PartDef]:
        toks = [t for t in re.split(r"\s+", text.strip().lower()) if t]
        out = []
        for p in self.all():
            hay = " ".join([p.mpn, p.manufacturer, p.description, p.category, p.package, " ".join(p.tags), " ".join(p.aliases)]).lower()
            if all(t in hay for t in toks):
                out.append(p)
        return out[:limit]

    def find(self, category: str, where: dict[str, tuple[str, Any]] | None = None,
             tags: list[str] | None = None, qty: int = 1) -> list[PartDef]:
        """Parametric search, cheapest first. where = {"vds_max": (">=", 100)}."""
        res = []
        for p in self.all(category):
            ok = True
            for k, (op, val) in (where or {}).items():
                if k not in p.params or not _OPS[op](p.params[k], val):
                    ok = False
                    break
            if ok and tags and not all(t in p.tags for t in tags):
                ok = False
            if ok:
                res.append(p)

        def cost(p: PartDef) -> float:
            o = p.best_offer(qty)
            pr = o.price_at(qty) if o else None
            return pr if pr is not None else 1e9
        res.sort(key=lambda p: (not p.verified, cost(p)))
        return res

    # ---- resolution with research fallback ---------------------------------------------
    def require(self, category: str, where: dict[str, tuple[str, Any]] | None = None,
                need: str = "", tags: list[str] | None = None, qty: int = 1) -> PartDef:
        """Best part satisfying constraints; asks the research agent if the catalog has none."""
        found = self.find(category, where, tags, qty)
        if found:
            return found[0]
        spec = need or f"{category} with " + ", ".join(f"{k} {op} {v}" for k, (op, v) in (where or {}).items())
        part = self.research(spec, category_hint=category, where=where)
        return part

    def resolve(self, query: str) -> PartDef:
        """MPN / alias / free-text -> part. Unknown => research (if enabled) else PartNotFound."""
        p = self.get(query)
        if p:
            return p
        hits = self.search(query, 1)
        if hits and query.lower() in hits[0].mpn.lower():
            return hits[0]
        return self.research(query)

    def research(self, spec: str, category_hint: str = "", where: dict | None = None) -> PartDef:
        self.conn.execute("INSERT INTO misses VALUES(?,?,0)", (spec, time.time()))
        self.conn.commit()
        if not self.auto_research or self.llm is None:
            raise PartNotFound(f"no part matches '{spec}' and research is disabled/offline. "
                               f"Run: forge parts research \"{spec}\"")
        from .research import research_part
        part = research_part(self, spec, category_hint=category_hint, where=where)
        self.conn.execute("UPDATE misses SET resolved=1 WHERE query=?", (spec,))
        self.conn.commit()
        self.research_log.append(f"researched '{spec}' -> {part.mpn}")
        return part

    def doc_dir(self, mpn: str) -> Path:
        d = self.root / "docs" / safe_name(mpn)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def pending_misses(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT query FROM misses WHERE resolved=0")]
