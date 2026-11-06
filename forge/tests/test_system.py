import json

import pytest

from studioforge.blocks.registry import REGISTRY, _load
from studioforge.llm.base import MockLLM
from studioforge.system import templates
from studioforge.system.build import build_system, decide_buy_vs_build
from studioforge.system.plan import SystemPlan
from studioforge.system.planner import PlannerError, SUPERVISOR_PROMPT, plan_request, plan_with_llm
from studioforge.system.resolver import Resolver


def joint_plan(motor_a=8.0, qty=1, duty=1.0, battery_a=30.0):
    return SystemPlan.from_dict({
        "name": "j", "budget_usd": 600,
        "boards": [{"id": "joint", "qty": qty, "power_duty": duty, "blocks": [
            {"id": "bus", "block": "battery_input"}, {"id": "buck12", "block": "buck_converter", "params": {"vout": 12, "rail": "+12V"}},
            {"id": "buck33", "block": "buck_converter", "params": {"vout": 3.3, "rail": "+3V3"}}, {"id": "mcu", "block": "mcu_stm32f103"},
            {"id": "can", "block": "can_node"}, {"id": "drv", "block": "motor_driver_3ph"}]}],
        "purchased": [{"id": "battery", "title": "pack", "unit_cost_usd": 300, "ports": [{"name": "out", "kind": "power", "direction": "out", "voltage": 48, "current_a": battery_a, "vmin": 39, "vmax": 54.6}]},
                      {"id": "motor", "title": "motor", "unit_cost_usd": 120, "qty": qty, "ports": [{"name": "phase", "kind": "motor_phase", "direction": "in", "current_a": motor_a, "signals": {"a": "a", "b": "b", "c": "c"}}]},
                      {"id": "canm", "title": "usb-can", "unit_cost_usd": 40, "ports": [{"name": "bus", "kind": "can_bus", "signals": {"canh": "canh", "canl": "canl"}}]}],
        "links": [{"a": "battery.out", "b": "joint.bus.src"}, {"a": "joint.bus.vbus", "b": "joint.buck12.vin"}, {"a": "joint.bus.vbus", "b": "joint.drv.vbus"},
                  {"a": "joint.buck12.vout", "b": "joint.buck33.vin"}, {"a": "joint.buck12.vout", "b": "joint.drv.vgate"},
                  {"a": "joint.buck33.vout", "b": "joint.mcu.vdd"}, {"a": "joint.buck33.vout", "b": "joint.can.vcc"}, {"a": "joint.buck33.vout", "b": "joint.drv.vlogic"},
                  {"a": "joint.mcu.pwm_drv", "b": "joint.drv.pwm"}, {"a": "joint.mcu.en_drv", "b": "joint.drv.enable"}, {"a": "joint.mcu.cur_sense", "b": "joint.drv.sense"},
                  {"a": "joint.mcu.can0", "b": "joint.can.logic"}, {"a": "joint.drv.phase", "b": "motor.phase"}, {"a": "joint.can.bus", "b": "canm.bus"}]})


def test_plan_validation_catches_bad_ids_and_links():
    _load()
    p = joint_plan()
    p.links.append(type(p.links[0])("joint.nope.x", "battery.out"))
    p.boards[0].blocks[0].block = "warp_drive"
    errs = p.validate(set(REGISTRY))
    assert any("warp_drive" in e for e in errs) and any("joint.nope" in e for e in errs)


def test_resolver_derives_block_params_from_neighbours(lib):
    res = Resolver(lib, joint_plan(motor_a=8.0), log=None).resolve()
    assert not res.errors(), [i.msg for i in res.errors()]
    P = res.params
    assert P[("joint", "drv")]["phase_current_a"] == 8.0                              # from the motor's rating
    assert P[("joint", "drv")]["vbus_max"] == 54.6                                     # from the battery window
    assert P[("joint", "buck12")]["vin_min"] == 39 and P[("joint", "buck12")]["vin_max"] == 54.6
    assert 11.0 < P[("joint", "buck33")]["vin_min"] < 12.0                             # from the 12 V rail tolerance
    assert P[("joint", "buck12")]["iout"] > 0.1                                        # sum of its consumers (+10%)
    kinds = {(r["name"], r["kind"]) for r in P[("joint", "mcu")]["requests"]}
    assert kinds == {("pwm_drv", "pwm"), ("en_drv", "gpio"), ("cur_sense", "adc"), ("can0", "can")}


def test_demand_scales_with_fleet_qty_and_duty(lib):
    one = Resolver(lib, joint_plan(qty=1, duty=1.0), log=None).resolve()
    fleet = Resolver(lib, joint_plan(qty=10, duty=0.2, battery_a=60.0), log=None).resolve()
    a1, a10 = one.demand["battery.out"], fleet.demand["battery.out"]
    assert a10 == pytest.approx(a1 * 10 * 0.2, rel=0.02)


def test_overloaded_source_is_an_error(lib):
    res = Resolver(lib, joint_plan(motor_a=40.0, battery_a=20.0), log=None).resolve()
    assert any("exceeds rated" in i.msg for i in res.errors())


def test_voltage_window_violation_detected(lib):
    p = joint_plan()
    # LDO with an explicit 4.5-5.5 V window fed straight from the 48 V pack: must be flagged, not silently accepted
    BI = type(p.boards[0].blocks[0])
    p.boards[0].blocks.append(BI("ldo5", "ldo", {"vin_min": 4.5, "vin_max": 5.5, "vout": 3.3, "iout": 0.1}))
    p.links.append(type(p.links[0])("joint.bus.vbus", "joint.ldo5.vin"))
    res = Resolver(lib, p, log=None).resolve()
    assert any("outside consumer window" in i.msg for i in res.errors())


