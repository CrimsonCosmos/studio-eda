"""Three-phase BLDC/PMSM inverter stage: gate drivers, MOSFET bridge, shunt current sensing."""
from __future__ import annotations

import math

from ..constraints import Cluster, Decouple, Loop, Separate
from ..model import Circuit, fmt_si
from ..parts.db import PartsLibrary
from .registry import Param, block
from .util import e24, e96


@block("motor_driver_3ph",
       "Three-phase half-bridge motor driver: per phase one half-bridge gate driver (IR2104 class), two power MOSFETs, bootstrap diode/cap, "
       "gate resistors, low-side shunt with difference amplifier (bidirectional current sense referenced to mid-supply), bus capacitors, "
       "phase terminal. FETs, bootstrap capacitance, gate resistors, shunt and amplifier gain are computed from bus voltage and phase current.",
       [Param("vbus_max", "number", 54.6, "max DC bus voltage (V)"), Param("vbus_nom", "number", 48.0, "nominal bus (V)"),
        Param("phase_current_a", "number", 10.0, "continuous phase current, RMS (A)"), Param("pwm_hz", "number", 20000.0, "PWM frequency (Hz)"),
        Param("vgate", "number", 12.0, "gate-drive supply (V)"), Param("vlogic", "number", 3.3, "logic/analog supply (V)"),
        Param("sense_gain", "number", 20.0, "current-sense amplifier gain (V/V)"), Param("shunt_mohm", "number", 2.0, "shunt resistance (mOhm)")],
       "motor", auto=("phase_current_a", "vbus_max", "vbus_nom"))
