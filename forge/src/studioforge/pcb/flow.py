"""End-to-end deterministic board build with automatic escalation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..model import Circuit
from . import analysis
from .board import Board
from .drc import DRCReport, run_drc
from .placement import PlaceOptions, place
from .planes import add_stitching, assign_planes, fill_zones
from .pours import plan_power_pours
from .router import RouteOptions, route
from .rules import Rules, rules_for


@dataclass
class BoardSpec:
    layers: int | None = None              # None = choose automatically
    width: float | None = None
    height: float | None = None
    profile: str | None = None
    seed: int = 1
    pitch: float | None = None
    copper_oz: float = 1.0
    max_layers: int = 6
    utilizations: tuple = (0.42, 0.32, 0.24)
    time_budget_s: float = 900.0
    rule_overrides: dict = field(default_factory=dict)


@dataclass
class BuildResult:
    board: Board
    drc: DRCReport
    analysis: dict
    log: list[str]
    success: bool


def suggest_layers(circuit: Circuit) -> int:
    n = len(circuit.components)
    hot = sum(1 for c in circuit.components.values() if c.power_w >= 0.5)
    high_i = any(a.current_a >= 5 for a in circuit.net_attrs.values())
    fine = any(p.part.category.startswith("ic.mcu") for p in circuit.components.values())
    if hot or high_i or n > 45 or fine:
        return 4
    return 2


def build_board(circuit: Circuit, spec: BoardSpec | None = None, log=print) -> BuildResult:
    spec = spec or BoardSpec()
    t_start = time.time()
    msgs: list[str] = []

    def say(s: str) -> None:
        msgs.append(s)
        if log:
            log(f"[{circuit.name}] {s}")
    layer_options = [spec.layers] if spec.layers else [l for l in (suggest_layers(circuit), 4, 6) if l <= spec.max_layers]
    layer_options = list(dict.fromkeys(layer_options))
    best: BuildResult | None = None
    utils = (spec.utilizations if not (spec.width and spec.height) else (1.0,))
    for layers in layer_options:
      for util, pwr_plane in [(u, v) for u in utils for v in ((True, False) if layers == 4 else (True,))]:
        if True:
            if time.time() - t_start > spec.time_budget_s:
                say("time budget exhausted")
                break
            over = dict(spec.rule_overrides)
            over["copper_oz_outer"] = spec.copper_oz
            rules = rules_for(layers, spec.profile, **over)
            opt = PlaceOptions(width=spec.width, height=spec.height, utilization=util, seed=spec.seed,
                               fixed_outline=bool(spec.width and spec.height))
            board = place(circuit, rules, opt)
            assign_planes(board, power_plane=pwr_plane)
            pours = plan_power_pours(board)
            say(f"{layers}L util={util}{'' if pwr_plane else ' (2nd inner layer = signal)'}: board {board.width}x{board.height} mm; "
                f"pours {pours or 'none'}; routing")
            route(board, RouteOptions(pitch=spec.pitch))
            fill_zones(board)
            if add_stitching(board):
                fill_zones(board)
            drc = run_drc(board)
            unrouted = board.stats.get("unrouted", [])
            ok = drc.clean and not unrouted
            say(f"   -> unrouted={len(unrouted)} drc[{drc.summary()}] {'OK' if ok else 'retry'}")
            res = BuildResult(board, drc, {}, msgs, ok)
            score = (len(unrouted), len(drc.errors), board.width * board.height)
            if best is None or score < best_score:
                best, best_score = res, score
            if ok:
                best.analysis = analysis.analyze(best.board)
                return best
    assert best is not None
    best.analysis = analysis.analyze(best.board)
    say("WARNING: no attempt produced a clean, fully routed board; returning the best attempt")
    return best
