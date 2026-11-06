"""Write the complete manufacturing + review package for a built board."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ..pcb.flow import BuildResult
from . import bom as bomx
from . import gerber, kicad, svg
from .report import render_report


def write_package(res: BuildResult, outdir: str | Path, project: str, extra: dict | None = None) -> dict[str, Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    b = res.board
    c = b.circuit
    files: dict[str, Path] = {}
    gdir = out / "gerber"
    gfiles = gerber.write_gerbers(b, gdir, project)
    for k, v in gfiles.items():
        files[f"gerber:{k}"] = v

    def w(name: str, text: str, key: str | None = None) -> None:
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        files[key or name] = p
    w("bom.csv", bomx.bom_csv(c))
    w("bom_jlcpcb.csv", bomx.bom_jlcpcb(c))
    w("cpl_jlcpcb.csv", bomx.cpl_csv(b))
    w("positions.csv", bomx.cpl_generic(b))
    w(f"{project}.kicad_pcb", kicad.to_kicad_pcb(b, project))
    w(f"{project}.net", kicad.to_kicad_netlist(c))
    w("board_top.svg", svg.board_svg(b, [b.layers[0]] if len(b.layers) > 2 else b.layers))
    for layer in b.layers:
        w(f"layers/{layer.replace('.', '_')}.svg", svg.board_svg(b, [layer], zones=True, silk=layer == "F.Cu"))
    w("design.json", json.dumps(c.to_dict(), indent=1, default=str))
    w("REPORT.md", render_report(res, project, extra or {}))
    # fab zip: gerbers + drills only (what the fab needs) + assembly files
    zp = out / f"{project}_fab.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in gfiles.items():
            z.write(v, v.name)
    files["fab.zip"] = zp
    zp2 = out / f"{project}_assembly.zip"
    with zipfile.ZipFile(zp2, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out / "bom_jlcpcb.csv", "bom.csv")
        z.write(out / "cpl_jlcpcb.csv", "cpl.csv")
    files["assembly.zip"] = zp2
    return files
