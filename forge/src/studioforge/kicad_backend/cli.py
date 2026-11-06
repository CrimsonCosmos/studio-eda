"""Thin wrapper around KiCad 7.0 tooling.

Facts about KiCad 7.0.x (verified on 7.0.11):
  * `kicad-cli pcb` only has `export` (gerber(s), drill, pos, pdf, svg, dxf, step). There is NO `pcb drc`
    (arrived in 8.0), and no IPC-2581 / ODB++ export (8.0+).  DRC therefore runs through the `pcbnew` python
    module (`pcbnew.WriteDRCReport`) in a subprocess of the system python that ships with KiCad.
  * DRC and plotting use the zone fill stored in the file; we refill every zone with ZONE_FILLER first so the
    result reflects KiCad's own copper, not our pre-computed polygons.
  * Design-rule limits come from `<board>.kicad_pro` next to the board (see `write_kicad_pro`).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


def find_kicad_cli() -> str | None:
    return shutil.which("kicad-cli")


def find_kicad_python() -> str | None:
    """A python interpreter that can `import pcbnew` (KiCad bindings live in the distro python)."""
    for exe in ("/usr/bin/python3", shutil.which("python3"), sys.executable):
        if not exe or not Path(exe).exists():
            continue
        try:
            r = subprocess.run([exe, "-c", "import pcbnew"], capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            return exe
    return None


def available() -> bool:
    return find_kicad_cli() is not None


def kicad_version() -> str | None:
    exe = find_kicad_cli()
    if not exe:
        return None
    r = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=60)
    return r.stdout.strip() or None


def capabilities() -> dict:
    """What the installed KiCad can do (7.0 has no CLI DRC, IPC-2581 or ODB++)."""
    exe = find_kicad_cli()
    out = {"kicad_cli": exe, "version": kicad_version(), "pcbnew_python": find_kicad_python(),
           "cli_drc": False, "ipc2581": False, "odb": False, "export": []}
    if not exe:
        return out
    txt = subprocess.run([exe, "pcb", "export", "--help"], capture_output=True, text=True).stdout
    out["export"] = re.findall(r"^\s{2,}(\w+)\s", txt, re.M)
    out["ipc2581"] = "ipc2581" in out["export"]
    out["odb"] = "odb" in out["export"]
    top = subprocess.run([exe, "pcb", "--help"], capture_output=True, text=True).stdout
    out["cli_drc"] = bool(re.search(r"^\s+drc\b", top, re.M))
    return out


# ---------------------------------------------------------------------------------------------------- project
def write_kicad_pro(path: str | Path, rules=None, name: str | None = None) -> Path:
    """Write a minimal .kicad_pro so KiCad's DRC uses OUR fab rules instead of its defaults.

    `rules` is a studioforge `pcb.rules.Rules` (or any object with the same attribute names)."""
    path = Path(path)
    g = (lambda k, d: getattr(rules, k, d)) if rules is not None else (lambda k, d: d)
    clr, trk, via, drl = g("clearance", 0.2), g("track", 0.2), g("via_dia", 0.6), g("via_drill", 0.3)
    pro = {
        "board": {"design_settings": {
            "defaults": {"board_outline_line_width": 0.1, "copper_line_width": 0.2, "copper_text_size_h": 1.5,
                         "copper_text_size_v": 1.5, "copper_text_thickness": 0.3, "other_line_width": 0.1,
                         "silk_line_width": 0.12, "silk_text_size_h": 0.8, "silk_text_size_v": 0.8,
                         "silk_text_thickness": 0.12},
            "rules": {"min_clearance": clr, "min_track_width": g("min_track", 0.127),
                      "min_via_diameter": max(g("min_drill", 0.3) + 2 * g("min_annular", 0.13), 0.0),
                      "min_through_hole_diameter": g("min_drill", 0.3), "min_via_annular_width": g("min_annular", 0.13),
                      "min_hole_to_hole": g("hole_to_hole", 0.35), "min_copper_edge_clearance": g("edge_clearance", 0.4),
                      "min_hole_clearance": 0.25, "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1,
                      "min_silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.08,
                      "min_resolved_spokes": 2, "min_connection_width": 0.0, "solder_mask_to_copper_clearance": 0.0},
            # footprints come from the project's own generator, not a KiCad library table
            "rule_severities": {"lib_footprint_issues": "ignore", "lib_footprint_mismatch": "ignore"},
        }},
        "meta": {"filename": name or path.name, "version": 1},
        "net_settings": {"classes": [{"name": "Default", "clearance": clr, "track_width": trk, "via_diameter": via,
                                      "via_drill": drl, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
                                      "diff_pair_width": 0.2, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                                      "wire_width": 6, "bus_width": 12, "line_style": 0, "pcb_color": "rgba(0, 0, 0, 0.000)",
                                      "schematic_color": "rgba(0, 0, 0, 0.000)"}],
                         "meta": {"version": 3}, "net_colors": None},
    }
    path.write_text(json.dumps(pro, indent=2))
    return path


# ---------------------------------------------------------------------------------------------------- pcbnew scripts
_DRC_SCRIPT = r'''
import sys, pcbnew
src, report, refill, saved = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
b = pcbnew.LoadBoard(src)
if refill:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    if saved != "-":
        pcbnew.SaveBoard(saved, b)
ok = pcbnew.WriteDRCReport(b, report, pcbnew.EDA_UNITS_MILLIMETRES, True)
sys.exit(0 if ok else 3)
'''

_FILL_SCRIPT = r'''
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
pcbnew.SaveBoard(sys.argv[2], b)
'''


def _run_py(script: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    py = find_kicad_python()
    if not py:
        raise RuntimeError("no python interpreter with the pcbnew module found (install KiCad's python bindings)")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        sp = f.name
    try:
        return subprocess.run([py, sp, *args], capture_output=True, text=True, timeout=timeout)
    finally:
        Path(sp).unlink(missing_ok=True)


def refill_zones(pcb_path: str | Path, out_path: str | Path) -> Path:
    r = _run_py(_FILL_SCRIPT, str(pcb_path), str(out_path))
    if r.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"zone refill failed: {r.stderr.strip()[-500:]}")
    return Path(out_path)


# ---------------------------------------------------------------------------------------------------- DRC
@dataclass
class DrcReport:
    version: str | None = None
    violations: list[dict] = field(default_factory=list)     # {type, description, severity, rule, items:[{x,y,desc}]}
    unconnected: list[dict] = field(default_factory=list)    # same shape; type == 'unconnected_items'
    footprint_errors: list[dict] = field(default_factory=list)
    refilled: bool = True
    raw: str = ""

    @property
    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v["type"]] = out.get(v["type"], 0) + 1
        return out

    @property
    def errors(self) -> list[dict]:
        return [v for v in self.violations if v["severity"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [v for v in self.violations if v["severity"] != "error"]

    @property
    def clean(self) -> bool:
        return not self.errors and not self.unconnected and not self.footprint_errors

    def summary(self) -> str:
        return (f"{len(self.errors)} errors, {len(self.warnings)} warnings, {len(self.unconnected)} unconnected, "
                f"{len(self.footprint_errors)} footprint errors")

    def to_dict(self) -> dict:
        return {"tool": "kicad pcbnew DRC", "version": self.version, "zones_refilled": self.refilled,
                "clean": self.clean, "counts": {"errors": len(self.errors), "warnings": len(self.warnings),
                                                "unconnected": len(self.unconnected),
                                                "footprint_errors": len(self.footprint_errors)},
                "by_type": self.by_type, "violations": self.violations, "unconnected_items": self.unconnected,
                "footprint_errors": self.footprint_errors}


_ITEM = re.compile(r"@\(([-\d.]+) mm, ([-\d.]+) mm\):\s*(.*)")


def parse_drc_text(text: str) -> DrcReport:
    rep = DrcReport(raw=text)
    section = "violations"
    cur: dict | None = None
    for line in text.splitlines():
        if line.startswith("** Found") and "unconnected" in line:
            section = "unconnected"
            cur = None
            continue
        if line.startswith("** Found") and "Footprint errors" in line:
            section = "footprint"
            cur = None
            continue
        m = re.match(r"^\[(\w+)\]: (.*)", line)
        if m:
            cur = {"type": m.group(1), "description": m.group(2), "severity": "error", "rule": "", "items": []}
            {"violations": rep.violations, "unconnected": rep.unconnected, "footprint": rep.footprint_errors}[section].append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if "Severity:" in s:
            rule, _, sev = s.rpartition("Severity:")
            cur["severity"] = sev.strip().lower()
            cur["rule"] = rule.strip(" ;")
        else:
            im = _ITEM.match(s)
            if im:
                cur["items"].append({"x": float(im.group(1)), "y": float(im.group(2)), "desc": im.group(3).strip()})
    return rep


def run_drc(pcb_path: str | Path, refill: bool = True, rules=None, report_path: str | Path | None = None) -> DrcReport:
    """Run KiCad's DRC on a .kicad_pcb. Zones are refilled in memory first (KiCad DRC needs a valid fill).

    If `rules` is given and no <board>.kicad_pro exists, one is written beside the board (in a temp copy, the
    original directory is never touched)."""
    pcb = Path(pcb_path).resolve()
    with tempfile.TemporaryDirectory(prefix="kidrc_") as td:
        tdp = Path(td)
        work = tdp / pcb.name
        shutil.copy(pcb, work)
        pro = pcb.with_suffix(".kicad_pro")
        if pro.exists():
            shutil.copy(pro, work.with_suffix(".kicad_pro"))
        else:
            write_kicad_pro(work.with_suffix(".kicad_pro"), rules)
        out = tdp / "drc.rpt"
        r = _run_py(_DRC_SCRIPT, str(work), str(out), "1" if refill else "0", "-")
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"KiCad DRC failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}")
        text = out.read_text(errors="replace")
    rep = parse_drc_text(text)
    rep.version = kicad_version()
    rep.refilled = refill
    if report_path:
        Path(report_path).write_text(json.dumps(rep.to_dict(), indent=1))
    return rep


# ---------------------------------------------------------------------------------------------------- fab export
def gerber_layers(n_copper: int) -> list[str]:
    """Untranslated KiCad layer names for a fab set. Works for 2/4/6/... layer boards."""
    cu = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n_copper - 1)] + ["B.Cu"]
    return cu + ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]


def _copper_count(pcb: Path) -> int:
    txt = pcb.read_text(errors="replace")
    names = set(re.findall(r'"((?:F|B|In\d+)\.Cu)"', txt.split("(footprint", 1)[0]))
    return max(2, len(names))


def _cli(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    exe = find_kicad_cli()
    if not exe:
        raise RuntimeError("kicad-cli not found on PATH")
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli {' '.join(args[:3])} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def export_fab(pcb_path: str | Path, outdir: str | Path, project: str | None = None, refill: bool = True,
               make_zip: bool = True) -> dict[str, Path]:
    """Gerbers (+ gerber job file via KiCad), Excellon drills (PTH/NPTH separate + map), position CSV, and a zip.

    Returns {key: path}. IPC-2581 / ODB++ are unavailable in KiCad 7.0 (see `capabilities()`)."""
    pcb = Path(pcb_path).resolve()
    out = Path(outdir)
    gdir = out / "gerber"
    gdir.mkdir(parents=True, exist_ok=True)
    project = project or pcb.stem
    files: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="kifab_") as td:
        src = Path(td) / f"{project}.kicad_pcb"
        if refill:
            refill_zones(pcb, src)
        else:
            shutil.copy(pcb, src)
        n = _copper_count(pcb)
        _cli("pcb", "export", "gerbers", "-o", str(gdir) + "/", "-l", ",".join(gerber_layers(n)),
             "--subtract-soldermask", str(src))
        _cli("pcb", "export", "drill", "-o", str(gdir) + "/", "--format", "excellon", "--excellon-separate-th",
             "--excellon-units", "mm", "--excellon-zeros-format", "decimal", "--generate-map", "--map-format", "gerberx2",
             str(src))
        pos = out / "kicad_positions.csv"
        _cli("pcb", "export", "pos", "-o", str(pos), "--format", "csv", "--units", "mm", "--side", "both", str(src))
    for p in sorted(gdir.iterdir()):
        files[f"gerber:{p.name}"] = p
    if pos.exists():
        files["positions"] = pos
    if make_zip:
        zp = out / f"{project}_kicad_fab.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(gdir.iterdir()):
                z.write(p, p.name)
        files["fab_zip"] = zp
    return files
