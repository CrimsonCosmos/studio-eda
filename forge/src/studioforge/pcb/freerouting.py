"""Alternative router: Freerouting (open-source Specctra autorouter) driven through KiCad's pcbnew.

Pipeline (everything happens in a temp dir, no network at runtime):

    Board --to_kicad_pcb--> .kicad_pcb --pcbnew_bridge dsn--> .dsn --(patched)--> Freerouting --> .ses
          <-- tracks/vias <-- .kicad_pcb <--pcbnew_bridge ses--+

Design decisions
  * Plane layers (4L+: GND on In1, power on In2) are KiCad zones that carry the plane; the DSN exports them as
    `power` layers.  Freerouting 2.1 does not fan pins out to planes by itself, so we place via stubs ourselves
    (SMD pads get a via + short track, through-hole pads connect to the plane by themselves) and Freerouting sees
    those nets as already connected.  Plane layers are never used for signal routing.
  * Outer-layer pours (2L GND, 4L outer GND) are NOT routed around: on 2L the pour net is routed like any net and
    fill_zones() pours the remainder; on 4L it is already on the plane.
  * High-current power pours (board.power_pours) are injected into the DSN as protected polygon wires of their net,
    so Freerouting sees those pads as connected and routes only the small remaining pads to the pour.  Other nets keep
    clear of the pour.  The pours themselves stay in board.power_pours (fill_zones re-emits them).
  * Net classes: nets that need more than the default width (IPC-2221 from net current, or explicit width) get a
    KiCad net class with that track width, set in pcbnew design settings before the DSN export.
  * Freerouting 2.1.0 ignores its pass limit, so the only hard bound is the wall-clock timeout (the SES is only written
    at the end).  On timeout/failure FreeroutingError is raised and the caller falls back to the builtin router.

Tool provisioning: `install_freerouting()` downloads the pinned release from github.com/freerouting/freerouting,
verifies SHA-256 and caches it under $FORGE_HOME/tools/ (never committed).  Requires Java >= 21 (2.1.0 is Java-21 bytecode;
2.2+ need Java 25).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .board import Board, Track, Via
from .rules import trace_width_for_current

FREEROUTING_VERSION = "2.1.0"
# Pinned: SHA-256 of freerouting-2.1.0.jar from the official GitHub release asset.  The GitHub API `digest` field
# could not be queried from the sandbox (API blocked), so this is the computed hash of the downloaded asset.
FREEROUTING_SHA256 = "2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def"
FREEROUTING_URL = f"https://github.com/freerouting/freerouting/releases/download/v{FREEROUTING_VERSION}/freerouting-{FREEROUTING_VERSION}.jar"
JAR_NAME = f"freerouting-{FREEROUTING_VERSION}.jar"
BRIDGE = Path(__file__).resolve().parent.parent / "kicad_backend" / "pcbnew_bridge.py"
KICAD_PYTHON = "/usr/bin/python3"


class FreeroutingError(RuntimeError):
    pass


@dataclass
class FreeroutingOptions:
    timeout_s: float = 600.0          # hard wall-clock limit for the Freerouting process
    threads: int = 1
    max_heap_mb: int = 2048
    max_class_width: float = 2.0      # widest net-class track handed to Freerouting (wider nets get this + warning)
    plane_stubs: bool = True
    keep_dir: str | None = None       # keep intermediate files here (debug)
    java: str = "java"
    jar: str | None = None
    verbose: bool = False


# ---------------------------------------------------------------------------------------------------- provisioning
def tools_dir() -> Path:
    home = os.environ.get("FORGE_HOME") or str(Path.home() / ".studioforge")
    return Path(home) / "tools"


def jar_path() -> Path:
    return tools_dir() / JAR_NAME


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jar_ok(p: Path | None = None) -> bool:
    p = p or jar_path()
    try:
        return p.is_file() and _sha256(p) == FREEROUTING_SHA256
    except OSError:
        return False


def _java_major(java: str = "java") -> int:
    exe = shutil.which(java)
    if not exe:
        return 0
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20).stderr
        m = re.search(r'version "(\d+)', out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def available(java: str = "java") -> bool:
    """java (>= 21) + verified jar + KiCad's python (pcbnew) all present."""
    if _java_major(java) < 21 or not jar_ok() or not BRIDGE.is_file() or not Path(KICAD_PYTHON).exists():
        return False
    try:
        return subprocess.run([KICAD_PYTHON, "-c", "import pcbnew"], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def install_freerouting(timeout_s: float = 300.0) -> Path:
    """Download the pinned official release asset into $FORGE_HOME/tools and verify its SHA-256."""
    import urllib.request
    dest = jar_path()
    if jar_ok(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(FREEROUTING_URL, timeout=timeout_s) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if _sha256(tmp) != FREEROUTING_SHA256:
        tmp.unlink(missing_ok=True)
        raise FreeroutingError("downloaded Freerouting jar does not match the pinned SHA-256")
    tmp.replace(dest)
    return dest


# ---------------------------------------------------------------------------------------------------- helpers
def _net_width(board: Board, net: str, base: float, cap: float) -> float:
    a = board.circuit.net_attrs.get(net)
    if a is None:
        return base
    if any(z.net == net for z in board.power_pours):
        return base                                    # the pour carries the current
    w = a.width or (trace_width_for_current(a.current_a, board.rules.temp_rise_c, board.rules.copper_oz_outer) if a.current_a > 0 else 0.0)
    w = round(max(base, min(w, cap)), 2)
    return w


def _plane_stubs(board: Board) -> int:
    """Via + stub for every SMD pad on a plane net (pads are on F.Cu; through-hole pads reach the plane themselves)."""
    r = board.rules
    planes = set(board.plane_nets.values())
    if not planes:
        return 0
    pads = board.pads()
    clr = r.clearance + 0.02
    vr = r.via_dia / 2
    edge = board.outline.buffer(-(r.edge_clearance + vr + 0.02))
    obst: list = []                                       # (net, geom) for everything a new via/stub must avoid
    for p in pads:
        obst.append((p.net, p.geom()))
    for p in pads:
        if p.thru and not p.plated:
            obst.append(("~npth", Point(p.x, p.y).buffer(p.drill / 2)))
    for pz in board.power_pours:
        for g in pz.polygons:
            obst.append((pz.net, g))
    added = 0
    todo = [p for p in pads if p.net in planes and not p.thru]
    # pads with the most neighbours first (they have the fewest options)
    for p in todo:
        placed = False
        cand = []
        inside = min(p.w, p.h) / 2 - vr - 0.05
        if inside >= 0.0 and min(p.w, p.h) >= r.via_dia + 0.15:
            cand.append((p.x, p.y, 0.0))
        base = max(p.w, p.h) / 2 + vr + clr
        step = max(0.15, r.track)
        for k in range(0, 14):
            rad = base + k * step
            n = 24
            for i in range(n):
                ang = 2 * math.pi * i / n
                cand.append((p.x + rad * math.cos(ang), p.y + rad * math.sin(ang), rad))
        by_net_geoms = [(n, g) for n, g in obst if n != p.net]
        tree = STRtree([g for _, g in by_net_geoms]) if by_net_geoms else None
        for (x, y, rad) in cand:
            if not edge.contains(Point(x, y)):
                continue
            via = Point(x, y).buffer(vr + clr, 12)
            stub = LineString([(p.x, p.y), (x, y)]).buffer(r.track / 2 + clr, 4) if rad > 0 else None
            probe = via if stub is None else unary_union([via, stub])
            idx = tree.query(probe) if tree is not None else []
            if any(by_net_geoms[j][1].intersects(probe) for j in idx):
                continue
            # own-net vias keep hole-to-hole spacing
            if any(v.net != "" and math.hypot(v.x - x, v.y - y) < r.via_dia + 0.05 for v in board.vias):
                continue
            if rad > 0:
                board.tracks.append(Track("F.Cu", p.net, round(p.x, 4), round(p.y, 4), round(x, 4), round(y, 4), r.track))
            board.vias.append(Via(round(x, 4), round(y, 4), p.net, r.via_dia, r.via_drill))
            obst.append((p.net, Point(x, y).buffer(vr)))
            if rad > 0:
                obst.append((p.net, LineString([(p.x, p.y), (x, y)]).buffer(r.track / 2)))
            added += 1
            placed = True
            break
        if not placed:
            board.warnings.append(f"freerouting: no room for a plane via near {p.ref}.{p.number} ({p.net})")
    return added


def _poly_wire(layer: str, net: str, poly) -> str:
    pts = list(poly.exterior.coords)[:-1]
    flat = "  ".join(f"{x * 1000:.0f} {-y * 1000:.0f}" for x, y in pts)
    return f'    (wire (polygon {layer} 0  {flat}) (net "{net}") (type protect))'


def _patch_dsn(text: str, board: Board) -> str:
    # 1) everything already in the wiring section (our plane stubs/vias) is fixed
    text = text.replace("(type route)", "(type protect)")
    # 2) power pours as protected copper of their net
    wires = []
    for z in board.power_pours:
        for g in z.polygons:
            geoms = g.geoms if hasattr(g, "geoms") else [g]
            for gg in geoms:
                if not gg.is_empty and gg.geom_type == "Polygon":
                    wires.append(_poly_wire(z.layer, z.net, gg))
    if wires:
        i = text.find("(wiring")
        if i < 0:
            raise FreeroutingError("DSN has no wiring section")
        j = text.find("\n", i)
        text = text[:j + 1] + "\n".join(wires) + "\n" + text[j + 1:]
    return text


def parse_ses(text: str) -> dict:
    """Specctra session -> {"tracks": [...], "vias": [...]} in mm / board coordinates (y flipped back)."""
    toks = re.findall(r'"[^"]*"|\(|\)|[^\s()]+', text)
    pos = 0

    def node():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t != "(":
            return t.strip('"')
        out = []
        while toks[pos] != ")":
            out.append(node())
        pos += 1
        return out
    root = node()

    def find(n, head):
        return [c for c in n if isinstance(c, list) and c and c[0] == head]
    routes = find(root, "routes")
    if not routes:
        raise FreeroutingError("SES has no routes section")
    routes = routes[0]
    res = find(routes, "resolution")
    unit, per = (res[0][1], float(res[0][2])) if res else ("um", 10.0)
    to_mm = {"um": 1e-3, "mm": 1.0, "mil": 0.0254, "inch": 25.4, "cm": 10.0}[unit] / per
    pads = {}
    lib = find(routes, "library_out")
    for ps in (find(lib[0], "padstack") if lib else []):
        m = re.match(r"Via\[[^\]]*\]_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um", ps[1])
        if m:
            pads[ps[1]] = (float(m.group(1)) / 1000, float(m.group(2)) / 1000)
    tracks, vias = [], []
    for nout in find(routes, "network_out"):
        for net in find(nout, "net"):
            name = net[1]
            for w in find(net, "wire"):
                for path in find(w, "path"):
                    layer, width = path[1], float(path[2]) * to_mm
                    pts = [(float(path[i]) * to_mm, -float(path[i + 1]) * to_mm) for i in range(3, len(path) - 1, 2)]
                    for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
                        tracks.append({"layer": layer, "net": name, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": width})
            for v in find(net, "via"):
                dia, drill = pads.get(v[1], (0.0, 0.0))
                vias.append({"x": float(v[2]) * to_mm, "y": -float(v[3]) * to_mm, "net": name, "dia": dia, "drill": drill})
    return {"tracks": tracks, "vias": vias}


def _run(cmd: list[str], timeout: float, cwd: str, what: str, env=None) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise FreeroutingError(f"{what} timed out after {timeout:.0f}s") from e
    if cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or "")[-600:]
        raise FreeroutingError(f"{what} failed (rc={cp.returncode}): {tail}")
    return cp


def _settings_json() -> str:
    return json.dumps({
        "profile": {"id": "00000000-0000-0000-0000-000000000000", "email": "", "allow_telemetry": False, "allow_contact": False},
        "gui": {"enabled": False},
        "router": {"max_threads": 1, "optimizer": {"max_threads": 1}},
        "usage_and_diagnostic_data": {"disable_analytics": True},
        "feature_flags": {"logging": False, "save_jobs": False},
        "api_server": {"enabled": False},
    })


# ---------------------------------------------------------------------------------------------------- main entry
def route_with_freerouting(board: Board, opt: FreeroutingOptions | None = None) -> Board:
    """Route `board` (placed, planes assigned, pours planned) with Freerouting; fills board.tracks / vias / stats."""
    from ..export.kicad import to_kicad_pcb
    from .drc import DRCReport, _connectivity
    from .planes import fill_zones

    o = opt or FreeroutingOptions()
    jar = Path(o.jar) if o.jar else jar_path()
    if _java_major(o.java) < 21:
        raise FreeroutingError("Java >= 21 not found")
    if not jar_ok(jar):
        raise FreeroutingError(f"Freerouting jar missing or SHA-256 mismatch: {jar} (see install_freerouting())")
    if not BRIDGE.is_file():
        raise FreeroutingError(f"pcbnew bridge missing: {BRIDGE}")
    t0 = time.time()
    r = board.rules
    board.tracks.clear()
    board.vias.clear()
    saved_zones = board.zones
    board.zones = []                                       # zones are produced by the bridge / fill_zones, not exported
    work = Path(o.keep_dir) if o.keep_dir else Path(tempfile.mkdtemp(prefix="forge_fr_"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        n_stub = _plane_stubs(board) if o.plane_stubs else 0
        pcb = work / "in.kicad_pcb"
        pcb.write_text(to_kicad_pcb(board))
        # net classes by required width
        classes: dict[float, list[str]] = {}
        capped = []
        for net, pins in board.circuit.nets.items():
            if len(pins) < 2:
                continue
            w = _net_width(board, net, r.track, 1e9)
            if w > o.max_class_width:
                capped.append(net)
                w = o.max_class_width
            if w > r.track + 1e-6:
                classes.setdefault(w, []).append(net)
        if capped:
            board.warnings.append(f"freerouting: nets {capped} need more than {o.max_class_width} mm; routed at {o.max_class_width} mm")
        inset = board.outline.buffer(-r.edge_clearance)
        spec = {
            "pcb": str(pcb), "dsn": str(work / "in.dsn"), "ses": str(work / "out.ses"),
            "rules": {"track": r.track, "min_track": r.min_track, "clearance": r.clearance, "via_dia": r.via_dia,
                      "via_drill": r.via_drill, "edge_clearance": r.edge_clearance},
            "classes": [{"name": f"w{w:g}", "track": w, "nets": nets} for w, nets in sorted(classes.items())],
            "planes": dict(board.plane_nets),
            "outline": [list(p) for p in inset.exterior.coords[:-1]],
        }
        spec_p = work / "spec.json"
        spec_p.write_text(json.dumps(spec))
        _run([KICAD_PYTHON, str(BRIDGE), "dsn", str(spec_p)], 120, str(work), "pcbnew DSN export")
        dsn_p = work / "in.dsn"
        dsn_p.write_text(_patch_dsn(dsn_p.read_text(), board))

        tmpd = work / "jtmp"                               # isolated java.io.tmpdir => private freerouting.json / log
        (tmpd / "freerouting").mkdir(parents=True, exist_ok=True)
        (tmpd / "freerouting" / "freerouting.json").write_text(_settings_json())
        cmd = [shutil.which(o.java) or o.java, f"-Xmx{o.max_heap_mb}m", "-Djava.awt.headless=true", f"-Djava.io.tmpdir={tmpd}",
               "-jar", str(jar), "-de", str(dsn_p), "-do", spec["ses"], "-mt", str(o.threads), "-da"]
        env = dict(os.environ)
        env["JAVA_TOOL_OPTIONS"] = ""
        t1 = time.time()
        cp = subprocess.Popen(cmd, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        try:
            out, _ = cp.communicate(timeout=o.timeout_s)
        except subprocess.TimeoutExpired:
            cp.kill()
            cp.communicate()
            raise FreeroutingError(f"Freerouting timed out after {o.timeout_s:.0f}s")
        t_fr = time.time() - t1
        (work / "freerouting.log").write_text(out or "")
        if o.verbose:
            print((out or "")[-800:])
        if cp.returncode != 0 or not Path(spec["ses"]).exists():
            raise FreeroutingError(f"Freerouting failed (rc={cp.returncode}): {(out or '')[-400:]}")
        res = parse_ses(Path(spec["ses"]).read_text())
    finally:
        board.zones = saved_zones
        if not o.keep_dir:
            pass                                           # cleaned below (after parsing)
    valid_layers = set(board.layers)
    board.tracks = [Track(t["layer"], t["net"], round(t["x1"], 4), round(t["y1"], 4), round(t["x2"], 4), round(t["y2"], 4), round(t["width"], 4))
                    for t in res["tracks"] if t["net"] and t["layer"] in valid_layers and (t["x1"], t["y1"]) != (t["x2"], t["y2"])]
    board.vias = [Via(round(v["x"], 4), round(v["y"], 4), v["net"], round(v["dia"], 4), round(v["drill"], 4)) for v in res["vias"] if v["net"]]
    # a signal track on a plane layer would be a bug (layers are power type); drop defensively
    board.tracks = [t for t in board.tracks if t.layer not in board.plane_nets]

    # which nets are still not connected? (pour/plane zones count, exactly as DRC sees them)
    fill_zones(board)
    rep = DRCReport()
    _connectivity(board, rep)
    unrouted = sorted({m.group(1) for v in rep.violations if v.kind == "unconnected" for m in [re.match(r"net (.+?):", v.msg)] if m})
    board.stats.update({"route_time_s": round(time.time() - t0, 2), "freerouting_time_s": round(t_fr, 2), "router": "freerouting",
                        "freerouting_version": FREEROUTING_VERSION, "plane_stubs": n_stub, "unrouted": unrouted})
    for n in unrouted:
        board.warnings.append(f"UNROUTED net {n}")
    if not o.keep_dir:
        shutil.rmtree(work, ignore_errors=True)
    return board
