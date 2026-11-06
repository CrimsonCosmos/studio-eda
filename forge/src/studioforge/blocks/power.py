"""Power blocks: battery input, buck converter, LDO."""
from __future__ import annotations

import math

from ..constraints import Cluster, Decouple, Edge, Loop
from ..model import Circuit, fmt_si
from ..parts.db import PartsLibrary
from .registry import Param, block
from .util import e12, e24, e96, infer_roles, std_inductor


@block("battery_input",
       "Battery/DC input stage: power connector, TVS surge clamp, bulk capacitance, fuse (<=10 A) - exposes the raw bus rail.",
       [Param("vbat_nom", "number", 48.0, "nominal pack voltage (V)"), Param("vbat_max", "number", 54.6, "max voltage incl. full charge (V)"),
        Param("vbat_min", "number", 39.0, "min voltage at cutoff (V)"), Param("ibat_max", "number", 10.0, "max continuous bus current (A)"),
        Param("rail", "string", "VBAT", "net name of the output rail")], "power", auto=("ibat_max", "vbat_nom", "vbat_max", "vbat_min"))
def battery_input(lib: PartsLibrary, vbat_nom=48.0, vbat_max=54.6, vbat_min=39.0, ibat_max=10.0, rail="VBAT") -> Circuit:
    c = Circuit("battery_input")
    c.set_net("GND", role="ground")
    c.set_net("RAW", role="power", current_a=ibat_max, voltage=vbat_max)
    if ibat_max <= 30:
        conn = c.add(lib.get("XT60PW-M"), prefix="J", block="battery_input")
        c.connect("RAW", f"{conn.ref}.1")
        c.connect("GND", f"{conn.ref}.2")
    else:
        n = math.ceil(ibat_max / 25)
        c.warnings.append(f"battery_input: {ibat_max} A exceeds one XT60 (30 A); fitted {n} terminal blocks - use a bus bar / external contactor")
        conn = c.add(lib.terminal(2), prefix="J", block="battery_input")
        c.connect("RAW", f"{conn.ref}.1")
        c.connect("GND", f"{conn.ref}.2")
    # fuse
    if ibat_max <= 10:
        f = c.add(lib.fuse(ibat_max * 1.25 if ibat_max * 1.25 <= 15 else 15, volts=max(63, vbat_max * 1.2)), prefix="F", block="battery_input")
        c.connect("RAW", f"{f.ref}.1")
        c.connect(rail, f"{f.ref}.2")
    else:
        # no on-board fuse above 10 A; the bus passes straight through
        c.nets["RAW"] = c.nets.pop("RAW")
        c.rename_net("RAW", rail)
        c.warnings.append(f"battery_input: {ibat_max} A is beyond SMD fuse capability - place an inline fuse (ANL/MIDI) or a BMS with short-circuit protection in the battery harness")
    bus = rail
    c.set_net(bus, role="power", current_a=ibat_max, voltage=vbat_max)
    # TVS
    tvs = lib.require("tvs", {"vrwm": (">=", vbat_max * 1.02)}, need=f"unidirectional TVS diode, standoff voltage >= {vbat_max * 1.02:.0f} V, 600 W SMB")
    t = c.add(tvs, prefix="D", block="battery_input")
    c.connect(bus, f"{t.ref}.1")
    c.connect("GND", f"{t.ref}.2")
    # bulk capacitors: ripple/ESR heuristic, 2 caps
    cap_v = 63 if vbat_max <= 50 else 100 if vbat_max <= 80 else 200
    n_bulk = 2 if ibat_max <= 40 else 3
    refs = []
    for i in range(n_bulk):
        cp = c.add(lib.electrolytic(220e-6 if cap_v == 100 else 470e-6, cap_v), prefix="C", block="battery_input")
        c.connect(bus, f"{cp.ref}.1")
        c.connect("GND", f"{cp.ref}.2")
        refs.append(cp.ref)
    h = c.add(lib.mlcc(2.2e-6, cap_v if cap_v <= 100 else 100), prefix="C", block="battery_input")
    c.connect(bus, f"{h.ref}.1")
    c.connect("GND", f"{h.ref}.2")
    c.constrain(Edge(conn.ref, "any", 4.0))
    c.constrain(Cluster([conn.ref, t.ref] + refs, 1.0, 25.0))
    c.add_port("vbus", bus, "power", "out", voltage=vbat_nom, current_a=ibat_max, vmin=vbat_min, vmax=vbat_max, source="battery")
    c.add_port("src", "RAW", "power", "in", voltage=vbat_nom, current_a=ibat_max, vmin=vbat_min, vmax=vbat_max, external=True, connector=conn.part.mpn)
    c.add_port("gnd", "GND", "ground", "bidir")
    c.calcs["battery_input"] = f"{vbat_min:g}-{vbat_max:g} V, {ibat_max:g} A; TVS {tvs.mpn}; bulk {n_bulk}x{refs and c.components[refs[0]].value}/{cap_v}V"
    return c


