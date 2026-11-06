"""Communication blocks: CAN transceiver node, I2C sensor module header."""
from __future__ import annotations

from ..constraints import Cluster, Decouple, Edge
from ..model import Circuit
from ..parts.db import PartsLibrary
from .registry import Param, block
from .util import infer_roles


@block("can_node", "CAN 2.0 transceiver (SN65HVD230) with bus termination option; logic side is a 'can_logic' port, bus side a 'can_bus' port.",
       [Param("terminated", "bool", True, "fit 120 ohm termination (jumper-selectable)"), Param("vcc", "number", 3.3, "logic supply (V)"),
        Param("bitrate", "number", 1e6, "bus speed (bit/s)")], "comms")
def can_node(lib: PartsLibrary, terminated=True, vcc=3.3, bitrate=1e6) -> Circuit:
    c = Circuit("can_node")
    part = lib.require("ic.interface.can", {"vcc_max": (">=", vcc), "vcc_min": ("<=", vcc), "bitrate": (">=", bitrate)},
                       need=f"CAN transceiver, {vcc:g} V supply, >= {bitrate / 1e3:.0f} kbit/s")
    u = c.add(part, prefix="U", block="can")
    pin = lambda name: f"{u.ref}.{part.pin(name).number}"
    c.set_net("GND", role="ground")
    c.set_net("VCC", role="power", current_a=0.05, voltage=vcc)
    c.connect("VCC", pin("VCC"))
    c.connect("GND", pin("GND"), pin("Rs"))           # Rs to GND = high-speed mode (datasheet)
    c.connect("TXD", pin("D"))
    c.connect("RXD", pin("R"))
    c.connect("CANH", pin("CANH"))
    c.connect("CANL", pin("CANL"))
    c.set_net("CANH", role="highspeed", noisy=True)
    c.set_net("CANL", role="highspeed", noisy=True)
    cap = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C", block="can")
    c.connect("VCC", f"{cap.ref}.1")
    c.connect("GND", f"{cap.ref}.2")
    c.constrain(Decouple(cap.ref, u.ref, pin("VCC"), 2.5, 8.0))
    if terminated:
        r = c.add(lib.resistor(120, "0603", 1.0), prefix="R", block="can")
        jp = c.add(lib.header(2, 1, "CAN termination jumper (fit on end nodes only)"), prefix="JP", block="can")
        c.connect("CANH", f"{r.ref}.1")
        c.connect("TERM", f"{r.ref}.2", f"{jp.ref}.1")
        c.connect("CANL", f"{jp.ref}.2")
        c.constrain(Cluster([u.ref, r.ref, jp.ref], 1.0, 12.0))
    c.add_port("logic", "TXD", "can_logic", "bidir", voltage=vcc, signals={"tx": "TXD", "rx": "RXD"}, role="device")
    c.add_port("bus", "CANH", "can_bus", "bidir", voltage=0.0, signals={"canh": "CANH", "canl": "CANL"}, role="peer", needs_connector=True)
    c.add_port("vcc", "VCC", "power", "in", voltage=vcc, current_a=0.05, vmin=3.0, vmax=3.6)
    c.add_port("gnd", "GND", "ground", "bidir")
    c.calcs["can"] = f"{part.mpn}: {bitrate / 1e3:.0f} kbit/s, Rs->GND (fast slope), termination {'jumper-selectable 120R' if terminated else 'none'}"
    return c


@block("i2c_sensor", "Off-board I2C sensor module on a pin header (module from the parts library, default MPU-6050 'GY-521') with pull-ups on the host side.",
       [Param("module", "string", "GY-521", "module MPN"), Param("pullups", "bool", True, "fit 4.7k SDA/SCL pull-ups"), Param("vcc", "number", 3.3, "supply (V)")], "sensor")
def i2c_sensor(lib: PartsLibrary, module="GY-521", pullups=True, vcc=3.3) -> Circuit:
    c = Circuit("i2c_sensor")
    part = lib.resolve(module)
    m = c.add(part, prefix="J", block="sensor")
    pn = lambda name: f"{m.ref}.{part.pin(name).number}"
    c.set_net("GND", role="ground")
    c.connect("VCC", pn("VCC"))
    c.connect("GND", pn("GND"))
    c.connect("SCL", pn("SCL"))
    c.connect("SDA", pn("SDA"))
    for extra in ("INT", "AD0"):
        try:
            if extra == "AD0":
                c.connect("GND", pn("AD0"))
            else:
                c.connect("INT", pn("INT"))
        except KeyError:
            pass
    if pullups:
        for net in ("SCL", "SDA"):
            r = c.add(lib.resistor(4.7e3, "0402"), prefix="R", block="sensor")
            c.connect("VCC", f"{r.ref}.1")
            c.connect(net, f"{r.ref}.2")
    c.constrain(Edge(m.ref, "any", 2.0))
    c.add_port("i2c", "SCL", "i2c", "in", voltage=vcc, signals={"scl": "SCL", "sda": "SDA"}, role="device", addr=part.params.get("i2c_addr", [0x68])[0])
    c.add_port("vcc", "VCC", "power", "in", voltage=vcc, current_a=0.01, vmin=part.params.get("vcc_min", 3.0), vmax=part.params.get("vcc_max", 5.0))
    c.add_port("gnd", "GND", "ground", "bidir")
    if any(p.name == "INT" for p in part.pins):
        c.add_port("int", "INT", "gpio", "out", voltage=vcc)
    return c
