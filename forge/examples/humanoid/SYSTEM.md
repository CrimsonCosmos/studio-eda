# humanoid_robot - system design

> Request: build me a humanoid robot

## Cost vs budget (estimates - refresh with live quotes before ordering)

| Item | USD |
|--|--|
| Custom boards (PCB + parts + assembly) | 1,974.72 |
| Purchased items | 23,370.00 |
| Harness / cables | 701.22 |
| Contingency (10%) | 2,604.59 |
| **Total** | **28,650.53** |
| Budget | 50,000.00 |
| **Within budget** | +21,349.47 |

## Buy vs build decisions

- joint_large.buck12: BUILD $11.29 vs buy $28.00 (building is cheaper by 60%)
- joint_small.buck12: BUILD $10.39 vs buy $28.00 (building is cheaper by 63%)

## Boards

| Board | Qty | Parts | Size (mm) | Layers | Unit cost | DRC | Notes |
|--|--|--|--|--|--|--|--|
| joint_large | 12 | 124 | 125 x 93 | 4 | $71.72 | clean | BLDC joint controller (15 A phase) |
| joint_small | 16 | 125 | 95 x 70 | 4 | $69.63 | clean | BLDC joint controller (6 A phase) |

## Power tree (demand propagated upstream, windows downstream)

- `battery.out` must supply **48.76 A** (fleet qty x duty applied across boards)
- `dcdc_aux.out` must supply **2.50 A** (fleet qty x duty applied across boards)
- `joint_large.buck12.vout` must supply **0.12 A** (fleet qty x duty applied across boards)
- `joint_large.buck33.vout` must supply **0.19 A** (fleet qty x duty applied across boards)
- `joint_large.bus.vbus` must supply **15.05 A** (fleet qty x duty applied across boards)
- `joint_small.buck12.vout` must supply **0.12 A** (fleet qty x duty applied across boards)
- `joint_small.buck33.vout` must supply **0.19 A** (fleet qty x duty applied across boards)
- `joint_small.bus.vbus` must supply **6.05 A** (fleet qty x duty applied across boards)

## Block parameters decided by the resolver