def _buck_losses(ic, vin: float, vout: float, iout: float, fsw: float) -> float:
    p = ic.params
    if "rds_on_hs" in p:      # TPS54360B datasheet Eq. 52-55 (non-sync)
        pc = iout ** 2 * p["rds_on_hs"] * vout / vin
        ps = vin * fsw * iout * p.get("trise_ns", 5) * 1e-9
        pg = vin * p.get("qg_nc", 3) * 1e-9 * fsw
        return pc + ps + pg + vin * 150e-6
    return iout * vout * (1 / 0.9 - 1)


@block("buck_converter",
       "Step-down switching regulator sized from vin/vout/iout: picks the IC from the parts library (researching one if none fits), "
       "computes feedback divider, inductor, in/out capacitors, bootstrap, compensation and loss from the datasheet equations.",
       [Param("vin_min", "number", 9.0, "min input (V)"), Param("vin_max", "number", 17.0, "max input (V)"),
        Param("vout", "number", None, "output voltage (V)", True), Param("iout", "number", None, "max load current (A)", True),
        Param("rail", "string", "VOUT", "output rail net name, e.g. +12V"), Param("ripple", "number", 0.3, "inductor ripple ratio KIND"),
        Param("part", "string", None, "force a specific regulator MPN")], "power", auto=("vin_min", "vin_max", "iout"))
