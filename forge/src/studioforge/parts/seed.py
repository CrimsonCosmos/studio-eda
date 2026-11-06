"""Seed catalog of real parts. Pinouts here are written from engineering knowledge and are
all `verified=False` until `forge parts verify` cross-checks them against the cached
official datasheet. The research agent extends the catalog at run time."""
from __future__ import annotations

from ..model import PartDef, PinDef
from .suppliers import search_offers

PI, PO, IN, OUT, BI, PA, OD = ("power_in", "power_out", "input", "output", "bidir", "passive", "open_collector")


def _p(num, name, et=PA, alt=()):
    return PinDef(str(num), name, et, tuple(alt))


def _part(mpn, mfr, desc, cat, pkg, pins, fp, params=None, price=None, ds="", emi="neutral", heat="cool",
          height=1.0, aliases=(), tags=(), notes="", breaks=None, suppliers=("digikey", "mouser", "lcsc")):
    return PartDef(mpn, mfr, desc, cat, pkg, list(pins), fp, params or {}, "", emi, heat, height, ds, [],
                   search_offers(mpn, price, suppliers, breaks), list(aliases), list(tags), "seed", False, notes)


def _stm32f103c8() -> PartDef:
    # (pin, name, type, default-mapping alternate functions)
    t = [
        (1, "VBAT", PI, ()), (2, "PC13", BI, ("TAMPER-RTC",)), (3, "PC14", BI, ("OSC32_IN",)), (4, "PC15", BI, ("OSC32_OUT",)),
        (5, "PD0", BI, ("OSC_IN",)), (6, "PD1", BI, ("OSC_OUT",)), (7, "NRST", IN, ()), (8, "VSSA", PI, ()), (9, "VDDA", PI, ()),
        (10, "PA0", BI, ("USART2_CTS", "ADC_IN0", "TIM2_CH1", "WKUP")), (11, "PA1", BI, ("USART2_RTS", "ADC_IN1", "TIM2_CH2")),
        (12, "PA2", BI, ("USART2_TX", "ADC_IN2", "TIM2_CH3")), (13, "PA3", BI, ("USART2_RX", "ADC_IN3", "TIM2_CH4")),
        (14, "PA4", BI, ("SPI1_NSS", "USART2_CK", "ADC_IN4")), (15, "PA5", BI, ("SPI1_SCK", "ADC_IN5")),
        (16, "PA6", BI, ("SPI1_MISO", "ADC_IN6", "TIM3_CH1", "TIM1_BKIN")), (17, "PA7", BI, ("SPI1_MOSI", "ADC_IN7", "TIM3_CH2", "TIM1_CH1N")),
        (18, "PB0", BI, ("ADC_IN8", "TIM3_CH3", "TIM1_CH2N")), (19, "PB1", BI, ("ADC_IN9", "TIM3_CH4", "TIM1_CH3N")),
        (20, "PB2", BI, ("BOOT1",)), (21, "PB10", BI, ("I2C2_SCL", "USART3_TX")), (22, "PB11", BI, ("I2C2_SDA", "USART3_RX")),
        (23, "VSS", PI, ()), (24, "VDD", PI, ()),
        (25, "PB12", BI, ("SPI2_NSS", "USART3_CK", "TIM1_BKIN")), (26, "PB13", BI, ("SPI2_SCK", "USART3_CTS", "TIM1_CH1N")),
        (27, "PB14", BI, ("SPI2_MISO", "USART3_RTS", "TIM1_CH2N")), (28, "PB15", BI, ("SPI2_MOSI", "TIM1_CH3N")),
        (29, "PA8", BI, ("USART1_CK", "TIM1_CH1", "MCO")), (30, "PA9", BI, ("USART1_TX", "TIM1_CH2")),
        (31, "PA10", BI, ("USART1_RX", "TIM1_CH3")), (32, "PA11", BI, ("USART1_CTS", "CAN_RX", "USB_DM", "TIM1_CH4")),
        (33, "PA12", BI, ("USART1_RTS", "CAN_TX", "USB_DP", "TIM1_ETR")), (34, "PA13", BI, ("SWDIO",)),
        (35, "VSS", PI, ()), (36, "VDD", PI, ()), (37, "PA14", BI, ("SWCLK",)), (38, "PA15", BI, ("JTDI",)),
        (39, "PB3", BI, ("JTDO",)), (40, "PB4", BI, ("NJTRST",)), (41, "PB5", BI, ("I2C1_SMBA",)),
        (42, "PB6", BI, ("I2C1_SCL", "TIM4_CH1")), (43, "PB7", BI, ("I2C1_SDA", "TIM4_CH2")), (44, "BOOT0", IN, ()),
        (45, "PB8", BI, ("TIM4_CH3",)), (46, "PB9", BI, ("TIM4_CH4",)), (47, "VSS", PI, ()), (48, "VDD", PI, ()),
    ]
    pins = [_p(n, nm, et, alt) for n, nm, et, alt in t]
    return _part("STM32F103C8T6", "STMicroelectronics", "ARM Cortex-M3 MCU 72MHz 64KB flash 20KB RAM, CAN/USB/3xUSART/2xSPI/2xI2C/ADC",
                 "ic.mcu", "LQFP-48", pins, "LQFP-48", {"vdd_min": 2.0, "vdd_max": 3.6, "icc_ma": 50, "vdd_pins": 3, "core": "cortex-m3"},
                 4.5, "https://www.st.com/resource/en/datasheet/stm32f103c8.pdf", emi="victim", heat="warm", height=1.6,
                 tags=["mcu", "can", "pwm", "timer1"], notes="NRND-class legacy part; fine as seed, planner may research newer (G4/H7) MCUs.")


