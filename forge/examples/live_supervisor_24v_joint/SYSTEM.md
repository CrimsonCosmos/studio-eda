# bldc_joint_controller_24v - system design

> Request: Single 24 V brushless-motor joint controller with CAN bus control and one I2C IMU. Prefer buying standard modules when cheaper than building.

## Cost vs budget (estimates - refresh with live quotes before ordering)

| Item | USD |
|--|--|
| Custom boards (PCB + parts + assembly) | 68.35 |
| Purchased items | 214.00 |
| Harness / cables | 23.40 |
| Contingency (10%) | 30.58 |
| **Total** | **336.32** |
| Budget | 400.00 |
| **Within budget** | +63.68 |

## Buy vs build decisions

- joint.buck12: BUY '12 V 1 A DC-DC buck module (24 V in)' $8.00 vs build $47.11 - purchased module is no more expensive than building (and saves design/verification risk)
- joint.buck33: BUY '3.3 V 1 A DC-DC buck module' $6.00 vs build $46.92 - purchased module is no more expensive than building (and saves design/verification risk)
- joint.drv: BUY '24 V / 10 A BLDC FOC motor driver module' $60.00 vs build $83.92 - purchased module is no more expensive than building (and saves design/verification risk)

## Boards

| Board | Qty | Parts | Size (mm) | Layers | Unit cost | DRC | Notes |
|--|--|--|--|--|--|--|--|
| joint | 1 | 47 | - | - | $68.35 | not built | 24 V BLDC joint controller board |

## Power tree (demand propagated upstream, windows downstream)

- `joint.bus.vbus` must supply **10.37 A** (fleet qty x duty applied across boards)
- `joint_buck12.vout` must supply **0.22 A** (fleet qty x duty applied across boards)
- `joint_buck33.vout` must supply **0.12 A** (fleet qty x duty applied across boards)
- `psu.out` must supply **5.00 A** (fleet qty x duty applied across boards)

## Block parameters decided by the resolver

