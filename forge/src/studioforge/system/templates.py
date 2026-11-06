"""Offline archetype plans (used when no LLM is available, and as worked examples for the supervisor)."""
from __future__ import annotations

from .plan import SystemPlan

TEMPLATES = ("humanoid", "robot_arm", "motor_controller")


def _joint_board(bid: str, qty: int, phase_a: float, duty: float) -> dict:
    return {"id": bid, "title": f"BLDC joint controller ({phase_a:g} A phase)", "qty": qty, "layers": 4, "copper_oz": 2.0, "power_duty": duty,
            "notes": "Application-specific glue: FOC-capable MCU + 3-phase inverter + CAN node; no suitable off-the-shelf driver at this current/size/cost.",
            "blocks": [
                {"id": "bus", "block": "battery_input", "params": {}},
                {"id": "buck12", "block": "buck_converter", "params": {"vout": 12, "rail": "+12V"},
                 "buy_alternative": {"title": "48V->12V 3A isolated DC-DC module (e.g. Mean Well / Recom class)", "unit_cost_usd": 28.0, "supplier": "Digi-Key/Mouser",
                                     "spec": "36-75 V in, 12 V out, 3 A"}},
                {"id": "buck33", "block": "buck_converter", "params": {"vout": 3.3, "rail": "+3V3"}},
                {"id": "mcu", "block": "mcu_stm32f103", "params": {}},
                {"id": "can", "block": "can_node", "params": {}},
                {"id": "drv", "block": "motor_driver_3ph", "params": {}}]}


def _joint_links(bid: str, act: str, can_master: str) -> list[dict]:
    b = bid
    return [{"a": "battery.out", "b": f"{b}.bus.src"},
            {"a": f"{b}.bus.vbus", "b": f"{b}.buck12.vin"}, {"a": f"{b}.bus.vbus", "b": f"{b}.drv.vbus"},
            {"a": f"{b}.buck12.vout", "b": f"{b}.buck33.vin"}, {"a": f"{b}.buck12.vout", "b": f"{b}.drv.vgate"},
            {"a": f"{b}.buck33.vout", "b": f"{b}.mcu.vdd"}, {"a": f"{b}.buck33.vout", "b": f"{b}.can.vcc"},
            {"a": f"{b}.buck33.vout", "b": f"{b}.drv.vlogic"},
            {"a": f"{b}.mcu.pwm_drv", "b": f"{b}.drv.pwm"}, {"a": f"{b}.mcu.en_drv", "b": f"{b}.drv.enable"},
            {"a": f"{b}.mcu.cur_sense", "b": f"{b}.drv.sense"}, {"a": f"{b}.mcu.can0", "b": f"{b}.can.logic"},
            {"a": f"{b}.drv.phase", "b": f"{act}.phase"}, {"a": f"{b}.can.bus", "b": f"{can_master}.bus"}]


