# joint_large - design report

## Summary

- Board: **125.0 x 92.6 mm**, **4 layers** (F.Cu, In1.Cu, In2.Cu, B.Cu), fab profile `jlcpcb-4l`
- Components: 124 (50 BOM lines), nets: 64
- Copper: 822 track segments, 421 vias, 10 zones; planes: {'In1.Cu': 'GND', 'In2.Cu': '+48V'}; pours: ['GND']
- Router: pitch 0.125 mm, rip-ups 0, neck-downs 0, unrouted: none
- DRC: **CLEAN** (clean); disconnected nets: 0
- Estimated BOM cost (1 board): **$54.90** (estimated prices - refresh before ordering), unpriced lines: 1

## Thermal

- Peak board temperature rise above ambient (steady state, still air): **104.7 C**
  - Q3: 1.81 W -> board +102.8 C (peak +104.7 C)
  - Q2: 1.81 W -> board +100.8 C (peak +104.2 C)
  - R28: 0.45 W -> board +98.3 C (peak +103.6 C)
  - Q5: 1.81 W -> board +97.7 C (peak +102.6 C)
  - R19: 0.45 W -> board +96.1 C (peak +99.3 C)
  - Q4: 1.81 W -> board +92.8 C (peak +101.6 C)
  - Q1: 1.81 W -> board +92.6 C (peak +96.7 C)
  - Q6: 1.81 W -> board +89.9 C (peak +95.8 C)

## EMI / signal-integrity checks

- Decoupling C6 -> U1.2: 2.58 mm (limit 2.5) OK
- Decoupling C14 -> U2.3: 1.98 mm (limit 2.5) OK
- Decoupling C18 -> U3.24: 2.68 mm (limit 3.0) OK
- Decoupling C19 -> U3.36: 2.64 mm (limit 3.0) OK
- Decoupling C20 -> U3.48: 2.69 mm (limit 3.0) OK
- Decoupling C21 -> U3.9: 2.63 mm (limit 3.0) OK
- Decoupling C22 -> U3.1: 2.68 mm (limit 3.0) OK
- Decoupling C28 -> U4.3: 2.22 mm (limit 2.5) OK
- Decoupling C34 -> Q1.2: 2.97 mm (limit 6.0) OK
- Decoupling C32 -> U6.1: 2.42 mm (limit 4.0) OK
- Decoupling C33 -> U6.1: 2.31 mm (limit 3.0) OK
- Decoupling C39 -> Q3.2: 4.23 mm (limit 6.0) OK
- Decoupling C37 -> U8.1: 2.57 mm (limit 4.0) OK
- Decoupling C38 -> U8.1: 1.9 mm (limit 3.0) OK
- Decoupling C44 -> Q5.2: 4.43 mm (limit 6.0) OK
- Decoupling C42 -> U10.1: 2.03 mm (limit 4.0) OK
- Decoupling C43 -> U10.1: 2.33 mm (limit 3.0) OK
- Hot loop `buck-hot-loop`: hull area 80.9 mm^2 (U1, C6, D2, C4)
- Hot loop `buck-hot-loop`: hull area 17.3 mm^2 (U2, C14, C12)
- Hot loop `half-bridge-A`: hull area 89.1 mm^2 (Q1, Q2, C34, R19)
- Hot loop `half-bridge-B`: hull area 60.6 mm^2 (Q3, Q4, C39, R28)
- Hot loop `half-bridge-C`: hull area 89.2 mm^2 (Q5, Q6, C44, R37)
- Crosstalk risk: mcu.OSC_OUT runs 4.7 mm within 1 mm of a noisy net on F.Cu
- Crosstalk risk: mcu.OSC_IN runs 4.3 mm within 1 mm of a noisy net on F.Cu
- Crosstalk risk: mcu.OSC_OUT runs 5.8 mm within 1 mm of a noisy net on B.Cu
- Crosstalk risk: mcu.OSC_IN runs 9.9 mm within 1 mm of a noisy net on B.Cu
- Tracks over a solid reference plane/pour: 91.9% of 1269.5 mm

## Warnings

- battery_input: 16.553 A is beyond SMD fuse capability - place an inline fuse (ANL/MIDI) or a BMS with short-circuit protection in the battery harness
- motor_driver: each MOSFET dissipates ~1.8 W - TO-220 tabs need a heatsink (or thermal pad to chassis); board estimate assumes none

## Design calculations (from deterministic block builders)