def buck_converter(lib: PartsLibrary, vin_min=9.0, vin_max=17.0, vout=None, iout=None, rail="VOUT", ripple=0.3, part=None) -> Circuit:
    c = Circuit(f"buck_{vout:g}V")
    c.set_net("GND", role="ground")
    if part:
        ic_part = lib.resolve(part)
    else:
        ic_part = lib.require("ic.regulator.buck",
                              {"vin_max": (">=", vin_max), "iout_max": (">=", iout), "vout_min": ("<=", vout), "vout_max": (">=", vout)},
                              need=f"synchronous or asynchronous buck converter IC: Vin up to {vin_max:g} V (min {vin_min:g} V), "
                                   f"Vout {vout:g} V, Iout >= {iout:.1f} A, adjustable output with external feedback divider, "
                                   f"datasheet gives a design procedure with compensation values")
    p = ic_part.params
    if iout > 0.85 * p.get("iout_max", iout):
        c.warnings.append(f"buck {ic_part.mpn}: load {iout:g} A is >85% of its {p['iout_max']:g} A rating - little thermal margin")
    roles = infer_roles(ic_part)
    fsw = p.get("fsw", 500e3)
    vref = p["vref"]
    ic = c.add(ic_part, prefix="U", block="buck")
    one = lambda r: f"{ic.ref}.{roles[r][0]}"
    miss = [r for r in ("vin", "sw", "fb", "gnd") if r not in roles]
    if miss:
        raise ValueError(f"{ic_part.mpn}: cannot find pins for roles {miss}; add params['roles'] to the part record")
    out_net = rail
    c.set_net("VIN", role="power", current_a=iout * vout / max(vin_min, 1) / 0.85, voltage=vin_max)
    c.set_net(out_net, role="power", current_a=iout, voltage=vout)
    c.set_net("SW", noisy=True, current_a=iout)
    c.set_net("FB", sensitive=True)
    c.connect("VIN", *[f"{ic.ref}.{n}" for n in roles["vin"]])
    c.connect("SW", *[f"{ic.ref}.{n}" for n in roles["sw"]])
    c.connect("FB", one("fb"))
    c.connect("GND", *[f"{ic.ref}.{n}" for n in roles["gnd"] + roles.get("pad", [])])
    # ---- feedback divider (datasheet: Vout = Vref*(1+R1/R2)) -------------------------------------------
    r_bot = 10e3
    r_top = e96(r_bot * (vout / vref - 1))
    vout_act = vref * (1 + r_top / r_bot)
    rt_, rb_ = c.add(lib.resistor(r_top, "0402"), prefix="R", block="buck"), c.add(lib.resistor(r_bot, "0402"), prefix="R", block="buck")
    c.connect(out_net, f"{rt_.ref}.1")
    c.connect("FB", f"{rt_.ref}.2", f"{rb_.ref}.1")
    c.connect("GND", f"{rb_.ref}.2")
    # ---- inductor ---------------------------------------------------------------------------------------------
    d = vout / vin_max
    l_calc = (vin_max - vout) * d / (fsw * ripple * iout)
    l_val = std_inductor(l_calc)
    rec = p.get("recommended")
    if rec:
        # datasheet table of loop-stable inductor ranges: use the nearest tabulated Vout at or above ours
        keys = sorted(rec, key=float)
        k = next((k for k in keys if float(k) >= vout - 1e-9), keys[-1])
        lo, hi = rec[k]["l_min_uh"] * 1e-6, rec[k]["l_max_uh"] * 1e-6
        if not (lo <= l_val <= hi):
            c.notes.append(f"inductor clamped to datasheet-recommended {lo * 1e6:g}-{hi * 1e6:g} uH (calc {l_val * 1e6:.1f} uH)")
            l_val = min(max(l_calc, lo), hi)
            l_val = min((v for v in (1.5e-6, 2.2e-6, 3.3e-6, 4.7e-6) if lo - 1e-12 <= v <= hi + 1e-12 and v >= min(l_val, hi) - 1e-12), default=hi)
    di = (vin_max - vout) * d / (fsw * l_val)
    ipk = iout + di / 2
    isat = ipk * 1.3
    ind = lib.inductor(l_val, isat)
    L = c.add(ind, prefix="L", block="buck", power_w=ipk ** 2 * ind.params["dcr"] * 0.6)
    c.connect("SW", f"{L.ref}.1")
    c.connect(out_net, f"{L.ref}.2")
    # ---- input capacitors ----------------------------------------------------------------------------------------
    cap_v_in = 100 if vin_max > 40 else 50 if vin_max > 20 else 25
    cin_req = 0.25 * iout / (fsw * 0.4)
    unit = 2.2e-6 if cap_v_in >= 50 else 10e-6
    derate = 0.5
    n_in = max(2, math.ceil(cin_req / (unit * derate)))
    cin_refs = []
    for i in range(n_in):
        cp = c.add(lib.mlcc(unit, cap_v_in), prefix="C", block="buck")
        c.connect("VIN", f"{cp.ref}.1")
        c.connect("GND", f"{cp.ref}.2")
        cin_refs.append(cp.ref)
    hf = c.add(lib.capacitor(100e-9, "0603", cap_v_in if cap_v_in >= 50 else 50), prefix="C", block="buck")
    c.connect("VIN", f"{hf.ref}.1")
    c.connect("GND", f"{hf.ref}.2")
    c.constrain(Decouple(hf.ref, ic.ref, one("vin"), 2.5, 10.0))
    # ---- output capacitors (datasheet Eq. 32-34 criteria) ---------------------------------------------------------------
    dI = 0.5 * iout
    c_trans = 2 * dI / (fsw * 0.04 * vout)
    iol, ioh = 0.25 * iout, 0.75 * iout
    c_over = l_val * (ioh ** 2 - iol ** 2) / ((1.04 * vout) ** 2 - vout ** 2)
    c_total = max(c_trans, c_over, 20e-6 if "cout_min_uf" in p else 10e-6)
    if "cout_max_uf" in p:
        c_total = min(c_total, p["cout_max_uf"] * 1e-6 * 0.9)
    unit_o = 22e-6 if vout <= 12 else 4.7e-6
    cv = max(10.0, math.ceil(vout * 2 / 5) * 5) if vout <= 12 else max(25.0, vout * 2)
    n_out = max(2, math.ceil(c_total / (unit_o * 0.6)))
    cout_refs = []
    for i in range(n_out):
        cp = c.add(lib.mlcc(unit_o, cv), prefix="C", block="buck")
        c.connect(out_net, f"{cp.ref}.1")
        c.connect("GND", f"{cp.ref}.2")
        cout_refs.append(cp.ref)
    cout_eff = n_out * unit_o * 0.6
    # ---- bootstrap ----------------------------------------------------------------------------------------------------------
    if "boot" in roles:
        cb = c.add(lib.capacitor(p.get("boot_uf", 0.1) * 1e-6, "0402", 16), prefix="C", block="buck")
        c.connect("BOOT", f"{ic.ref}.{roles['boot'][0]}", f"{cb.ref}.1")
        c.connect("SW", f"{cb.ref}.2")
    # ---- enable ---------------------------------------------------------------------------------------------------------------
    if "en" in roles:
        en_pin = one("en")
        if vin_max <= p.get("en_max", 5.5):
            c.connect("VIN", en_pin)
        else:
            # programmable UVLO divider (datasheet Eq. 40/41 for devices with hysteresis current)
            ven = p.get("vref_en", 1.2)
            vstart = max(vin_min * 0.92, ven * 2)
            vstop = vstart * 0.8
            ihys = 3.4e-6
            r1 = e96((vstart - vstop) / ihys)
            r2 = e96(ven / ((vstart - ven) / r1 + 1.2e-6))
            ra, rb = c.add(lib.resistor(r1, "0603", power_w=0.1), prefix="R", block="buck"), c.add(lib.resistor(r2, "0402"), prefix="R", block="buck")
            c.connect("VIN", f"{ra.ref}.1")
            c.connect("EN", en_pin, f"{ra.ref}.2", f"{rb.ref}.1")
            c.connect("GND", f"{rb.ref}.2")
            c.calcs["uvlo"] = f"EN divider {fmt_si(r1)}/{fmt_si(r2)}: start {vstart:.1f} V stop {vstop:.1f} V"
    # ---- non-synchronous: catch diode + RT + compensation ------------------------------------------------------------------------
    diode = None
    if p.get("needs_catch_diode"):
        dp = lib.require("diode", {"vr": (">=", vin_max), "if": (">=", iout * 1.2)}, tags=["schottky"],
                         need=f"Schottky catch diode Vr >= {vin_max:g} V, If >= {iout * 1.2:.1f} A")
        diode = c.add(dp, prefix="D", block="buck", power_w=dp.params.get("vf", 0.6) * iout * (1 - d))
        c.connect("SW", f"{diode.ref}.1")          # cathode to SW, anode to GND (conducts SW->GND off-time)
        c.swap = None
        # catch diode: cathode on SW? conventional buck: cathode to SW, anode to GND
        c.nets["SW"].remove(f"{diode.ref}.1")
        c.connect("SW", f"{diode.ref}.1")
        c.connect("GND", f"{diode.ref}.2")
        if "rt" in roles and "rt_500k" in p:
            rt = c.add(lib.resistor(p["rt_500k"], "0402"), prefix="R", block="buck")
            c.connect("RT", one("rt"), f"{rt.ref}.1")
            c.connect("GND", f"{rt.ref}.2")
            fsw = 500e3
    if "comp" in roles and "gm_ea" in p:
        # Datasheet Eq. 44-51 (type-2A, ceramic output)
        esr = 2.5e-3
        fp_mod = iout / (2 * math.pi * vout * cout_eff)
        fz_esr = 1 / (2 * math.pi * esr * cout_eff)
        fco = min(math.sqrt(fp_mod * fz_esr), math.sqrt(fp_mod * fsw / 2))
        r4 = e24((2 * math.pi * fco * cout_eff / p["gm_ps"]) * (vout / (vref * p["gm_ea"])))
        c5 = e12(1 / (2 * math.pi * r4 * fp_mod))
        c8 = max(cout_eff * esr / r4, 1 / (r4 * fsw * math.pi))
        c8 = e12(max(c8, 10e-12))
        rc = c.add(lib.resistor(r4, "0402"), prefix="R", block="buck")
        c5p = c.add(lib.capacitor(c5, "0402", 50), prefix="C", block="buck")
        c8p = c.add(lib.capacitor(c8, "0402", 50, "C0G"), prefix="C", block="buck")
        c.connect("COMP", one("comp"), f"{rc.ref}.1", f"{c8p.ref}.1")
        c.connect("COMPR", f"{rc.ref}.2", f"{c5p.ref}.1")
        c.connect("GND", f"{c5p.ref}.2", f"{c8p.ref}.2")
        c.set_net("COMP", sensitive=True)
        c.calcs["compensation"] = (f"fp_mod={fp_mod:.0f} Hz, fco={fco / 1e3:.1f} kHz, Rcomp={fmt_si(r4)}, Ccomp={fmt_si(c5, 'F')}, "
                                   f"Cpole={fmt_si(c8, 'F')}")
    # ---- thermal / placement hints --------------------------------------------------------------------------------------------------
    pw = _buck_losses(ic_part, (vin_min + vin_max) / 2, vout, iout, fsw)
    ic.power_w = pw
    loop_refs = [ic.ref, hf.ref] + ([diode.ref] if diode else []) + cin_refs[:1]
    c.constrain(Loop(loop_refs, "buck-hot-loop", 6.0))
    c.constrain(Cluster([ic.ref, L.ref] + cout_refs, 1.5, 22.0))
    for r in cin_refs:
        c.constrain(Cluster([ic.ref, r], 1.0, 9.0))
    c.add_port("vin", "VIN", "power", "in", voltage=(vin_min + vin_max) / 2, current_a=iout * vout / max(vin_min, 1) / 0.85,
               vmin=vin_min, vmax=vin_max)
    c.add_port("vout", out_net, "power", "out", voltage=vout_act, current_a=iout, source="buck", tolerance=0.03)
    c.add_port("gnd", "GND", "ground", "bidir")
    eta = vout * iout / (vout * iout + pw + (diode.power_w if diode else 0) + L.power_w)
    c.calcs["buck"] = (f"{ic_part.mpn}: {vin_min:g}-{vin_max:g} V -> {vout_act:.3f} V @ {iout:g} A, fsw {fsw / 1e3:.0f} kHz, "
                       f"L={fmt_si(l_val, 'H')} (calc {fmt_si(l_calc, 'H')}, ripple {di:.2f} A, Isat>={isat:.1f} A), "
                       f"Cout {n_out}x{fmt_si(unit_o, 'F')} (need {c_total * 1e6:.0f} uF: transient {c_trans * 1e6:.0f}, overshoot {c_over * 1e6:.0f}), "
                       f"Cin {n_in}x{fmt_si(unit, 'F')}/{cap_v_in}V, FB {fmt_si(r_top)}/{fmt_si(r_bot)} (Vout error {100 * (vout_act / vout - 1):+.2f}%), "
                       f"loss {pw:.2f} W IC, est. efficiency {100 * eta:.0f}%")
    if vin_min < vout + 1.0:
        c.warnings.append(f"buck {vout:g} V: vin_min {vin_min:g} V is within 1 V of Vout - dropout risk")
    if abs(vout_act / vout - 1) > 0.02:
        c.warnings.append(f"buck {vout:g} V: feedback divider gives {vout_act:.3f} V (>2% off)")
    if pw > 1.5:
        c.warnings.append(f"buck {ic_part.mpn} dissipates ~{pw:.1f} W: ensure copper area / airflow")
    return c


