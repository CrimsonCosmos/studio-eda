"""pcbnew helper, run with the SYSTEM python (the one that can `import pcbnew`), never imported by studioforge.

    /usr/bin/python3 pcbnew_bridge.py dsn spec.json     # .kicad_pcb -> Specctra DSN
    /usr/bin/python3 pcbnew_bridge.py ses spec.json     # .kicad_pcb + Freerouting .ses -> routed .kicad_pcb + result json

spec.json keys (all lengths in mm):
  pcb, dsn, ses, out_pcb, result      file paths
  rules   {track, clearance, via_dia, via_drill}              default net class
  classes [{name, track, nets:[...]}]                         wider-track net classes (same clearance/via as default)
  planes  {layer: net}                                        full-layer copper planes (layer becomes a power layer)
  outline [[x, y], ...]                                       plane polygon (board outline inset by edge clearance)
"""
import json
import sys

import pcbnew

MM = pcbnew.FromMM


def _layer(board, name):
    i = board.GetLayerID(name)
    if i < 0:
        raise SystemExit(f"unknown layer {name}")
    return i


def _prepare(board, spec):
    ds = board.GetDesignSettings()
    r = spec["rules"]
    ns = ds.m_NetSettings
    dflt = ns.m_DefaultNetClass
    for nc, w in [(dflt, r["track"])]:
        nc.SetClearance(MM(r["clearance"]))
        nc.SetTrackWidth(MM(w))
        nc.SetViaDiameter(MM(r["via_dia"]))
        nc.SetViaDrill(MM(r["via_drill"]))
    ds.m_MinClearance = MM(r["clearance"])
    ds.m_TrackMinWidth = MM(min(r["track"], r.get("min_track", r["track"])))
    ds.m_ViasMinSize = MM(r["via_dia"])
    ds.m_MinThroughDrill = MM(r["via_drill"])
    ds.m_CopperEdgeClearance = MM(r.get("edge_clearance", 0.3))
    for c in spec.get("classes", []):
        nc = pcbnew.NETCLASS(c["name"])
        nc.SetClearance(MM(r["clearance"]))
        nc.SetTrackWidth(MM(c["track"]))
        nc.SetViaDiameter(MM(r["via_dia"]))
        nc.SetViaDrill(MM(r["via_drill"]))
        for n in c["nets"]:
            nc.Add(n)
        ns.m_NetClasses[c["name"]] = nc
    board.SynchronizeNetsAndNetClasses(False) if hasattr(board, "SynchronizeNetsAndNetClasses") else None
    for layer, net in spec.get("planes", {}).items():
        lid = _layer(board, layer)
        board.SetLayerType(lid, pcbnew.LT_POWER)
        z = pcbnew.ZONE(board)
        z.SetLayer(lid)
        info = board.FindNet(net)
        z.SetNet(info)
        z.SetMinThickness(MM(0.2))
        z.SetLocalClearance(MM(r["clearance"]))
        ol = z.Outline()
        ol.NewOutline()
        for x, y in spec["outline"]:
            ol.Append(MM(x), MM(y))
        board.Add(z)


def cmd_dsn(spec):
    board = pcbnew.LoadBoard(spec["pcb"])
    _prepare(board, spec)
    if not pcbnew.ExportSpecctraDSN(board, spec["dsn"]):
        raise SystemExit("ExportSpecctraDSN failed")
    board.Save(spec["pcb"] + ".prepared")


def cmd_ses(spec):
    board = pcbnew.LoadBoard(spec["pcb"])
    _prepare(board, spec)
    if not pcbnew.ImportSpecctraSES(board, spec["ses"]):
        raise SystemExit("ImportSpecctraSES failed")
    board.Save(spec["out_pcb"])
    tracks, vias = [], []
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            v = pcbnew.Cast_to_PCB_VIA(t)
            p = v.GetPosition()
            vias.append({"x": pcbnew.ToMM(p.x), "y": pcbnew.ToMM(p.y), "net": v.GetNetname(),
                         "dia": pcbnew.ToMM(v.GetWidth()), "drill": pcbnew.ToMM(v.GetDrillValue())})
        elif cls == "PCB_TRACK":
            a, b = t.GetStart(), t.GetEnd()
            tracks.append({"layer": board.GetLayerName(t.GetLayer()), "net": t.GetNetname(),
                           "x1": pcbnew.ToMM(a.x), "y1": pcbnew.ToMM(a.y), "x2": pcbnew.ToMM(b.x), "y2": pcbnew.ToMM(b.y),
                           "width": pcbnew.ToMM(t.GetWidth())})
    json.dump({"tracks": tracks, "vias": vias}, open(spec["result"], "w"))


if __name__ == "__main__":
    spec = json.load(open(sys.argv[2]))
    {"dsn": cmd_dsn, "ses": cmd_ses}[sys.argv[1]](spec)
