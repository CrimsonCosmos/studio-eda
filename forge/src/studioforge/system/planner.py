"""Supervisor LLM: turns a one-line request into a SystemPlan by recursive decomposition.

The same SUPERVISOR_PROMPT is used (a) when this program drives Claude Code headlessly and
(b) as the instructions shipped in CLAUDE.md for the MCP workflow where Claude Code itself is the
supervisor and calls the deterministic tools. Claude never emits netlists or coordinates.
"""
from __future__ import annotations

import json

from ..blocks.registry import REGISTRY, _load, describe_blocks
from ..llm.base import LLM, LLMUnavailable, extract_json
from ..llm.claude_code import RESEARCH_TOOLS
from ..parts.db import PartsLibrary
from .plan import SystemPlan

SUPERVISOR_PROMPT = """\
You are the supervising electrical-systems engineer of an automated design pipeline. The user gives you a goal
("build me X, budget $Y"). You do NOT draw schematics, netlists, placements or routes: deterministic tools do that
(parametric block builders, a pin-mux solver, a placer, an autorouter, DRC, Gerber export). Your job is to
decompose the goal into subsystems, decide what to BUILD and what to BUY, and describe how the parts connect.

## BUY vs BUILD policy (important)
Unless the user explicitly asks for something to be designed from scratch, or building is NECESSARY to fit the budget,
or no suitable product exists, you must NOT build every subassembly yourself. Where it makes sense, BUY it:
  * If a commercial off-the-shelf product with the exact required specs exists (power supplies, DC-DC converters,
    battery packs / BMS / chargers, motor drivers / ESCs / servo-actuator modules, compute modules and dev kits, IMUs,
    cameras, radios, displays, sensors, cables / harnesses / connectors) plan it as a `purchased` item with its spec,
    a realistic unit cost and a source URL (research it with web search; set cost_is_estimate=false only if you saw a live price).
  * Build a custom board (blocks) only for application-specific glue, when no bought part meets the spec, when buying would
    exceed the budget, or when the build cost is clearly lower at the required quantity.
  * For every block you plan to build that has a plausible purchasable equivalent, attach `buy_alternative`
    {title, unit_cost_usd, url, supplier, spec}; the pipeline then compares the real computed build cost against it and
    picks the cheaper/safer option (set prefer="build" or "buy" only when the user's request or the budget forces it).
  * Tight budget -> buy proven modules for low-volume items and build in volume where the BOM is cheap; loose budget ->
    prefer proven off-the-shelf parts for reliability and time-to-first-prototype. Always keep a running cost estimate
    and stay under the budget; say so in `assumptions` if you had to trade capability for cost.

## How to work
1. Restate the requirements and list explicit assumptions (voltages, currents, bus types, quantities, environment).
2. Decompose recursively: system -> subsystems -> components, until every leaf is either a block from the catalog
   or a purchased item. Quantities matter: a board used in 28 joints is one `boards` entry with qty 28.
3. Describe interfaces ONLY as `links` between ports (see schema). Power links carry no numbers: the resolver computes
   current demand upstream (qty x duty) and voltage windows downstream, sizes converters, picks connectors and wire gauge,
   and assigns MCU pins. Supply facts about purchased items in their `ports` (voltage, rated current, kind, signals).
   `power_duty` (0..1) on a board is the fraction of its rated current drawn simultaneously across the fleet.
4. Port kinds: power, ground, can_logic, can_bus, uart, i2c, spi, pwm, analog, gpio, motor_phase.
   MCU peripheral ports are created automatically from the link (name the MCU endpoint e.g. `joint.mcu.pwm_drv`).
5. If no block fits a custom need, use block `custom_netlist` (parts by MPN/description + nets from a datasheet's typical
   application circuit); the parts library researches unknown parts. Never invent MPNs or pinouts.
6. Output ONLY one JSON object (SystemPlan), no prose.
"""