@block("ldo", "Linear regulator from the parts library with input/output capacitors and dissipation check.",
       [Param("vin_max", "number", 5.5, "max input (V)"), Param("vin_min", "number", 4.5, "min input (V)"),
        Param("vout", "number", 3.3, "fixed output (V)"), Param("iout", "number", 0.3, "load (A)"),
        Param("rail", "string", "+3V3", "output rail net")], "power", auto=("vin_min", "vin_max", "iout"))
def ldo(lib: PartsLibrary, vin_max=5.5, vin_min=4.5, vout=3.3, iout=0.3, rail="+3V3") -> Circuit:
    c = Circuit(f"ldo_{vout:g}V")
    part = lib.require("ic.regulator.ldo", {"vin_max": (">=", vin_max), "vout": ("==", vout), "iout_max": (">=", iout * 1.2)},
                       need=f"fixed {vout:g} V LDO, Vin up to {vin_max:g} V, {iout * 1.2:.2f} A, SOT-23/SOT-223")
    roles = infer_roles(part)
    u = c.add(part, prefix="U", block="ldo")
    c.set_net("GND", role="ground")
    c.set_net("VIN", role="power", current_a=iout, voltage=vin_max)
    c.set_net(rail, role="power", current_a=iout, voltage=vout)
    c.connect("VIN", *[f"{u.ref}.{n}" for n in roles["vin"]])
    c.connect(rail, *[f"{u.ref}.{n}" for n in roles["vout"]])
    c.connect("GND", *[f"{u.ref}.{n}" for n in roles["gnd"] + roles.get("pad", [])])
    if "en" in roles:
        c.connect("VIN", *[f"{u.ref}.{n}" for n in roles["en"]])
    ci = c.add(lib.mlcc(10e-6, 16 if vin_max <= 8 else 25), prefix="C", block="ldo")
    co = c.add(lib.mlcc(22e-6, 10), prefix="C", block="ldo")
    c.connect("VIN", f"{ci.ref}.1")
    c.connect(rail, f"{co.ref}.1")
    c.connect("GND", f"{ci.ref}.2", f"{co.ref}.2")
    c.constrain(Decouple(ci.ref, u.ref, f"{u.ref}.{roles['vin'][0]}", 3.0, 5.0))
    c.constrain(Decouple(co.ref, u.ref, f"{u.ref}.{roles['vout'][0]}", 3.0, 5.0))
    pw = (vin_max - vout) * iout
    u.power_w = pw
    if pw > 0.8:
        c.warnings.append(f"ldo {part.mpn}: dissipates {pw:.2f} W at Vin {vin_max:g} V - consider a buck stage first")
    if part.mpn.startswith("AMS1117"):
        c.warnings.append("AMS1117 needs output-capacitor ESR for stability: ceramic-only outputs can oscillate; use tantalum/electrolytic (>=22 uF) or a different LDO")
    c.add_port("vin", "VIN", "power", "in", voltage=(vin_min + vin_max) / 2, current_a=iout, vmin=max(vin_min, vout + part.params.get("dropout", 1.0)), vmax=vin_max)
    c.add_port("vout", rail, "power", "out", voltage=vout, current_a=iout, source="ldo", tolerance=0.03)
    c.add_port("gnd", "GND", "ground", "bidir")
    c.calcs["ldo"] = f"{part.mpn}: {vin_max:g}->{vout:g} V @ {iout:g} A, dissipation {pw:.2f} W"
    return c