- `joint_large.buck12`: {'vout': 12, 'rail': '+12V', 'iout': 0.132, 'vin_min': 39.0, 'vin_max': 54.6}
- `joint_large.buck33`: {'vout': 3.3, 'rail': '+3V3', 'iout': 0.209, 'vin_min': 11.64, 'vin_max': 12.36}
- `joint_large.bus`: {'ibat_max': 16.553, 'vbat_min': 39, 'vbat_max': 54.6, 'vbat_nom': 48}
- `joint_large.can`: {}
- `joint_large.conn1`: {'pins': [{'name': 'canh'}, {'name': 'canl'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 0.0, 'kind': 'can_bus', 'label': 'link'}
- `joint_large.drv`: {'vbus_max': 54.6, 'vbus_nom': 48.0, 'phase_current_a': 15}
- `joint_large.mcu`: {} requests=[('pwm_drv', 'pwm'), ('en_drv', 'gpio'), ('cur_sense', 'adc'), ('can0', 'can')]
- `joint_small.buck12`: {'vout': 12, 'rail': '+12V', 'iout': 0.132, 'vin_min': 39.0, 'vin_max': 54.6}
- `joint_small.buck33`: {'vout': 3.3, 'rail': '+3V3', 'iout': 0.209, 'vin_min': 11.64, 'vin_max': 12.36}
- `joint_small.bus`: {'ibat_max': 6.653, 'vbat_min': 39, 'vbat_max': 54.6, 'vbat_nom': 48}
- `joint_small.can`: {}
- `joint_small.conn2`: {'pins': [{'name': 'canh'}, {'name': 'canl'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 0.0, 'kind': 'can_bus', 'label': 'link'}
- `joint_small.drv`: {'vbus_max': 54.6, 'vbus_nom': 48.0, 'phase_current_a': 6}
- `joint_small.mcu`: {} requests=[('pwm_drv', 'pwm'), ('en_drv', 'gpio'), ('cur_sense', 'adc'), ('can0', 'can')]

## Interfaces (links)

| A | B | Note |
|--|--|--|
| battery.out | joint_large.bus.src |  |
| joint_large.bus.vbus | joint_large.buck12.vin |  |
| joint_large.bus.vbus | joint_large.drv.vbus |  |
| joint_large.buck12.vout | joint_large.buck33.vin |  |
| joint_large.buck12.vout | joint_large.drv.vgate |  |
| joint_large.buck33.vout | joint_large.mcu.vdd |  |
| joint_large.buck33.vout | joint_large.can.vcc |  |
| joint_large.buck33.vout | joint_large.drv.vlogic |  |
| joint_large.mcu.pwm_drv | joint_large.drv.pwm |  |
| joint_large.mcu.en_drv | joint_large.drv.enable |  |
| joint_large.mcu.cur_sense | joint_large.drv.sense |  |
| joint_large.mcu.can0 | joint_large.can.logic |  |
| joint_large.drv.phase | act_large.phase |  |
| joint_large.can.bus | joint_large.conn1.link | auto-connector |
| joint_large.conn1.link | can_master.bus |  |
| battery.out | joint_small.bus.src |  |
| joint_small.bus.vbus | joint_small.buck12.vin |  |
| joint_small.bus.vbus | joint_small.drv.vbus |  |
| joint_small.buck12.vout | joint_small.buck33.vin |  |
| joint_small.buck12.vout | joint_small.drv.vgate |  |
| joint_small.buck33.vout | joint_small.mcu.vdd |  |
| joint_small.buck33.vout | joint_small.can.vcc |  |
| joint_small.buck33.vout | joint_small.drv.vlogic |  |
| joint_small.mcu.pwm_drv | joint_small.drv.pwm |  |
| joint_small.mcu.en_drv | joint_small.drv.enable |  |
| joint_small.mcu.cur_sense | joint_small.drv.sense |  |
| joint_small.mcu.can0 | joint_small.can.logic |  |
| joint_small.drv.phase | act_small.phase |  |
| joint_small.can.bus | joint_small.conn2.link | auto-connector |
| joint_small.conn2.link | can_master.bus |  |
| battery.out | dcdc_aux.in |  |
| dcdc_aux.out | compute.pwr |  |

## Wiring harness

| From | To | Kind | Wires | AWG | Amps | Volts | Qty | Est $ each |
|--|--|--|--|--|--|--|--|--|
| battery.out | joint_large.bus.src | power | 2 | 6 | 60.95 | 54.6 | 12 | 18.6 |
| joint_large.drv.phase | act_large.phase | motor_phase | 3 | 12 | 18.75 | 48.0 | 12 | 5.7 |
| joint_large.can.bus | can_master.bus | can_bus | 2 | 26 | 0.1 | 0.0 | 12 | 0.78 |
| battery.out | joint_small.bus.src | power | 2 | 6 | 60.95 | 54.6 | 16 | 18.6 |
| joint_small.drv.phase | act_small.phase | motor_phase | 3 | 16 | 7.5 | 48.0 | 16 | 4.26 |
| joint_small.can.bus | can_master.bus | can_bus | 2 | 26 | 0.1 | 0.0 | 16 | 0.78 |
| battery.out | dcdc_aux.in | power | 2 | 6 | 60.95 | 75 | 1 | 18.6 |
| dcdc_aux.out | compute.pwr | power | 2 | 20 | 3.12 | 20 | 1 | 3.42 |

## Purchased items

| Qty | Item | Category | Unit USD | Total USD | Source | Notes |
|--|--|--|--|--|--|--|
| 1 | 48 V 13S Li-ion pack with BMS, ~1.2 kWh, 60 A continuous | battery | 1,500.00* | 1,500.00 | e.g. battery pack vendor (search needed) |  |
| 12 | BLDC joint actuator, high torque (planetary, 15 A rms) | actuator | 850.00* | 10,200.00 | search needed |  |
| 16 | BLDC joint actuator, medium torque (6 A rms) | actuator | 320.00* | 5,120.00 | search needed |  |
| 1 | 48V->12V 10A DC-DC converter module (aux rail for compute/cameras) | power | 70.00* | 70.00 | search needed |  |
| 1 | NVIDIA Jetson Orin NX 16GB developer kit (perception + planning) | compute | 700.00* | 700.00 | search needed |  |
| 1 | USB-to-CAN adapter (isolated, 1 Mbit/s) | module | 70.00* | 70.00 | search needed |  |
| 2 | Stereo depth cameras x2 | sensor | 350.00* | 700.00 | search needed |  |
| 1 | 9-axis IMU module (torso) | sensor | 40.00* | 40.00 | search needed |  |
| 1 | Aluminium/CFRP frame, fasteners, covers (150-170 cm) | mechanical | 4,500.00* | 4,500.00 | search needed |  |
| 1 | 48 V battery charger | power | 250.00* | 250.00 | search needed |  |
| 1 | Emergency stop + contactor + main fuse | power | 220.00* | 220.00 | search needed |  |

\* planning estimate, not a quote.

## Issues

- **warning**: battery.out: load 48.8 A is 81% of rated 60 A
- **warning**: joint_large.bus: battery_input: 16.553 A is beyond SMD fuse capability - place an inline fuse (ANL/MIDI) or a BMS with short-circuit protection in the battery harness
- **warning**: joint_large.drv: motor_driver: each MOSFET dissipates ~1.8 W - TO-220 tabs need a heatsink (or thermal pad to chassis); board estimate assumes none

## Assumptions

- 28 actuated degrees of freedom (12 large 15 A joints, 16 small 6 A joints), 48 V (13S Li-ion) bus
- joints seldom peak simultaneously: 15% fleet duty on the bus
- perception/planning runs on a purchased compute module (no custom compute board)
- actuator/structure/battery prices are planning estimates (no live quote)
- firmware, mechanical design and software stack are out of scope

## What this tool does and does not guarantee

- Deterministic stages (resolver, placement, routing, DRC, Gerber/drill/BOM export) are reproducible and independently re-checked.
- Electrical design uses datasheet equations but is **not simulated** (loop stability, EMC, thermal in an enclosure). Prototype and measure.
- Mechanical parts, actuators, and software/firmware are outside what this tool designs; they appear as purchased items with planning-grade costs.
- Review the per-board REPORT.md checklist before releasing any board to fabrication.