SCHEMA = """{
 "name": "short_snake_case", "request": "...", "budget_usd": 50000,
 "assumptions": ["..."],
 "boards": [ { "id": "joint", "title": "...", "qty": 28, "layers": 4, "copper_oz": 2.0, "power_duty": 0.15, "notes": "why custom",
     "blocks": [ {"id": "mcu", "block": "mcu_stm32f103", "params": {}, "buy_alternative": null, "prefer": "auto"} ] } ],
 "purchased": [ { "id": "battery", "title": "...", "category": "battery|power|actuator|compute|sensor|mechanical|harness|module", "qty": 1,
     "unit_cost_usd": 1400, "cost_is_estimate": true, "spec": "...", "url": "https://...", "supplier": "...",
     "ports": [ {"name": "out", "kind": "power", "direction": "out", "voltage": 48, "current_a": 60, "vmin": 39, "vmax": 54.6} ] } ],
 "links": [ {"a": "battery.out", "b": "joint.bus.src"}, {"a": "joint.mcu.pwm_drv", "b": "joint.drv.pwm"} ]
}"""


def catalog_text() -> str:
    _load()
    return json.dumps(describe_blocks(), indent=1)


def planning_prompt(request: str, budget: float | None, feedback: str = "") -> str:
    p = (f"REQUEST: {request}\nBUDGET (USD): {budget if budget is not None else 'not specified'}\n\n"
         f"BLOCK CATALOG (deterministic builders you may use; params marked auto are derived by the resolver from links):\n{catalog_text()}\n\n"
         f"PLAN SCHEMA:\n{SCHEMA}\n\n"
         f"Link endpoint format: `board.block.port` for board blocks (ports are listed in each block's catalog entry: "
         f"battery_input: src(in, external) vbus(out); buck_converter/ldo: vin(in) vout(out); mcu_stm32f103: vdd(in) + any port name you invent "
         f"for a peripheral link; can_node: logic vcc bus; motor_driver_3ph: vbus vgate vlogic pwm enable sense phase; i2c_sensor: i2c vcc) "
         f"and `item.port` for purchased items.\n")
    if feedback:
        p += f"\nYour previous plan failed validation; fix exactly these problems and return the full corrected plan:\n{feedback}\n"
    return p


class PlannerError(RuntimeError):
    pass


def _dry_run(lib: PartsLibrary, plan: SystemPlan) -> list[str]:
    from .resolver import Resolver
    try:
        res = Resolver(lib, plan, log=None).resolve()
    except Exception as e:
        return [f"resolution failed: {e}"]
    return [i.msg for i in res.issues if i.severity == "error"]


def plan_with_llm(request: str, budget: float | None, lib: PartsLibrary, llm: LLM, max_repairs: int = 3, log=print) -> SystemPlan:
    _load()
    feedback = ""
    for attempt in range(max_repairs + 1):
        res = llm.run(planning_prompt(request, budget, feedback), system=SUPERVISOR_PROMPT, tools=RESEARCH_TOOLS, json_output=True,
                      timeout=1800, budget_usd=5.0)
        data = res.data if isinstance(res.data, dict) else extract_json(res.text)
        try:
            plan = SystemPlan.from_dict(data)
            plan.request = plan.request or request
            if plan.budget_usd is None:
                plan.budget_usd = budget
        except Exception as e:
            feedback = f"plan JSON did not match the schema: {e}"
            continue
        errs = plan.validate(set(REGISTRY)) or _dry_run(lib, plan)
        if not errs:
            return plan
        feedback = "; ".join(errs[:12])
        if log:
            log(f"[planner] attempt {attempt + 1} rejected: {feedback[:300]}")
    raise PlannerError(f"supervisor could not produce a valid plan after {max_repairs + 1} attempts: {feedback}")


def plan_request(request: str, budget: float | None, lib: PartsLibrary, llm: LLM | None, log=print) -> SystemPlan:
    """LLM planner if available, otherwise a built-in archetype template (limited set)."""
    from . import templates
    if llm is not None:
        try:
            return plan_with_llm(request, budget, lib, llm, log=log)
        except LLMUnavailable as e:
            if log:
                log(f"[planner] LLM unavailable ({e}); trying built-in templates")
    plan = templates.plan_from_template(request, budget)
    if plan is None:
        raise PlannerError("no LLM backend is available and the request does not match a built-in template "
                           f"({', '.join(templates.TEMPLATES)}). Install/login to Claude Code (`claude`) or set ANTHROPIC_API_KEY.")
    return plan
