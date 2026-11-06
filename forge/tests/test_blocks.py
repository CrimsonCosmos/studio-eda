import math

import pytest

from studioforge.blocks.mcu import PinMuxError, solve_pinmux
from studioforge.blocks.registry import build_block, describe_blocks
from studioforge.erc import run_erc


@pytest.mark.parametrize("name,params", [
    ("battery_input", {}), ("ldo", {"vin_max": 5.5, "vout": 3.3, "iout": 0.2}),
    ("buck_converter", {"vin_min": 9, "vin_max": 14, "vout": 3.3, "iout": 2.0}),
    ("buck_converter", {"vin_min": 39, "vin_max": 54.6, "vout": 12, "iout": 3.0}),
    ("can_node", {}), ("i2c_sensor", {}), ("motor_driver_3ph", {"phase_current_a": 8}),
    ("mcu_stm32f103", {"requests": [{"name": "p", "kind": "pwm", "signals": ["a", "b", "c"]}, {"name": "c", "kind": "can"}]}),
])
def test_every_block_builds_erc_clean(lib, name, params):
    c = build_block(lib, name, params)
    assert [i.msg for i in run_erc(c) if i.severity == "error"] == []
    assert c.ports and c.components


def test_catalog_describes_all_blocks():
    names = {b["name"] for b in describe_blocks()}
    assert {"battery_input", "buck_converter", "ldo", "mcu_stm32f103", "can_node", "motor_driver_3ph", "connector_breakout", "custom_netlist"} <= names


def test_buck_follows_datasheet_equations(lib):
    c = build_block(lib, "buck_converter", {"vin_min": 8.5, "vin_max": 14, "vout": 5.0, "iout": 2.5})
    assert c.components["U1"].part.mpn == "TPS563201DDCR"
    # Vout = Vref*(1+R1/R2) within 1%
    r = {x.ref: x for x in c.components.values() if x.part.category == "resistor"}
    vout = float(c.ports["vout"].voltage)
    assert abs(vout - 5.0) / 5.0 < 0.01
    ind = next(x for x in c.components.values() if x.part.category == "inductor")
    # TPS563201 datasheet: 3.3 uH..4.7 uH required for 5 V
    assert 3.3e-6 <= ind.part.params["henries"] <= 4.7e-6
    assert ind.part.params["isat"] >= 2.5


def test_high_voltage_buck_uses_catch_diode_and_compensation(lib):
    c = build_block(lib, "buck_converter", {"vin_min": 39, "vin_max": 54.6, "vout": 12, "iout": 3.0})
    mpns = {x.part.mpn for x in c.components.values()}
    assert "TPS54360BDDAR" in mpns and "B560C-13-F" in mpns
    assert "compensation" in c.calcs and "uvlo" in c.calcs


def test_buck_miss_offline_raises_part_not_found(lib):
    from studioforge.parts.db import PartNotFound
    with pytest.raises(PartNotFound):
        build_block(lib, "buck_converter", {"vin_min": 9, "vin_max": 60, "vout": 5.0, "iout": 5.0})


def test_motor_driver_sizes_from_current(lib):
    c = build_block(lib, "motor_driver_3ph", {"phase_current_a": 10})
    assert sum(1 for x in c.components.values() if x.part.mpn == "IRFB4110PBF") == 6
    assert c.ports["phase"].current_a == 10 and set(c.ports["sense"].signals) == {"a", "b", "c"}
    assert any("heatsink" in w for w in c.warnings)


def test_pinmux_solver_resolves_and_respects_conflicts(lib):
    part = lib.get("STM32F103C8T6")
    reqs = [{"name": "pwm", "kind": "pwm", "signals": ["a", "b", "c"]}, {"name": "can", "kind": "can"}, {"name": "uart", "kind": "uart"},
            {"name": "i2c", "kind": "i2c"}, {"name": "spi", "kind": "spi"}, {"name": "adc", "kind": "adc", "signals": ["a", "b", "c"]}, {"name": "io", "kind": "gpio", "count": 4}]
    sol = solve_pinmux(part, reqs, {"34", "37", "5", "6", "7", "44", "20"})
    used = [p for d in sol.values() for p in d.values()]
    assert len(used) == len(set(used))                              # no pin used twice
    assert sol["can"] == {"tx": "33", "rx": "32"}


def test_pinmux_impossible_request_fails_loudly(lib):
    part = lib.get("STM32F103C8T6")
    with pytest.raises(PinMuxError):
        solve_pinmux(part, [{"name": f"u{i}", "kind": "uart"} for i in range(5)], set())


def test_custom_netlist_erc_blocks_bad_input(lib):
    parts = [{"ref": "U1", "part": "AMS1117-3.3"}, {"ref": "U2", "part": "AP2112K-3.3TRG1"}]
    with pytest.raises(ValueError):
        build_block(lib, "custom_netlist", {"parts": parts, "nets": {"OUT": ["U1.2", "U2.5"]}})     # two power outputs shorted


def test_connector_family_follows_current(lib):
    lo = build_block(lib, "connector_breakout", {"pins": [{"name": "a"}, {"name": "b"}], "max_current_a": 0.5, "voltage": 5})
    hi = build_block(lib, "connector_breakout", {"pins": [{"name": "p"}, {"name": "g"}], "max_current_a": 25, "voltage": 48})
    assert "XH" in next(iter(lo.components.values())).part.mpn
    assert "XT60" in next(iter(hi.components.values())).part.mpn


def test_researched_part_drives_block_with_safe_defaults(lib):
    """A part that arrived through the research path (no TI-specific hysteresis data) must still yield a correct,
    flagged design: catch diode because it is non-synchronous, generic EN divider + explicit warning."""
    from studioforge.footprints import resolve_footprint
    from studioforge.model import PartDef, PinDef
    pins = [PinDef("1", "BOOT"), PinDef("2", "NC", "nc"), PinDef("3", "NC", "nc"), PinDef("4", "VSENSE", "input"), PinDef("5", "ENA", "input"),
            PinDef("6", "GND", "power_in"), PinDef("7", "VIN", "power_in"), PinDef("8", "PH", "output"), PinDef("9", "DAP", "power_in")]
    lib.put(PartDef("TPS5430DDA", "Texas Instruments", "36V 3A buck", "ic.regulator.buck", "SO-PowerPAD-8", pins, "SOIC-8-EP",
                    {"vin_min": 5.5, "vin_max": 36, "vout_min": 1.23, "vout_max": 31, "iout_max": 3.0, "vref": 1.221, "fsw_khz": 500, "sync": False,
                     "en_max": 7, "roles": {"vin": "VIN", "sw": "PH", "boot": "BOOT", "fb": "VSENSE", "en": "ENA", "gnd": "GND", "pad": "DAP"}},
                    provenance="researched", verified=True))
    c = build_block(lib, "buck_converter", {"vin_min": 12, "vin_max": 30, "vout": 5.0, "iout": 2.5, "part": "TPS5430DDA"})
    assert [i.msg for i in run_erc(c) if i.severity == "error"] == []
    cats = [x.part.category for x in c.components.values()]
    assert "diode" in cats                                              # non-synchronous => catch diode
    assert any("enable-pin thresholds not in the catalog" in w for w in c.warnings)
    assert abs(c.ports["vout"].voltage - 5.0) / 5.0 < 0.01              # feedback computed from the researched Vref (1.221 V)