def humanoid_plan(budget: float | None) -> SystemPlan:
    budget = budget or 50000.0
    # joint count scales with budget so the plan stays feasible
    if budget >= 40000:
        n_large, n_small = 12, 16
    elif budget >= 25000:
        n_large, n_small = 8, 12
    else:
        n_large, n_small = 6, 8
    d = {
        "name": "humanoid_robot", "request": "humanoid robot", "budget_usd": budget,
        "assumptions": [f"{n_large + n_small} actuated degrees of freedom ({n_large} large 15 A joints, {n_small} small 6 A joints), 48 V (13S Li-ion) bus",
                        "joints seldom peak simultaneously: 15% fleet duty on the bus", "perception/planning runs on a purchased compute module (no custom compute board)",
                        "actuator/structure/battery prices are planning estimates (no live quote)", "firmware, mechanical design and software stack are out of scope"],
        "boards": [_joint_board("joint_large", n_large, 15, 0.15), _joint_board("joint_small", n_small, 6, 0.15)],
        "purchased": [
            {"id": "battery", "title": "48 V 13S Li-ion pack with BMS, ~1.2 kWh, 60 A continuous", "category": "battery", "qty": 1, "unit_cost_usd": 1500,
             "spec": "13S, 39-54.6 V, >=60 A discharge, integrated BMS + contactor", "supplier": "e.g. battery pack vendor (search needed)",
             "ports": [{"name": "out", "kind": "power", "direction": "out", "voltage": 48, "current_a": 60, "vmin": 39, "vmax": 54.6}]},
            {"id": "act_large", "title": "BLDC joint actuator, high torque (planetary, 15 A rms)", "category": "actuator", "qty": n_large, "unit_cost_usd": 850,
             "spec": "48 V, 15 A rms phase, hall/encoder", "ports": [{"name": "phase", "kind": "motor_phase", "direction": "in", "current_a": 15, "signals": {"a": "a", "b": "b", "c": "c"}}]},
            {"id": "act_small", "title": "BLDC joint actuator, medium torque (6 A rms)", "category": "actuator", "qty": n_small, "unit_cost_usd": 320,
             "spec": "48 V, 6 A rms phase", "ports": [{"name": "phase", "kind": "motor_phase", "direction": "in", "current_a": 6, "signals": {"a": "a", "b": "b", "c": "c"}}]},
            {"id": "dcdc_aux", "title": "48V->12V 10A DC-DC converter module (aux rail for compute/cameras)", "category": "power", "qty": 1, "unit_cost_usd": 70,
             "spec": "36-75 V in, 12 V / 10 A out, isolated or non-isolated",
             "ports": [{"name": "in", "kind": "power", "direction": "in", "voltage": 48, "current_a": 3.0, "vmin": 36, "vmax": 75},
                       {"name": "out", "kind": "power", "direction": "out", "voltage": 12, "current_a": 10, "vmin": 11.5, "vmax": 12.5}]},
            {"id": "compute", "title": "NVIDIA Jetson Orin NX 16GB developer kit (perception + planning)", "category": "compute", "qty": 1, "unit_cost_usd": 700,
             "spec": "9-20 V in, ~25 W", "ports": [{"name": "pwr", "kind": "power", "direction": "in", "voltage": 12, "current_a": 2.5, "vmin": 9, "vmax": 20}]},
            {"id": "can_master", "title": "USB-to-CAN adapter (isolated, 1 Mbit/s)", "category": "module", "qty": 1, "unit_cost_usd": 70, "spec": "CAN 2.0B, USB",
             "ports": [{"name": "bus", "kind": "can_bus", "direction": "bidir", "signals": {"canh": "canh", "canl": "canl"}}]},
            {"id": "cameras", "title": "Stereo depth cameras x2", "category": "sensor", "qty": 2, "unit_cost_usd": 350, "spec": "USB3 RGB-D", "ports": []},
            {"id": "imu", "title": "9-axis IMU module (torso)", "category": "sensor", "qty": 1, "unit_cost_usd": 40, "spec": "I2C/USB", "ports": []},
            {"id": "structure", "title": "Aluminium/CFRP frame, fasteners, covers (150-170 cm)", "category": "mechanical", "qty": 1, "unit_cost_usd": 4500, "spec": "custom machined; estimate", "ports": []},
            {"id": "charger", "title": "48 V battery charger", "category": "power", "qty": 1, "unit_cost_usd": 250, "spec": "13S, 5-10 A", "ports": []},
            {"id": "estop", "title": "Emergency stop + contactor + main fuse", "category": "power", "qty": 1, "unit_cost_usd": 220, "spec": "safety-rated", "ports": []}],
        "links": _joint_links("joint_large", "act_large", "can_master") + _joint_links("joint_small", "act_small", "can_master")
                 + [{"a": "battery.out", "b": "dcdc_aux.in"}, {"a": "dcdc_aux.out", "b": "compute.pwr"}]}
    # the second board shares battery; remove duplicate battery.out->bus link is fine (two consumers)
    return SystemPlan.from_dict(d)


def motor_controller_plan(budget: float | None) -> SystemPlan:
    d = {"name": "bldc_controller", "request": "single BLDC motor controller", "budget_usd": budget,
         "assumptions": ["48 V bus, 10 A rms motor"], "boards": [_joint_board("joint", 1, 10, 1.0)],
         "purchased": [{"id": "battery", "title": "48 V battery", "category": "battery", "qty": 1, "unit_cost_usd": 400,
                        "ports": [{"name": "out", "kind": "power", "direction": "out", "voltage": 48, "current_a": 30, "vmin": 39, "vmax": 54.6}]},
                       {"id": "act", "title": "BLDC motor 10 A", "category": "actuator", "qty": 1, "unit_cost_usd": 150,
                        "ports": [{"name": "phase", "kind": "motor_phase", "direction": "in", "current_a": 10, "signals": {"a": "a", "b": "b", "c": "c"}}]},
                       {"id": "can_master", "title": "USB-CAN adapter", "category": "module", "qty": 1, "unit_cost_usd": 40,
                        "ports": [{"name": "bus", "kind": "can_bus", "direction": "bidir", "signals": {"canh": "canh", "canl": "canl"}}]}],
         "links": _joint_links("joint", "act", "can_master")}
    return SystemPlan.from_dict(d)


def plan_from_template(request: str, budget: float | None):
    r = request.lower()
    if "humanoid" in r or "biped" in r:
        p = humanoid_plan(budget)
    elif "arm" in r and "robot" in r:
        p = humanoid_plan(budget)
        p.name = "robot_arm"
    elif "motor" in r and ("controller" in r or "driver" in r or "bldc" in r):
        p = motor_controller_plan(budget)
    else:
        return None
    p.request = request
    return p
