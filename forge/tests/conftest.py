import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from studioforge.parts.db import PartsLibrary  # noqa: E402


@pytest.fixture
def lib(tmp_path):
    return PartsLibrary(tmp_path / "home", auto_research=False)


def mcu_circuit(lib):
    from studioforge.constraints import Decouple
    from studioforge.model import Circuit
    c = Circuit("mcu_test")
    u = c.add(lib.get("STM32F103C8T6"), prefix="U", power_w=0.15)
    c.set_net("GND", role="ground")
    c.set_net("+3V3", role="power", current_a=0.2)
    for p in u.part.pins:
        if p.name in ("VDD", "VDDA", "VBAT"):
            c.connect("+3V3", f"U1.{p.number}")
        if p.name in ("VSS", "VSSA"):
            c.connect("GND", f"U1.{p.number}")
    for pn in ("24", "36", "48", "9"):
        cap = c.add(lib.capacitor(100e-9, "0402", 16), prefix="C")
        c.nets["+3V3"].append(f"{cap.ref}.1")
        c.connect("GND", f"{cap.ref}.2")
        c.constrain(Decouple(cap.ref, "U1", f"U1.{pn}"))
    c.add(lib.header(8), prefix="J")
    for i, s in enumerate(["PA9", "PA10", "PA2", "PA3", "PB6", "PB7"]):
        pin = [p for p in u.part.pins if p.name == s][0]
        c.connect(f"S_{s}", f"U1.{pin.number}", f"J1.{i + 1}")
    c.connect("GND", "J1.7")
    c.connect("+3V3", "J1.8")
    return c