- `bus.battery_input`: 39-54.6 V, 16.553 A; TVS SMBJ58A; bulk 2x220uF/100V
- `buck12.uvlo`: EN divider 2.1M/68.1k: start 35.9 V stop 28.7 V
- `buck12.compensation`: fp_mod=66 Hz, fco=4.1 kHz, Rcomp=2.4k, Ccomp=1uF, Cpole=270pF
- `buck12.buck`: TPS54360BDDAR: 39-54.6 V -> 12.000 V @ 0.132 A, fsw 500 kHz, L=680uH (calc 473uH, ripple 0.03 A, Isat>=0.2 A), Cout 2x22uF (need 10 uF: transient 1, overshoot 1), Cin 2x2.2uF/100V, FB 140k/10k (Vout error +0.00%), loss 0.09 W IC, est. efficiency 90%
- `buck33.buck`: TPS563201DDCR: 11.64-12.36 V -> 3.318 V @ 0.209 A, fsw 580 kHz, L=4.7uH (calc 66.5uH, ripple 0.89 A, Isat>=0.8 A), Cout 2x22uF (need 20 uF: transient 3, overshoot 0), Cin 2x10uF/25V, FB 33.2k/10k (Vout error +0.54%), loss 0.08 W IC, est. efficiency 84%
- `mcu.pinmux`: can0: tx=PA12,rx=PA11; pwm_drv: a=PA8,b=PA9,c=PA10; cur_sense: a=PA0,b=PA1,c=PA2; en_drv: en_drv=PA3
- `can.can`: SN65HVD230DR: 1000 kbit/s, Rs->GND (fast slope), termination jumper-selectable 120R
- `drv.motor_driver`: IRFB4110PBF x6, IR2104SPBF x3; Rds(on) 4.5 mOhm: conduction 0.76 W + switching 1.05 W = 1.81 W per FET at 15 A rms / 48 V / 20 kHz (tsw 324 ns); Cbootstrap 2.2uF (need >= 1.5uF); Rg 10 ohm; shunt 2 mOhm, gain 20: +-0.85 V swing at 21 A peak around 1.65 V
- `conn1.connector`: link: JST-XH-3 (JST XH (3 A/pin)), 3 pins, 0.1 A/pin, 0 V

## Bill of materials

