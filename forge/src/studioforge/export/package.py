"""Write the complete manufacturing + review package for a built board."""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from ..pcb.flow import BuildResult
from . import bom as bomx
from .. import kicad_backend
from . import gerber, kicad, svg
from .report import kicad_drc_section, render_report


def write_package(res: BuildResult, outdir: str | Path, project: str, extra: dict | None = None,
                  use_kicad: bool | None = None) -> dict[str, Path]:
    """`use_kicad`: None = auto (on when kicad-cli is on PATH and FORGE_KICAD != "0"), True/False to force."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    b = res.board
    c = b.circuit
    files: dict[str, Path] = {}
    gdir = out / "gerber"
    extra = dict(extra or {})
    if use_kicad is None:
        use_kicad = kicad_backend.available() and os.environ.get("FORGE_KICAD", "1") != "0"
    gfiles: dict[str, Path] | None = None
    if use_kicad:
        try:      # KiCad is the authoritative DRC + gerber/drill/position backend
            pcb_p = out / f"{project}.kicad_pcb"
            pcb_p.write_text(kicad.to_kicad_pcb(b, project))
            kicad_backend.write_kicad_pro(out / f"{project}.kicad_pro", b.rules)
            kfiles = kicad_backend.export_fab(pcb_p, out, project)
            rep = kicad_backend.run_drc(pcb_p, report_path=out / "kicad_drc.json")
            files["kicad_drc.json"] = out / "kicad_drc.json"
            extra["KiCad DRC (authoritative)"] = kicad_drc_section(rep)
            gfiles = {k.split(":", 1)[1]: v for k, v in kfiles.items() if k.startswith("gerber:")}
            for k, v in kfiles.items():
                files[k if k.startswith("gerber:") else f"kicad:{k}"] = v
        except Exception as e:                     # fall back to the built-in writers, say so in the report
            extra["KiCad DRC (authoritative)"] = f"KiCad backend failed ({e}); built-in Gerber writer and DRC used instead."
            gfiles = None
    if gfiles is None:
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
    if not (out / f"{project}.kicad_pro").exists():
        kicad_backend.write_kicad_pro(out / f"{project}.kicad_pro", b.rules)
    w(f"{project}.net", kicad.to_kicad_netlist(c))
    w("board_top.svg", svg.board_svg(b, [b.layers[0]] if len(b.layers) > 2 else b.layers))
    for layer in b.layers:
        w(f"layers/{layer.replace('.', '_')}.svg", svg.board_svg(b, [layer], zones=True, silk=layer == "F.Cu"))
    w("design.json", json.dumps(c.to_dict(), indent=1, default=str))
    w("REPORT.md", render_report(res, project, extra))
    # fab zip: gerbers + drills only (what the fab needs) + assembly files
    zp = out / f"{project}_fab.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in gfiles.items():
            if v.suffix not in (".gbrjob",) and "drl_map" not in v.name:
                z.write(v, v.name)
    files["fab.zip"] = zp
    zp2 = out / f"{project}_assembly.zip"
    with zipfile.ZipFile(zp2, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out / "bom_jlcpcb.csv", "bom.csv")
        z.write(out / "cpl_jlcpcb.csv", "cpl.csv")
    files["assembly.zip"] = zp2
    return files
