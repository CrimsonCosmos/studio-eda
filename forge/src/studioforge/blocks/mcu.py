"""MCU block with a deterministic pin-multiplexing solver."""
from __future__ import annotations

import re
from itertools import combinations

from ..constraints import Cluster, Decouple, Edge
from ..model import Circuit, PartDef
from ..parts.db import PartsLibrary
from .registry import Param, block


class PinMuxError(RuntimeError):
    pass


def _func_map(part: PartDef) -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for p in part.pins:
        for f in p.alt:
            m.setdefault(f, []).append(p.number)
    return m


def solve_pinmux(part: PartDef, requests: list[dict], reserved: set[str]) -> dict[str, dict[str, str]]:
    """Assign MCU pins to requested peripherals. request = {name, kind, count?, signals?}.
    Returns {request name: {signal: pin number}}. Backtracking over alternative peripheral instances."""
    fm = _func_map(part)
    gpio_pins = [p.number for p in part.pins if p.etype == "bidir" and p.name.startswith("P")]
    gpio_pins.sort(key=lambda n: (part.pin(n).name[1], int(re.sub(r"\D", "", part.pin(n).name) or 0)))

    def single(f: str) -> list[str]:
        return fm.get(f, [])

    def options(req: dict) -> list[dict[str, str]]:
        kind, n = req["kind"], int(req.get("count", 1))
        out: list[dict[str, str]] = []
        if kind == "uart":
            for inst in sorted({re.match(r"(USART\d)", f).group(1) for f in fm if re.match(r"USART\d_TX", f)}):
                tx, rx = single(f"{inst}_TX"), single(f"{inst}_RX")
                if tx and rx:
                    out.append({"tx": tx[0], "rx": rx[0]})
        elif kind == "can":
            tx, rx = single("CAN_TX"), single("CAN_RX")
            if tx and rx:
                out.append({"tx": tx[0], "rx": rx[0]})
        elif kind == "i2c":
            for inst in sorted({re.match(r"(I2C\d)", f).group(1) for f in fm if re.match(r"I2C\d_SCL", f)}):
                s, d = single(f"{inst}_SCL"), single(f"{inst}_SDA")
                if s and d:
                    out.append({"scl": s[0], "sda": d[0]})
        elif kind == "spi":
            for inst in sorted({re.match(r"(SPI\d)", f).group(1) for f in fm if re.match(r"SPI\d_SCK", f)}):
                sc, mi, mo = single(f"{inst}_SCK"), single(f"{inst}_MISO"), single(f"{inst}_MOSI")
                if sc and mi and mo:
                    out.append({"sck": sc[0], "miso": mi[0], "mosi": mo[0]})
        elif kind in ("pwm", "timer"):
            names = req.get("signals") or [f"ch{i + 1}" for i in range(n)]
            for tim in sorted({re.match(r"(TIM\d)", f).group(1) for f in fm if re.match(r"TIM\d_CH\d$", f)}):
                chans = [(int(f[-1]), single(f)[0]) for f in sorted(fm) if f.startswith(tim + "_CH") and re.match(r"TIM\d_CH\d$", f)]
                chans.sort()
                if len(chans) >= len(names):
                    for combo in combinations(chans, len(names)):
                        out.append({nm: pin for nm, (_, pin) in zip(names, combo)})
        elif kind == "adc":
            names = req.get("signals") or [f"ch{i + 1}" for i in range(n)]
            pins = sorted({single(f)[0] for f in fm if re.match(r"ADC_IN\d+$", f)}, key=lambda x: int(x))
            for combo in combinations(pins, len(names)):
                out.append({nm: pin for nm, pin in zip(names, combo)})
        elif kind == "gpio":
            names = req.get("signals") or [f"io{i + 1}" for i in range(n)]
            avoid = {p for p in gpio_pins if any(a in part.pin(p).alt for a in ("SWDIO", "SWCLK", "OSC_IN", "OSC_OUT", "BOOT1", "JTDI", "JTDO", "NJTRST"))}
            pool = [p for p in gpio_pins if p not in avoid and not part.pin(p).alt[:1] == ("TAMPER-RTC",)] + \
                   [p for p in gpio_pins if part.pin(p).alt[:1] == ("TAMPER-RTC",)]
            out.append({nm: pin for nm, pin in zip(names, pool[:len(names)])}) if len(pool) >= len(names) else None
        else:
            raise PinMuxError(f"unsupported peripheral kind '{kind}'")
        return out

    order = sorted(requests, key=lambda r: (r["kind"] == "gpio", len(options(r)) or 0))
    result: dict[str, dict[str, str]] = {}

    def solve(i: int, used: set[str]) -> bool:
        if i == len(order):
            return True
        req = order[i]
        cands = options(req)
        if req["kind"] == "gpio":      # gpio takes whatever is free
            names = req.get("signals") or [f"io{j + 1}" for j in range(int(req.get("count", 1)))]
            free = [p for p in gpio_pins if p not in used and p not in reserved]
            low = [p for p in free if part.pin(p).alt[:1] != ("TAMPER-RTC",) and not any(a in part.pin(p).alt for a in ("SWDIO", "SWCLK", "OSC_IN", "OSC_OUT", "BOOT1", "JTDI", "JTDO", "NJTRST"))]
            pool = low + [p for p in free if p not in low and part.pin(p).alt[:1] == ("TAMPER-RTC",)]
            if len(pool) < len(names):
                return False
            result[req["name"]] = dict(zip(names, pool[:len(names)]))
            return solve(i + 1, used | set(pool[:len(names)]))
        for cand in cands:
            pins = set(cand.values())
            if pins & used or pins & reserved:
                continue
            result[req["name"]] = cand
            if solve(i + 1, used | pins):
                return True
        result.pop(req["name"], None)
        return False

    if not solve(0, set()):
        raise PinMuxError("cannot satisfy peripheral requests on " + part.mpn + ": " + ", ".join(f"{r['name']}({r['kind']}x{r.get('count', 1)})" for r in requests))
    return result