- `joint.bus`: {'vbat_nom': 24.0, 'vbat_max': 28.8, 'vbat_min': 19.0, 'ibat_max': 10.0, 'rail': 'VBAT'}
- `joint.can`: {'terminated': True, 'vcc': 3.3, 'bitrate': 1000000}
- `joint.conn1`: {'pins': [{'name': 'vcc', 'current_a': 12.964396284829721}, {'name': 'gnd'}], 'max_current_a': 12.964396284829721, 'voltage': 28.8, 'kind': 'power', 'label': 'link'}
- `joint.conn2`: {'pins': [{'name': 'vcc', 'current_a': 12.964396284829721}, {'name': 'gnd'}], 'max_current_a': 12.964396284829721, 'voltage': 28.8, 'kind': 'power', 'label': 'link'}
- `joint.conn3`: {'pins': [{'name': 'vcc', 'current_a': 0.14375000000000002}, {'name': 'gnd'}], 'max_current_a': 0.14375000000000002, 'voltage': 3.6, 'kind': 'power', 'label': 'link'}
- `joint.conn4`: {'pins': [{'name': 'vcc', 'current_a': 0.14375000000000002}, {'name': 'gnd'}], 'max_current_a': 0.14375000000000002, 'voltage': 3.6, 'kind': 'power', 'label': 'link'}
- `joint.conn5`: {'pins': [{'name': 'vcc', 'current_a': 0.14375000000000002}, {'name': 'gnd'}], 'max_current_a': 0.14375000000000002, 'voltage': 5.0, 'kind': 'power', 'label': 'link'}
- `joint.conn6`: {'pins': [{'name': 'canh'}, {'name': 'canl'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 0.0, 'kind': 'can_bus', 'label': 'link'}
- `joint.conn7`: {'pins': [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 3.3, 'kind': 'pwm', 'label': 'link'}
- `joint.conn8`: {'pins': [{'name': 'drv_en'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 3.3, 'kind': 'gpio', 'label': 'link'}
- `joint.conn9`: {'pins': [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}, {'name': 'gnd'}], 'max_current_a': 0.1, 'voltage': 3.3, 'kind': 'analog', 'label': 'link'}
- `joint.imu`: {'module': 'GY-521', 'pullups': True, 'vcc': 3.3}
- `joint.mcu`: {'status_led': True, 'vdd': 3.3} requests=[('can_if', 'can'), ('i2c_imu', 'i2c'), ('pwm_drv', 'pwm'), ('drv_en', 'gpio'), ('sense_adc', 'adc')]
- `joint.mount`: {'diameter': 3.2}

## Interfaces (links)

| A | B | Note |
|--|--|--|
| psu.out | joint.bus.src |  |
| joint.bus.vbus | joint.conn1.link | auto-connector |
| joint.conn1.link | joint_buck12.vin |  |
| joint.bus.vbus | joint.conn2.link | auto-connector |
| joint.conn2.link | joint_drv.vbus |  |
| joint_buck12.vout | joint_drv.vgate |  |
| joint_buck12.vout | joint_buck33.vin |  |
| joint.mcu.vdd | joint.conn3.link | auto-connector |
| joint_buck33.vout | joint.conn3.link |  |
| joint.can.vcc | joint.conn4.link | auto-connector |
| joint_buck33.vout | joint.conn4.link |  |
| joint.imu.vcc | joint.conn5.link | auto-connector |
| joint_buck33.vout | joint.conn5.link |  |
| joint_buck33.vout | joint_drv.vlogic |  |
| joint.mcu.can_if | joint.can.logic |  |
| joint.can.bus | joint.conn6.link | auto-connector |
| joint.conn6.link | can_host.bus |  |
| joint.mcu.i2c_imu | joint.imu.i2c |  |
| joint.mcu.pwm_drv | joint.conn7.link | auto-connector |
| joint.conn7.link | joint_drv.pwm |  |
| joint.mcu.drv_en | joint.conn8.link | auto-connector |
| joint.conn8.link | joint_drv.enable |  |
| joint.mcu.sense_adc | joint.conn9.link | auto-connector |
| joint.conn9.link | joint_drv.sense |  |
| joint_drv.phase | motor.phase |  |

## Wiring harness

| From | To | Kind | Wires | AWG | Amps | Volts | Qty | Est $ each |
|--|--|--|--|--|--|--|--|--|
| psu.out | joint.bus.src | power | 2 | 18 | 6.25 | 28.8 | 1 | 3.6 |
| joint.bus.vbus | joint_buck12.vin | power | 2 | 14 | 12.96 | 28.8 | 1 | 4.2 |
| joint.bus.vbus | joint_drv.vbus | power | 2 | 14 | 12.96 | 28.8 | 1 | 4.2 |
| joint_buck12.vout | joint_drv.vgate | power | 2 | 26 | 0.28 | 20.0 | 1 | 0.78 |
| joint_buck12.vout | joint_buck33.vin | power | 2 | 26 | 0.28 | 12.4 | 1 | 0.78 |
| joint_buck33.vout | joint.mcu.vdd | power | 2 | 26 | 0.14 | 3.6 | 1 | 0.78 |
| joint_buck33.vout | joint.can.vcc | power | 2 | 26 | 0.14 | 3.6 | 1 | 0.78 |
| joint_buck33.vout | joint.imu.vcc | power | 2 | 26 | 0.14 | 5.0 | 1 | 0.78 |
| joint_buck33.vout | joint_drv.vlogic | power | 2 | 26 | 0.14 | 5.5 | 1 | 0.78 |
| joint.can.bus | can_host.bus | can_bus | 1 | 26 | 0.1 | 0.0 | 1 | 0.69 |
| joint.mcu.pwm_drv | joint_drv.pwm | pwm | 3 | 26 | 0.1 | 3.3 | 1 | 0.87 |
| joint.mcu.drv_en | joint_drv.enable | gpio | 1 | 26 | 0.1 | 3.3 | 1 | 0.69 |
| joint.mcu.sense_adc | joint_drv.sense | analog | 3 | 26 | 0.1 | 3.3 | 1 | 0.87 |
| joint_drv.phase | motor.phase | motor_phase | 1 | 14 | 12.5 | 24.0 | 1 | 3.6 |

## Purchased items

| Qty | Item | Category | Unit USD | Total USD | Source | Notes |
|--|--|--|--|--|--|--|
| 1 | 24 V DC supply (or battery) for the joint | power | 45.00* | 45.00 | [Mean Well](https://www.meanwell.com/productPdf.aspx?i=459) |  |
| 1 | 24 V brushless DC motor (gimbal or servo class) | actuator | 70.00* | 70.00 | [Generic BLDC supplier](https://www.moteus.com/) |  |
| 1 | USB-to-CAN adapter (host side) | module | 25.00* | 25.00 | [CANable](https://www.canable.io/) |  |
| 1 | 12 V 1 A DC-DC buck module (24 V in) | module | 8.00* | 8.00 | [Pololu](https://www.pololu.com/category/131/step-down-voltage-regulators) | replaces block joint.buck12 (buck_converter) |
| 1 | 3.3 V 1 A DC-DC buck module | module | 6.00* | 6.00 | [Pololu](https://www.pololu.com/category/131/step-down-voltage-regulators) | replaces block joint.buck33 (buck_converter) |
| 1 | 24 V / 10 A BLDC FOC motor driver module | module | 60.00* | 60.00 | [Mouser](https://www.mouser.com/c/?q=bldc%20motor%20driver%20module) | replaces block joint.drv (motor_driver_3ph) |

\* planning estimate, not a quote.

## Issues

- none

## Assumptions

- 24 V nominal bus (19-28.8 V incl. charge/regen headroom) from a bench or battery supply, bought, up to 10 A.
- Motor: 24 V BLDC, about 5 A RMS continuous phase current, 10 A peak. A 10 A driver gives headroom.
- Single joint, qty 1, so BOM is dominated by purchased items. The custom board holds only application-specific glue: MCU, CAN, driver, IMU header and power rails.
- Fix for the failed link buck33 -> imu.vcc: the 3.3 V buck regulated to 3.28 V, below the IMU module's 3.3-5 V window. The buck vout is now 3.4 V. The resolver should then compute about 3.38 V, which is inside the IMU window (3.3-5 V) and inside the MCU (2.0-3.6 V), CAN transceiver (3.0-3.6 V) and driver logic limits.
- The 12 V gate-drive rail is made from the 24 V bus by a buck and also feeds the 3.3 V buck, so it works as an intermediate rail.
- CAN 2.0 at 1 Mbit/s. The bus is terminated at this node (jumper-selectable). The host or CAN adapter is outside this plan.
- Prices are estimates (cost_is_estimate=true), not live quotes. Refresh them from the supplier URLs before ordering.
- Running cost estimate: PSU about $45, motor about $70, CAN adapter about $25, custom board and parts about $60-90, PCB and assembly for qty 1 about $60. Total is roughly $280-300, under the $400 budget.

## What this tool does and does not guarantee

- Deterministic stages (resolver, placement, routing, DRC, Gerber/drill/BOM export) are reproducible and independently re-checked.
- Electrical design uses datasheet equations but is **not simulated** (loop stability, EMC, thermal in an enclosure). Prototype and measure.
- Mechanical parts, actuators, and software/firmware are outside what this tool designs; they appear as purchased items with planning-grade costs.
- Review the per-board REPORT.md checklist before releasing any board to fabrication.
