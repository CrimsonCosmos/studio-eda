"""pcbnew helper, run with the SYSTEM python (the one that can `import pcbnew`), never imported by studioforge.

    /usr/bin/python3 pcbnew_bridge.py dsn spec.json     # .kicad_pcb -> Specctra DSN
(KiCad 7's python API cannot import a .ses outside the GUI - ImportSpecctraSES works on the GUI board only - so the
 Freerouting result is parsed by studioforge.pcb.freerouting.parse_ses instead.)

spec.json keys (all lengths in mm):
  pcb, dsn                            file paths
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
            ni = board.FindNet(n)
            if ni is not None:
                ni.SetNetClass(nc)
        ns.m_NetClasses[c["name"]] = nc
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


if __name__ == "__main__":
    spec = json.load(open(sys.argv[2]))
    {"dsn": cmd_dsn}[sys.argv[1]](spec)