_PORT_KIND = {"uart": ("uart", "bidir"), "can": ("can_logic", "bidir"), "i2c": ("i2c", "out"), "spi": ("spi", "out"),
              "pwm": ("pwm", "out"), "timer": ("pwm", "out"), "adc": ("analog", "in"), "gpio": ("gpio", "bidir")}


@block("mcu_stm32f103",
       "STM32F103C8 microcontroller core: decoupling, HSE crystal, reset, boot straps, SWD header, status LED. "
       "Peripheral `requests` (uart/can/i2c/spi/pwm/adc/gpio) are assigned to pins by a pin-mux solver and exposed as ports.",
       [Param("requests", "list", [], "peripherals: [{name, kind: uart|can|i2c|spi|pwm|adc|gpio, count?, signals?: [names]}]"),
        Param("status_led", "bool", True, "fit a status LED on PC13"), Param("vdd", "number", 3.3, "supply (V)")], "compute", auto=("requests",))
def mcu_stm32f103(lib: PartsLibrary, requests=None, status_led=True, vdd=3.3) -> Circuit:
    c = Circuit("mcu_stm32f103")
    part = lib.get("STM32F103C8T6")
    u = c.add(part, prefix="U", block="mcu", power_w=0.15)
    c.set_net("GND", role="ground")
    c.set_net("+3V3", role="power", current_a=0.08, voltage=vdd)
    requests = list(requests or [])
    reserved = {p.number for p in part.pins if any(a in p.alt for a in ("SWDIO", "SWCLK", "OSC_IN", "OSC_OUT"))}
    reserved |= {"20", "7", "44"}                       # PB2/BOOT1, NRST, BOOT0
    if status_led:
        reserved.add("2")
    pin = lambda n: f"{u.ref}.{n}"
    # power pins
    for p in part.pins:
        if p.name in ("VDD", "VDDA", "VBAT"):
            c.connect("+3V3", pin(p.number))
        elif p.name in ("VSS", "VSSA"):
            c.connect("GND", pin(p.number))
    # decoupling: 100nF per VDD pin (+VDDA, VBAT), bulk 10uF + 4.7uF
    for num in ("24", "36", "48", "9", "1"):
        cap = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C", block="mcu")
        c.connect("+3V3", f"{cap.ref}.1")
        c.connect("GND", f"{cap.ref}.2")
        c.constrain(Decouple(cap.ref, u.ref, pin(num), 3.0, 8.0))
    for val in (10e-6, 1e-6):
        cap = c.add(lib.mlcc(val, 10), prefix="C", block="mcu")
        c.connect("+3V3", f"{cap.ref}.1")
        c.connect("GND", f"{cap.ref}.2")
        c.constrain(Cluster([u.ref, cap.ref], 1.0, 12.0))
    # crystal (8 MHz, CL=12 pF -> 2*(CL-3pF stray)=18 pF)
    xt = c.add(lib.crystal(8.0, 12.0), prefix="Y", block="mcu")
    c.connect("OSC_IN", pin("5"), f"{xt.ref}.1")
    c.connect("OSC_OUT", pin("6"), f"{xt.ref}.3")
    c.connect("GND", f"{xt.ref}.2", f"{xt.ref}.4")
    cl = []
    for net, num in (("OSC_IN", 1), ("OSC_OUT", 3)):
        cc = c.add(lib.capacitor(18e-12, "0402", 50, "C0G"), prefix="C", block="mcu")
        c.connect(net, f"{cc.ref}.1")
        c.connect("GND", f"{cc.ref}.2")
        cl.append(cc.ref)
    c.set_net("OSC_IN", sensitive=True, noisy=True)
    c.set_net("OSC_OUT", sensitive=True, noisy=True)
    c.constrain(Cluster([u.ref, xt.ref] + cl, 3.0, 9.0))
    # reset / boot
    rc = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C", block="mcu")
    c.connect("NRST", pin("7"), f"{rc.ref}.1")
    c.connect("GND", f"{rc.ref}.2")
    for num, nm in (("44", "BOOT0"), ("20", "BOOT1")):
        r = c.add(lib.resistor(10e3, "0402"), prefix="R", block="mcu")
        c.connect(nm, pin(num), f"{r.ref}.1")
        c.connect("GND", f"{r.ref}.2")
    # SWD header: 3V3, SWDIO, SWCLK, NRST, GND
    swd = c.add(lib.header(5, 1, "SWD programming header (3V3, SWDIO, SWCLK, NRST, GND)"), prefix="J", block="mcu")
    c.connect("+3V3", f"{swd.ref}.1")
    c.connect("SWDIO", f"{swd.ref}.2", pin("34"))
    c.connect("SWCLK", f"{swd.ref}.3", pin("37"))
    c.connect("NRST", f"{swd.ref}.4")
    c.connect("GND", f"{swd.ref}.5")
    c.constrain(Cluster([u.ref, swd.ref], 1.5, 28.0))
    c.constrain(Cluster([u.ref, rc.ref], 3.0, 6.0))
    if status_led:
        led = c.add(lib.led("green", "0603"), prefix="D", block="mcu")
        rl = c.add(lib.resistor(1e3, "0402"), prefix="R", block="mcu")
        c.connect("LED_STATUS", pin("2"), f"{rl.ref}.1")
        c.connect("LED_A", f"{rl.ref}.2", f"{led.ref}.2")
        c.connect("GND", f"{led.ref}.1")
    # peripherals
    assign = solve_pinmux(part, requests, reserved) if requests else {}
    for req in requests:
        nm = req["name"]
        sigs = assign[nm]
        nets = {}
        for sig, num in sigs.items():
            net = f"{nm.upper()}_{sig.upper()}"
            c.connect(net, pin(num))
            nets[sig] = net
        kind, direction = _PORT_KIND[req["kind"]]
        c.add_port(nm, next(iter(nets.values())), kind, direction, voltage=vdd, signals=nets,
                   pins={s: part.pin(n).name for s, n in sigs.items()}, role="host")
        if req["kind"] == "analog":
            for net in nets.values():
                c.set_net(net, sensitive=True)
    c.calcs["pinmux"] = "; ".join(f"{k}: " + ",".join(f"{s}={part.pin(n).name}" for s, n in v.items()) for k, v in assign.items()) or "no peripherals requested"
    c.add_port("vdd", "+3V3", "power", "in", voltage=vdd, current_a=0.08 + 0.01 * len(requests), vmin=2.0, vmax=3.6)
    c.add_port("gnd", "GND", "ground", "bidir")
    return c