| Qty | Designators | MPN | Value | Package | Est. $ | Verified |
|--|--|--|--|--|--|--|
| 14 | C9,C17,C18,C19,C20,C21... | CAP-100nF-0402-16V-X7R | 100nF | 0402 | 0.006 | no |
| 3 | C33,C38,C43 | CAP-100nF-0402-25V-X7R | 100nF | 0402 | 0.006 | no |
| 1 | C6 | CAP-100nF-0603-100V-X7R | 100nF | 0603 | 0.025 | no |
| 1 | C14 | CAP-100nF-0603-50V-X7R | 100nF | 0603 | 0.017 | no |
| 1 | C23 | CAP-10uF-0805-10V-X5R | 10uF | 0805 | 0.168 | no |
| 2 | C12,C13 | CAP-10uF-0805-25V-X5R | 10uF | 0805 | 0.210 | no |
| 2 | C25,C26 | CAP-18pF-0402-50V-C0G | 18pF | 0402 | 0.008 | no |
| 1 | C24 | CAP-1uF-0402-10V-X7R | 1uF | 0402 | 0.008 | no |
| 1 | C10 | CAP-1uF-0402-50V-X7R | 1uF | 0402 | 0.013 | no |
| 3 | C31,C36,C41 | CAP-2.2uF-0603-25V-X5R | 2.2uF | 0603 | 0.028 | no |
| 6 | C3,C4,C5,C34,C39,C44 | CAP-2.2uF-1206-100V-X7R | 2.2uF | 1206 | 0.348 | no |
| 2 | C15,C16 | CAP-22uF-1206-10V-X5R | 22uF | 1206 | 0.852 | no |
| 2 | C7,C8 | CAP-22uF-1206-25V-X5R | 22uF | 1206 | 1.065 | no |
| 1 | C11 | CAP-270pF-0402-50V-C0G | 270pF | 0402 | 0.008 | no |
| 3 | C32,C37,C42 | CAP-4.7uF-0603-25V-X5R | 4.7uF | 0603 | 0.046 | no |
| 2 | C1,C2 | ECAP-220uF-100V-D10 | 220uF | radial-D10 | 0.344 | no |
| 2 | C46,C47 | ECAP-330uF-100V-D10 | 330uF | radial-D10 | 0.476 | no |
| 1 | D2 | B560C-13-F |  | D_SMC | 0.550 | no |
| 3 | D4,D5,D6 | ES1J |  | D_SMA | 0.120 | no |
| 1 | D3 | LED-green-0603 | green | 0603 | 0.030 | no |
| 1 | D1 | SMBJ58A |  | D_SMB | 0.400 | no |
| 4 | H1,H2,H3,H4 | MH-M3.2 | M3.2 | M3.2 | ? | no |
| 1 | J2 | HDR-1x05-2.54 | 1x5 | 1x5 | 0.200 | no |
| 1 | J4 | JST-XH-3 | XH3 | XH-3 | 0.340 | no |
| 1 | J3 | TB-5.08-3 | TB3 | TB-3 | 1.350 | no |
| 1 | J1 | XT60PW-M |  | XT60PW | 0.950 | no |
| 1 | JP1 | HDR-1x02-2.54 | 1x2 | 1x2 | 0.110 | no |
| 1 | L2 | IND-4.7uH-4x4-2A | 4.7uH | 4x4mm | 0.440 | no |
| 1 | L1 | IND-680uH-4x4-2A | 680uH | 4x4mm | 0.440 | no |
| 6 | Q1,Q2,Q3,Q4,Q5,Q6 | IRFB4110PBF |  | TO-220-3 | 3.900 | no |
| 6 | R15,R16,R24,R25,R33,R34 | RES-10-0603-1% | 10 | 0603 | 0.002 | no |
| 6 | R17,R18,R26,R27,R35,R36 | RES-100k-0603-1% | 100k | 0603 | 0.002 | no |
| 7 | R2,R8,R9,R10,R13,R14... | RES-10k-0402-1% | 10k | 0402 | 0.001 | no |
| 1 | R12 | RES-120-0603-1% | 120 | 0603 | 0.002 | no |
| 1 | R1 | RES-140k-0402-1% | 140k | 0402 | 0.001 | no |
| 7 | R11,R20,R21,R29,R30,R38... | RES-1k-0402-1% | 1k | 0402 | 0.001 | no |
| 1 | R3 | RES-2.1M-0603-1% | 2.1M | 0603 | 0.002 | no |
| 1 | R6 | RES-2.4k-0402-1% | 2.4k | 0402 | 0.001 | no |
| 1 | R5 | RES-200k-0402-1% | 200k | 0402 | 0.001 | no |
| 6 | R22,R23,R31,R32,R40,R41 | RES-20k-0402-1% | 20k | 0402 | 0.001 | no |
| 1 | R7 | RES-33.2k-0402-1% | 33.2k | 0402 | 0.001 | no |
| 1 | R4 | RES-68.1k-0402-1% | 68.1k | 0402 | 0.001 | no |
| 3 | R19,R28,R37 | SHUNT-2m-2512-1W | 2m | 2512 | 0.120 | no |
| 3 | U6,U8,U10 | IR2104SPBF |  | SOIC-8 | 1.850 | no |
| 4 | U5,U7,U9,U11 | LMV321IDBVR |  | SOT-23-5 | 0.300 | no |
| 1 | U4 | SN65HVD230DR |  | SOIC-8 | 2.200 | no |
| 1 | U3 | STM32F103C8T6 |  | LQFP-48 | 4.500 | no |
| 1 | U1 | TPS54360BDDAR |  | SOIC-8-EP | 2.950 | no |
| 1 | U2 | TPS563201DDCR |  | SOT-23-6 | 0.620 | no |
| 1 | Y1 | XTAL-8MHz-12pF-3225 | 8MHz | 3225 | 0.350 | no |

## Manufacturing-readiness checklist

The software verifies geometry (DRC), connectivity and format validity. It cannot replace these human checks:

- [x] DRC clean and every net connected (automated)
- [ ] Pinouts cross-checked against datasheets - **unverified: B560C-13-F, ES1J, SMBJ58A, XT60PW-M, IRFB4110PBF, IR2104SPBF, LMV321IDBVR, SN65HVD230DR, STM32F103C8T6, TPS54360BDDAR, TPS563201DDCR**
- [ ] Footprint land patterns compared to the manufacturer drawings (generated patterns are IPC-nominal; connectors/modules especially)
- [ ] Schematic reviewed by an engineer (block calculations are listed above; stability/EMC are not simulated)
- [ ] High-current nets reviewed (ampacity warnings above); thermal estimate is a 2-D model, not a substitute for testing
- [ ] Gerbers opened in an independent viewer (KiCad/Gerbv) and the fab's online DFM check passed
- [ ] Prices/stock refreshed from suppliers before ordering (`forge purchase`)

## System context

Board `joint_large` x12 in system `humanoid_robot`. Application-specific glue: FOC-capable MCU + 3-phase inverter + CAN node; no suitable off-the-shelf driver at this current/size/cost.