def test_missing_part_triggers_research_path_offline(lib):
    from studioforge.parts.db import PartNotFound
    p = joint_plan()
    p.purchased[0].ports[0].update(voltage=72.0, vmin=60.0, vmax=80.0)       # needs an >=82 V TVS: not in the catalog
    with pytest.raises(PartNotFound):
        Resolver(lib, p, log=None).resolve()


def test_i2c_address_conflict_and_kind_mismatch(lib):
    p = joint_plan()
    p.boards[0].blocks += [type(p.boards[0].blocks[0])("imu1", "i2c_sensor"), type(p.boards[0].blocks[0])("imu2", "i2c_sensor")]
    Link = type(p.links[0])
    p.links += [Link("joint.mcu.bus_i2c", "joint.imu1.i2c"), Link("joint.mcu.bus_i2c", "joint.imu2.i2c")]
    res = Resolver(lib, p, log=None).resolve()
    assert any("I2C address conflict" in i.msg for i in res.errors())
    p2 = joint_plan()
    p2.links.append(type(p2.links[0])("joint.mcu.dbg", "joint.can.vcc"))
    res2 = Resolver(lib, p2, log=None).resolve()
    assert res2.errors()


def test_connectors_and_harness_follow_constraints(lib):
    res = Resolver(lib, joint_plan(qty=4, duty=0.3, battery_a=60.0), log=None).resolve()
    by = {(h["from"], h["to"]): h for h in res.harness}
    power = by[("battery.out", "joint.bus.src")]
    assert power["awg"] <= 14 and power["qty"] == 4                                    # wire gauge from demand
    assert any(bi.block == "connector_breakout" for bi in res.extra_blocks["joint"])   # CAN needs a connector on the board


def test_buy_vs_build_policy(lib):
    p = joint_plan()
    from studioforge.system.plan import BuyAlternative
    buck = p.boards[0].blocks[1]
    buck.buy_alternative = BuyAlternative("48-12V 3A module", 12.0, supplier="digikey")
    plan2, dec = decide_buy_vs_build(lib, p, log=None)
    assert any("BUY" in d for d in dec) and not any(b.id == "buck12" for b in plan2.boards[0].blocks)
    assert any(i.id == "joint_buck12" for i in plan2.purchased)
    assert "joint_buck12.vout" in [l.a for l in plan2.links] + [l.b for l in plan2.links]
    # user insists on building
    p3 = joint_plan()
    p3.boards[0].blocks[1].buy_alternative = BuyAlternative("module", 1.0)
    p3.boards[0].blocks[1].prefer = "build"
    plan3, dec3 = decide_buy_vs_build(lib, p3, log=None)
    assert any(b.id == "buck12" for b in plan3.boards[0].blocks) and any("BUILD (user preference)" in d for d in dec3)
    # expensive module -> keep building
    p4 = joint_plan()
    p4.boards[0].blocks[1].buy_alternative = BuyAlternative("pricey module", 500.0)
    plan4, dec4 = decide_buy_vs_build(lib, p4, log=None)
    assert any(b.id == "buck12" for b in plan4.boards[0].blocks)


def test_supervisor_prompt_states_buy_vs_build_policy():
    t = SUPERVISOR_PROMPT.lower()
    assert "buy" in t and "unless the user explicitly asks" in t and "necessary to fit the budget" in t and "off-the-shelf" in t
    assert "buy_alternative" in t


def test_humanoid_template_resolves_without_errors(lib):
    plan = templates.plan_from_template("build me a humanoid robot", 50000)
    assert plan.budget_usd == 50000 and {b.id for b in plan.boards} == {"joint_large", "joint_small"}
    res = Resolver(lib, plan, log=None).resolve()
    assert not res.errors(), [i.msg for i in res.errors()]
    assert res.demand["battery.out"] > 10


def test_end_to_end_system_without_pcbs(lib, tmp_path):
    sb = build_system(joint_plan(), lib, tmp_path, log=None, build_pcbs=False)
    assert (tmp_path / "SYSTEM.md").exists() and (tmp_path / "plan_resolved.json").exists()
    assert sb.cost["total_usd"] > 400 and sb.cost["within_budget"] in (True, False)
    assert "Cost vs budget" in (tmp_path / "SYSTEM.md").read_text()


def test_llm_planner_repair_loop(lib):
    bad = {"name": "x", "boards": [{"id": "b", "blocks": [{"id": "a", "block": "warp_drive"}]}], "links": []}
    good = joint_plan().to_dict()
    replies = iter([bad, good])
    llm = MockLLM(lambda p, kw: next(replies))
    plan = plan_with_llm("a joint", 600, lib, llm, log=None)
    assert plan.name == "j" and len(llm.calls) == 2
    assert "warp_drive" in llm.calls[1][0]                                              # the error is fed back
    assert llm.calls[0][1]["system"] == SUPERVISOR_PROMPT


def test_llm_planner_gives_up(lib):
    llm = MockLLM(lambda p, kw: {"name": "x", "boards": [{"id": "b", "blocks": [{"id": "a", "block": "nope"}]}]})
    with pytest.raises(PlannerError):
        plan_with_llm("x", None, lib, llm, max_repairs=1, log=None)


def test_offline_planner_falls_back_to_template_or_explains(lib):
    assert plan_request("humanoid robot", 50000, lib, None, log=None).name == "humanoid_robot"
    with pytest.raises(PlannerError):
        plan_request("quantum toaster", None, lib, None, log=None)