def seed_parts() -> list[PartDef]:
    out: list[PartDef] = []
    out.append(_stm32f103c8())
    out.append(_part("TPS563201DDCR", "Texas Instruments", "17V 3A synchronous buck converter, Eco-mode, SOT-23-6", "ic.regulator.buck",
                     "SOT-23-6", [_p(1, "GND", PI), _p(2, "SW", PO), _p(3, "VIN", PI), _p(4, "VFB", IN), _p(5, "EN", IN), _p(6, "VBST", PA)],
                     "SOT-23-6", {"vin_min": 4.5, "vin_max": 17.0, "iout_max": 3.0, "vref": 0.768, "fsw": 580e3, "topology": "buck",
                                  "vout_min": 0.76, "vout_max": 7.0, "rds_on_sum": 0.108, "sync": True, "en_max": 17.0, "cout_min_uf": 20, "cout_max_uf": 68,
                                  "recommended": {"1.8": {"l_min_uh": 1.5, "l_max_uh": 4.7}, "2.5": {"l_min_uh": 2.2, "l_max_uh": 4.7}, "3.3": {"l_min_uh": 2.2, "l_max_uh": 4.7},
                                                  "5": {"l_min_uh": 3.3, "l_max_uh": 4.7}, "6.5": {"l_min_uh": 3.3, "l_max_uh": 4.7}}},
                     0.62, "https://www.ti.com/lit/ds/symlink/tps563201.pdf", "aggressor", "warm", 1.1, aliases=["TPS563201"], tags=["power", "buck"]))
    out.append(_part("TPS54360BDDAR", "Texas Instruments", "60V 3.5A step-down converter with Eco-mode, SO-PowerPAD-8", "ic.regulator.buck",
                     "SOIC-8-EP", [_p(1, "BOOT", PA), _p(2, "VIN", PI), _p(3, "EN", IN), _p(4, "RT/CLK", IN), _p(5, "FB", IN), _p(6, "COMP", PA),
                                   _p(7, "GND", PI), _p(8, "SW", PO), _p(9, "PAD", PI)],
                     "SOIC-8-EP", {"vin_min": 4.5, "vin_max": 60.0, "iout_max": 3.5, "vref": 0.8, "fsw": 500e3, "topology": "buck",
                                   "vout_min": 0.8, "vout_max": 58.0, "rds_on_sum": 0.2, "sync": False, "needs_catch_diode": True, "rds_on_hs": 0.092,
                                   "gm_ea": 350e-6, "gm_ps": 12.0, "vref_en": 1.2, "rt_500k": 200e3, "qg_nc": 3.0, "trise_ns": 4.9, "boot_uf": 0.1},
                     2.95, "https://www.ti.com/lit/ds/symlink/tps54360b.pdf", "aggressor", "hot", 1.7, aliases=["TPS54360B"], tags=["power", "buck", "hv"]))
    out.append(_part("AMS1117-3.3", "Advanced Monolithic Systems", "1A 3.3V LDO regulator SOT-223", "ic.regulator.ldo", "SOT-223",
                     [_p(1, "GND", PI), _p(2, "VOUT", PO), _p(3, "VIN", PI)], "SOT-223",
                     {"vin_max": 15.0, "vout": 3.3, "iout_max": 1.0, "dropout": 1.1, "topology": "ldo"}, 0.14,
                     "http://www.advanced-monolithic.com/pdf/ds1117.pdf", heat="warm", height=1.8, tags=["power", "ldo"]))
    out.append(_part("AP2112K-3.3TRG1", "Diodes Inc.", "600mA 3.3V LDO SOT-23-5", "ic.regulator.ldo", "SOT-23-5",
                     [_p(1, "VIN", PI), _p(2, "GND", PI), _p(3, "EN", IN), _p(4, "NC", "nc"), _p(5, "VOUT", PO)], "SOT-23-5",
                     {"vin_max": 6.0, "vout": 3.3, "iout_max": 0.6, "dropout": 0.25, "topology": "ldo"}, 0.30,
                     "https://www.diodes.com/assets/Datasheets/AP2112.pdf", tags=["power", "ldo"]))
    out.append(_part("IR2104SPBF", "Infineon", "600V half-bridge gate driver, single PWM input with shutdown, SOIC-8", "ic.gatedriver",
                     "SOIC-8", [_p(1, "VCC", PI), _p(2, "IN", IN), _p(3, "~SD", IN), _p(4, "COM", PI), _p(5, "LO", OUT), _p(6, "VS", PA),
                                _p(7, "HO", OUT), _p(8, "VB", PI)], "SOIC-8",
                     {"vcc_min": 10.0, "vcc_max": 20.0, "vs_max": 600.0, "io_source_a": 0.21, "io_sink_a": 0.36, "deadtime_ns": 520},
                     1.85, "", "aggressor", "warm", 1.75, aliases=["IR2104S", "IR2104"], tags=["gate-driver", "motor"]))
    out.append(_part("IRFB4110PBF", "Infineon", "100V 180A N-MOSFET TO-220, Rds(on) 4.5mΩ", "mosfet", "TO-220-3",
                     [_p(1, "G", IN), _p(2, "D", PA), _p(3, "S", PA)], "TO-220-3",
                     {"vds_max": 100.0, "id_max": 180.0, "rds_on": 0.0045, "qg_nc": 150.0, "qgd_nc": 43.0, "vgs_th": 3.0, "channel": "n", "pd_max_w": 370.0},
                     3.9, "", "aggressor", "hot", 4.5, aliases=["IRFB4110"], tags=["mosfet", "power", "motor"]))
    out.append(_part("IRLML6344TRPBF", "Infineon", "30V 5A logic-level N-MOSFET SOT-23", "mosfet", "SOT-23",
                     [_p(1, "G", IN), _p(2, "S", PA), _p(3, "D", PA)], "SOT-23",
                     {"vds_max": 30.0, "id_max": 5.0, "rds_on": 0.029, "qg_nc": 6.8, "vgs_th": 0.8, "channel": "n"}, 0.38, "", tags=["mosfet", "small"]))
    out.append(_part("2N7002", "Nexperia", "60V 300mA N-MOSFET SOT-23", "mosfet", "SOT-23",
                     [_p(1, "G", IN), _p(2, "S", PA), _p(3, "D", PA)], "SOT-23",
                     {"vds_max": 60.0, "id_max": 0.3, "rds_on": 2.5, "channel": "n"}, 0.05, "", tags=["mosfet", "small"]))
    out.append(_part("ES1J", "Generic", "600V 1A ultrafast rectifier SMA (bootstrap diode)", "diode", "D_SMA",
                     [_p(1, "K"), _p(2, "A")], "D_SMA", {"vr": 600.0, "if": 1.0, "trr_ns": 35}, 0.12, "", tags=["diode"]))
    out.append(_part("SS34", "Generic", "40V 3A Schottky rectifier SMA/DO-214AC", "diode", "D_SMA",
                     [_p(1, "K"), _p(2, "A")], "D_SMA", {"vr": 40.0, "if": 3.0, "vf": 0.5}, 0.13, "", tags=["diode"]))
    out.append(_part("B560C-13-F", "Diodes Inc.", "60V 5A Schottky rectifier SMC (buck catch diode)", "diode", "D_SMC",
                     [_p(1, "K"), _p(2, "A")], "D_SMC", {"vr": 60.0, "if": 5.0, "vf": 0.7}, 0.55, "", heat="warm", tags=["diode", "schottky"]))
    out.append(_part("SMBJ58A", "Generic", "58V standoff unidirectional TVS 600W SMB", "tvs", "D_SMB",
                     [_p(1, "K"), _p(2, "A")], "D_SMB", {"vrwm": 58.0, "vbr": 64.4, "vclamp": 93.6, "ppk_w": 600}, 0.40, "", tags=["protection"]))
    out.append(_part("SN65HVD230DR", "Texas Instruments", "3.3V CAN transceiver SOIC-8 (1Mbps)", "ic.interface.can", "SOIC-8",
                     [_p(1, "D", IN), _p(2, "GND", PI), _p(3, "VCC", PI), _p(4, "R", OUT), _p(5, "Vref", OUT), _p(6, "CANL", BI),
                      _p(7, "CANH", BI), _p(8, "Rs", IN)], "SOIC-8", {"vcc_min": 3.0, "vcc_max": 3.6, "bitrate": 1e6, "protocol": "can2.0"},
                     2.2, "https://www.ti.com/lit/ds/symlink/sn65hvd230.pdf", tags=["can", "interface"]))
    out.append(_part("LMV321IDBVR", "Texas Instruments", "Single rail-to-rail op amp SOT-23-5", "ic.opamp", "SOT-23-5",
                     [_p(1, "IN+", IN), _p(2, "GND", PI), _p(3, "IN-", IN), _p(4, "OUT", OUT), _p(5, "VCC+", PI)], "SOT-23-5",
                     {"vs_min": 2.7, "vs_max": 5.5, "gbw": 1e6}, 0.30, "https://www.ti.com/lit/ds/symlink/lmv321.pdf", emi="victim", tags=["analog"]))
    out.append(_part("XT60PW-M", "Amass", "XT60PW male PCB-mount power connector 30A", "connector.power", "XT60PW",
                     [_p(1, "+"), _p(2, "-")], "XT60PW", {"imax": 30.0, "vmax": 500.0}, 0.95, "", heat="warm", height=10.0,
                     tags=["connector", "power", "battery"], notes="Verify land pattern against the Amass datasheet before ordering boards."))
    out.append(_part("GY-521", "Generic", "MPU-6050 6-axis IMU breakout module (I2C), 8-pin 2.54mm header", "module.imu", "module-1x08",
                     [_p(1, "VCC", PI), _p(2, "GND", PI), _p(3, "SCL", IN), _p(4, "SDA", BI), _p(5, "XDA", BI), _p(6, "XCL", BI), _p(7, "AD0", IN), _p(8, "INT", OUT)],
                     "HDR-1x08", {"i2c_addr": [0x68, 0x69], "vcc_min": 3.3, "vcc_max": 5.0}, 2.5, "", emi="victim", tags=["imu", "i2c", "module"]))
    return out