def motor_driver_3ph(lib: PartsLibrary, vbus_max=54.6, vbus_nom=48.0, phase_current_a=10.0, pwm_hz=20000.0, vgate=12.0,
                     vlogic=3.3, sense_gain=20.0, shunt_mohm=2.0) -> Circuit:
    c = Circuit("motor_driver_3ph")
    I = phase_current_a
    fet = lib.require("mosfet", {"vds_max": (">=", vbus_max * 1.5), "id_max": (">=", I * 3.0), "channel": ("==", "n")}, tags=["motor"],
                      need=f"N-channel power MOSFET, Vds >= {vbus_max * 1.5:.0f} V, Id >= {I * 3:.0f} A, Rds(on) as low as possible, TO-220 or DPAK, logic drive from 10-12 V gate driver")
    drv = lib.require("ic.gatedriver", {"vs_max": (">=", vbus_max * 1.15), "vcc_max": (">=", vgate), "vcc_min": ("<=", vgate)}, tags=["motor"],
                      need=f"half-bridge gate driver, bootstrap high side, VS >= {vbus_max * 1.15:.0f} V, VCC {vgate:g} V")
    amp = lib.get("LMV321IDBVR")
    rds = fet.params["rds_on"]
    qg, qgd = fet.params["qg_nc"] * 1e-9, fet.params.get("qgd_nc", fet.params["qg_nc"] * 0.3) * 1e-9
    idrv_src, idrv_snk = drv.params["io_source_a"], drv.params["io_sink_a"]
    tsw = qgd / idrv_src + qgd / idrv_snk
    p_cond = 0.5 * I ** 2 * rds * 1.5
    p_sw = 0.5 * vbus_nom * 0.9 * I * tsw * pwm_hz * 0.5
    p_fet = p_cond + p_sw
    # bootstrap capacitor: Cbs >= 10*Qg/dV (dV = 1 V droop allowed)
    cbs_req = 10 * qg / 1.0
    cbs = 2.2e-6 if cbs_req <= 2.2e-6 else 4.7e-6
    rg = 10.0
    rs = shunt_mohm / 1000.0
    v_sense_pk = 1.414 * I * rs * sense_gain
    c.set_net("GND", role="ground", current_a=I * 1.2)
    c.set_net("BUS", role="power", current_a=I, voltage=vbus_max)
    c.set_net("VGATE", role="power", current_a=0.05, voltage=vgate)
    c.set_net("VLOGIC", role="power", current_a=0.02, voltage=vlogic)
    for n in ("EN", "SD"):
        c.set_net(n)
    q_hi, q_lo, drivers, shunts, amps = {}, {}, {}, {}, {}
    cbus_refs = []
    # reference buffer for amplifiers
    rr1, rr2 = c.add(lib.resistor(10e3, "0402"), prefix="R", block="motor"), c.add(lib.resistor(10e3, "0402"), prefix="R", block="motor")
    ub = c.add(amp, prefix="U", block="motor")
    c.connect("VLOGIC", f"{rr1.ref}.1", f"{ub.ref}.5")
    c.connect("VREF_RAW", f"{rr1.ref}.2", f"{rr2.ref}.1", f"{ub.ref}.1")
    c.connect("GND", f"{rr2.ref}.2", f"{ub.ref}.2")
    c.connect("VREF", f"{ub.ref}.3", f"{ub.ref}.4")
    for nm in ("VREF_RAW", "VREF"):
        cc = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C", block="motor")
        c.connect(nm, f"{cc.ref}.1")
        c.connect("GND", f"{cc.ref}.2")
    c.set_net("VREF", sensitive=True)
    c.set_net("VREF_RAW", sensitive=True)
    c.constrain(Cluster([ub.ref, rr1.ref, rr2.ref], 1.0, 10.0))
    for x in "ABC":
        X = x
        qh = c.add(fet, prefix="Q", block=f"motor_{x}", power_w=p_fet)
        ql = c.add(fet, prefix="Q", block=f"motor_{x}", power_w=p_fet)
        u = c.add(drv, prefix="U", block=f"motor_{x}", power_w=0.08)
        d_bs = c.add(lib.get("ES1J"), prefix="D", block=f"motor_{x}")
        c_bs = c.add(lib.mlcc(cbs, 25), prefix="C", block=f"motor_{x}")
        rgh, rgl = c.add(lib.resistor(rg, "0603"), prefix="R", block=f"motor_{x}"), c.add(lib.resistor(rg, "0603"), prefix="R", block=f"motor_{x}")
        rsh, rsl = c.add(lib.resistor(100e3, "0603"), prefix="R", block=f"motor_{x}"), c.add(lib.resistor(100e3, "0603"), prefix="R", block=f"motor_{x}")
        sh = c.add(lib.shunt(shunt_mohm, max(1.0, I * I * rs * 2), "2512"), prefix="R", block=f"motor_{x}", power_w=I * I * rs)
        cv1, cv2 = c.add(lib.mlcc(4.7e-6, 25), prefix="C", block=f"motor_{x}"), c.add(lib.capacitor(100e-9, "0402", 25), prefix="C", block=f"motor_{x}")
        cbus = c.add(lib.mlcc(2.2e-6, 100), prefix="C", block=f"motor_{x}")
        pu = lambda n: f"{u.ref}.{drv.pin(n).number}"
        # driver
        c.connect("VGATE", pu("VCC"), f"{d_bs.ref}.2", f"{cv1.ref}.1", f"{cv2.ref}.1")
        c.connect("GND", pu("COM"), f"{cv1.ref}.2", f"{cv2.ref}.2")
        c.connect(f"PWM_{X}", pu("IN"))
        c.connect("SD", pu("~SD"))
        c.connect(f"VB_{X}", pu("VB"), f"{d_bs.ref}.1", f"{c_bs.ref}.1")
        c.connect(f"PH_{X}", pu("VS"), f"{c_bs.ref}.2", f"{qh.ref}.3", f"{ql.ref}.2")
        c.connect(f"HO_{X}", pu("HO"), f"{rgh.ref}.1")
        c.connect(f"LO_{X}", pu("LO"), f"{rgl.ref}.1")
        # power stage
        c.connect(f"GH_{X}", f"{rgh.ref}.2", f"{qh.ref}.1", f"{rsh.ref}.1")
        c.connect(f"GL_{X}", f"{rgl.ref}.2", f"{ql.ref}.1", f"{rsl.ref}.1")
        c.connect(f"PH_{X}", f"{rsh.ref}.2")
        c.connect(f"SH_{X}", f"{ql.ref}.3", f"{rsl.ref}.2", f"{sh.ref}.1")
        c.connect("GND", f"{sh.ref}.2")
        c.connect("BUS", f"{qh.ref}.2", f"{cbus.ref}.1")
        c.connect("GND", f"{cbus.ref}.2")
        cbus_refs.append(cbus.ref)
        c.set_net(f"PH_{X}", role="power", current_a=I, noisy=True)
        c.set_net(f"SH_{X}", role="power", current_a=I)
        for nm in (f"HO_{X}", f"LO_{X}", f"GH_{X}", f"GL_{X}", f"VB_{X}"):
            c.set_net(nm, noisy=True)
        # sense amplifier (difference amp referenced to VREF):  Vout = Vref + G*(V(SH)-V(GND))
        ua = c.add(amp, prefix="U", block=f"motor_{x}")
        r1 = c.add(lib.resistor(1e3, "0402"), prefix="R", block=f"motor_{x}")
        r3 = c.add(lib.resistor(1e3, "0402"), prefix="R", block=f"motor_{x}")
        r2 = c.add(lib.resistor(e96(1e3 * sense_gain), "0402"), prefix="R", block=f"motor_{x}")
        r4 = c.add(lib.resistor(e96(1e3 * sense_gain), "0402"), prefix="R", block=f"motor_{x}")
        cd = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C", block=f"motor_{x}")
        c.connect(f"AMPP_{X}", f"{ua.ref}.1", f"{r3.ref}.2", f"{r4.ref}.1")
        c.connect(f"SH_{X}", f"{r3.ref}.1")
        c.connect("VREF", f"{r4.ref}.2")
        c.connect(f"AMPN_{X}", f"{ua.ref}.3", f"{r1.ref}.2", f"{r2.ref}.1")
        c.connect("GND", f"{r1.ref}.1", f"{ua.ref}.2", f"{cd.ref}.2")
        c.connect(f"SENSE_{X}", f"{ua.ref}.4", f"{r2.ref}.2")
        c.connect("VLOGIC", f"{ua.ref}.5", f"{cd.ref}.1")
        for nm in (f"AMPP_{X}", f"AMPN_{X}", f"SENSE_{X}"):
            c.set_net(nm, sensitive=True)
        # placement physics
        c.constrain(Loop([qh.ref, ql.ref, cbus.ref, sh.ref], f"half-bridge-{x}", 6.0))
        c.constrain(Decouple(cbus.ref, qh.ref, f"{qh.ref}.2", 6.0, 8.0))
        c.constrain(Decouple(cv1.ref, u.ref, pu("VCC"), 4.0, 5.0))
        c.constrain(Decouple(cv2.ref, u.ref, pu("VCC"), 3.0, 6.0))
        c.constrain(Cluster([u.ref, d_bs.ref, c_bs.ref, rgh.ref, rgl.ref, rsh.ref, rsl.ref], 2.0, 14.0))
        c.constrain(Cluster([qh.ref, ql.ref, u.ref], 2.0, 24.0))
        c.constrain(Cluster([ql.ref, sh.ref, r3.ref, r1.ref], 2.0, 14.0))
        q_hi[x], q_lo[x], drivers[x], shunts[x], amps[x] = qh.ref, ql.ref, u.ref, sh.ref, ua.ref
        c.constrain(Separate([qh.ref, ql.ref], [ua.ref], 12.0, 3.0, "emi"))
    # DC-link bulk capacitance (ripple current sized): 2 electrolytic
    cap_v = 100 if vbus_max > 50 else 63
    bulk = []
    for i in range(2):
        cp = c.add(lib.electrolytic(330e-6 if cap_v == 100 else 470e-6, cap_v), prefix="C", block="motor_bus")
        c.connect("BUS", f"{cp.ref}.1")
        c.connect("GND", f"{cp.ref}.2")
        bulk.append(cp.ref)
    c.constrain(Cluster(bulk + cbus_refs, 1.0, 40.0))
    # EN pull-down so the bridge is off until the MCU drives it
    ren = c.add(lib.resistor(10e3, "0402"), prefix="R", block="motor")
    c.connect("SD", f"{ren.ref}.1")
    c.connect("GND", f"{ren.ref}.2")
    # phase terminal
    tb = c.add(lib.terminal(3), prefix="J", block="motor")
    for i, x in enumerate("ABC", 1):
        c.connect(f"PH_{x}", f"{tb.ref}.{i}")
    c.constrain(Cluster([tb.ref] + [q_hi[x] for x in "ABC"], 0.5, 60.0))
    c.add_port("vbus", "BUS", "power", "in", voltage=vbus_nom, current_a=I, vmin=24.0 if vbus_nom >= 24 else 12.0, vmax=vbus_max)
    c.add_port("vgate", "VGATE", "power", "in", voltage=vgate, current_a=0.05, vmin=10.0, vmax=20.0)
    c.add_port("vlogic", "VLOGIC", "power", "in", voltage=vlogic, current_a=0.02, vmin=2.7, vmax=5.5)
    c.add_port("gnd", "GND", "ground", "bidir")
    c.add_port("pwm", "PWM_A", "pwm", "in", voltage=vlogic, signals={"a": "PWM_A", "b": "PWM_B", "c": "PWM_C"}, role="device")
    c.add_port("enable", "SD", "gpio", "in", voltage=vlogic, signals={"io": "SD"}, role="device")
    c.add_port("sense", "SENSE_A", "analog", "out", voltage=vlogic, signals={"a": "SENSE_A", "b": "SENSE_B", "c": "SENSE_C"},
               role="device", bias=vlogic / 2, gain=sense_gain, rshunt=rs)
    c.add_port("phase", "PH_A", "motor_phase", "out", voltage=vbus_nom, current_a=I, signals={"a": "PH_A", "b": "PH_B", "c": "PH_C"}, terminal=tb.ref)
    c.calcs["motor_driver"] = (f"{fet.mpn} x6, {drv.mpn} x3; Rds(on) {rds * 1e3:.1f} mOhm: conduction {p_cond:.2f} W + switching {p_sw:.2f} W "
                               f"= {p_fet:.2f} W per FET at {I:g} A rms / {vbus_nom:g} V / {pwm_hz / 1e3:.0f} kHz (tsw {tsw * 1e9:.0f} ns); "
                               f"Cbootstrap {fmt_si(cbs, 'F')} (need >= {fmt_si(cbs_req, 'F')}); Rg {rg:g} ohm; shunt {shunt_mohm:g} mOhm, gain {sense_gain:g}: "
                               f"+-{v_sense_pk:.2f} V swing at {1.414 * I:.0f} A peak around {vlogic / 2:.2f} V")
    if p_fet > 0.6:
        c.warnings.append(f"motor_driver: each MOSFET dissipates ~{p_fet:.1f} W - TO-220 tabs need a heatsink (or thermal pad to chassis); "
                          f"board estimate assumes none")
    if v_sense_pk > vlogic / 2 * 0.9:
        c.warnings.append(f"motor_driver: current-sense swing {v_sense_pk:.2f} V exceeds the ADC headroom; reduce gain")
    if I > 15:
        c.warnings.append(f"motor_driver: {I:g} A RMS phase current needs 2 oz copper and wide planes; review ampacity warnings")
    return c
