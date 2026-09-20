"""The design plan of the D5 reference board, as data (`build_plan()` -> `design_plan.json`).

The plan is what the scripted conversation in `tests/test_e2e_reference_board.py` follows and what
`grader.py` grades against (its `intent` block is the connectivity the spec asks for). Every
coordinate here is derived, not guessed:

- schematic pin endpoints: the stock library pins of KiCad 10.0.6 / 10.99 (identical in both;
  dumped with `kiwrap.formats.libs.LibResolver` while writing this file) transformed the way the
  writer places them (docs/VERIFICATION.md claim 3: sheet = (Sx + px, Sy - py) at rotation 0,
  (Sx - py, Sy - px) at 90, (Sx - px, Sy + py) at 180, (Sx + py, Sy + px) at 270; the test checks
  a sample of the receipts' absolute pins against these numbers);
- board pad positions: the stock footprints' pad tables at rotation 0 (every footprint is
  placed unrotated so the KiCad 11 nightly's ipc11 placement path is exercised without the G16
  rotation guard, docs/UPSTREAM.md);
- the courtyard boxes the placement was checked against are the footprints' F.CrtYd rings.

Symbols are placed first, wires afterwards: the writer refuses a wire that passes over a pin
end, so rails END at pins or stubs T onto rails (AGENTS.md authoring rules).

Run `uv run python -m evals.reference_board.plan` to regenerate `design_plan.json`;
`tests/test_e2e_reference_board.py::test_design_plan_json_is_current` keeps the two in sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLAN_PATH = Path(__file__).with_name("design_plan.json")

Point = list[float]


def pin(ref: str, number: str) -> dict[str, Any]:
    return {"ref": ref, "pin": number}


def at(x: float, y: float) -> dict[str, Any]:
    return {"at_mm": [x, y]}


def _r(v: float) -> float:
    return round(v, 4)


# ---- parts ------------------------------------------------------------------------------------------
# ref, lib_id, value, fpid, sheet, schematic origin, schematic rotation, board placement, role
# fmt: off
_PARTS: list[tuple[str, str, str | None, str, str, Point, int, Point, str]] = [
    # power sheet: USB-C -> CC pulldowns / ESD -> MCP73831 charger -> +BATT -> AMS1117 -> +3V3
    ("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", "power", [38.1, 88.9], 0, [25.0, 31.5], "USB-C 2.0 receptacle (power + data), bottom edge"),
    ("R1", "Device:R", "5.1k", "Resistor_SMD:R_0603_1608Metric", "power", [60.96, 78.74], 90, [33.5, 23.5], "CC1 pulldown (USB sink)"),
    ("R2", "Device:R", "5.1k", "Resistor_SMD:R_0603_1608Metric", "power", [60.96, 81.28], 90, [33.5, 26.0], "CC2 pulldown (USB sink)"),
    ("U2", "Power_Protection:USBLC6-2SC6", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", "power", [76.2, 88.9], 0, [25.0, 23.5], "USB ESD protection"),
    ("C1", "Device:C", "10u", "Capacitor_SMD:C_0603_1608Metric", "power", [91.44, 78.74], 0, [17.0, 23.5], "VBUS bulk"),
    ("U3", "Battery_Management:MCP73831-2-OT", "MCP73831-2-OT", "Package_TO_SOT_SMD:SOT-23-5", "power", [114.3, 88.9], 0, [5.0, 12.0], "1-cell LiPo charger"),
    ("R3", "Device:R", "2k", "Resistor_SMD:R_0603_1608Metric", "power", [99.06, 96.52], 0, [9.5, 10.5], "PROG: 500 mA charge current"),
    ("D1", "Device:LED", "CHG", "LED_SMD:LED_0603_1608Metric", "power", [132.08, 91.44], 0, [5.0, 8.5], "charge status LED (STAT low = charging)"),
    ("R4", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "power", [140.97, 91.44], 270, [9.5, 8.5], "CHG LED series resistor"),
    ("C2", "Device:C", "10u", "Capacitor_SMD:C_0603_1608Metric", "power", [129.54, 78.74], 0, [9.5, 13.0], "VBAT bulk (charger output)"),
    ("J2", "Connector_Generic:Conn_01x02", "BAT", "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical", "power", [157.48, 81.28], 0, [3.5, 3.5], "LiPo battery connector (JST-PH), left edge"),
    ("U4", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "power", [177.8, 88.9], 0, [7.0, 23.0], "3.3 V LDO"),
    ("C3", "Device:C", "10u", "Capacitor_SMD:C_0603_1608Metric", "power", [165.1, 93.98], 0, [5.5, 29.5], "LDO input"),
    ("C4", "Device:C", "10u", "Capacitor_SMD:C_0603_1608Metric", "power", [190.5, 93.98], 0, [10.0, 29.5], "LDO output"),
    ("R5", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "power", [203.2, 93.98], 0, [10.0, 32.5], "power LED series resistor"),
    ("D2", "Device:LED", "PWR", "LED_SMD:LED_0603_1608Metric", "power", [203.2, 101.6], 90, [5.5, 32.5], "3V3 power LED"),
    # mcu sheet: the module, decoupling, EN reset circuit, I2C pull-ups
    ("U1", "RF_Module:ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1", "RF_Module:ESP32-S3-WROOM-1", "mcu", [101.6, 76.2], 0, [25.0, 6.75], "ESP32-S3 module, antenna over the top edge (keep-out off-board)"),
    ("C5", "Device:C", "10u", "Capacitor_SMD:C_0603_1608Metric", "mcu", [127.0, 46.99], 0, [13.0, 2.76], "3V3 bulk at the module"),
    ("C6", "Device:C", "100n", "Capacitor_SMD:C_0603_1608Metric", "mcu", [134.62, 46.99], 0, [13.0, 5.0], "3V3 decoupling"),
    ("R6", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "mcu", [66.04, 46.99], 0, [13.0, 7.5], "EN pull-up"),
    ("C7", "Device:C", "100n", "Capacitor_SMD:C_0603_1608Metric", "mcu", [66.04, 59.69], 0, [13.0, 10.0], "EN RC delay"),
    ("R7", "Device:R", "4.7k", "Resistor_SMD:R_0603_1608Metric", "mcu", [78.74, 71.12], 0, [13.0, 14.0], "SDA pull-up"),
    ("R8", "Device:R", "4.7k", "Resistor_SMD:R_0603_1608Metric", "mcu", [71.12, 71.12], 0, [13.0, 16.5], "SCL pull-up"),
    # io sheet: buttons, user LED, two I2C headers
    ("SW1", "Switch:SW_Push", "RESET", "Button_Switch_SMD:SW_SPST_TL3342", "io", [76.2, 50.8], 0, [44.0, 4.0], "reset button (EN to GND)"),
    ("SW2", "Switch:SW_Push", "BOOT", "Button_Switch_SMD:SW_SPST_TL3342", "io", [76.2, 63.5], 0, [44.0, 11.0], "boot button (IO0 to GND)"),
    ("R9", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "io", [71.12, 76.2], 90, [45.0, 30.5], "user LED series resistor"),
    ("D3", "Device:LED", "USER", "LED_SMD:LED_0603_1608Metric", "io", [78.74, 76.2], 180, [39.5, 30.5], "user LED on IO48"),
    ("J3", "Connector_Generic:Conn_01x04", "I2C", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "io", [111.76, 50.8], 0, [39.5, 17.5], "I2C header A: 3V3 SDA SCL GND"),
    ("J4", "Connector_Generic:Conn_01x04", "I2C", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "io", [111.76, 68.58], 0, [46.0, 17.5], "I2C header B: 3V3 SDA SCL GND"),
]

# library pin offsets (Y-up, mm) of the symbols used, from the stock libraries of 10.0.6 / 10.99
_LIB_PINS: dict[str, dict[str, tuple[float, float]]] = {
    "Connector:USB_C_Receptacle_USB2.0_16P": {
        "A1": (0.0, -22.86), "A4": (15.24, 15.24), "A5": (15.24, 10.16), "A6": (15.24, -2.54),
        "A7": (15.24, 2.54), "A8": (15.24, -12.7), "A9": (15.24, 15.24), "A12": (0.0, -22.86),
        "B1": (0.0, -22.86), "B4": (15.24, 15.24), "B5": (15.24, 7.62), "B6": (15.24, -5.08),
        "B7": (15.24, 0.0), "B8": (15.24, -15.24), "B9": (15.24, 15.24), "B12": (0.0, -22.86),
        "SH": (-7.62, -22.86),
    },
    "Device:R": {"1": (0.0, 3.81), "2": (0.0, -3.81)},
    "Device:C": {"1": (0.0, 3.81), "2": (0.0, -3.81)},
    "Device:LED": {"1": (-3.81, 0.0), "2": (3.81, 0.0)},
    "Power_Protection:USBLC6-2SC6": {
        "1": (-5.08, 0.0), "2": (0.0, -7.62), "3": (-5.08, -2.54), "4": (5.08, -2.54), "5": (0.0, 5.08),
        "6": (5.08, 0.0),
    },
    "Battery_Management:MCP73831-2-OT": {
        "1": (10.16, -2.54), "2": (0.0, -7.62), "3": (10.16, 2.54), "4": (0.0, 7.62), "5": (-10.16, -2.54)
    },
    "Connector_Generic:Conn_01x02": {"1": (-5.08, 0.0), "2": (-5.08, -2.54)},
    "Connector_Generic:Conn_01x04": {"1": (-5.08, 2.54), "2": (-5.08, 0.0), "3": (-5.08, -2.54), "4": (-5.08, -5.08)},
    "Regulator_Linear:AMS1117-3.3": {"1": (0.0, -7.62), "2": (7.62, 0.0), "3": (-7.62, 0.0)},
    "Switch:SW_Push": {"1": (-5.08, 0.0), "2": (5.08, 0.0)},
    "RF_Module:ESP32-S3-WROOM-1": {
        "1": (0.0, -27.94), "2": (0.0, 27.94), "3": (-15.24, 22.86), "4": (-15.24, 7.62), "5": (-15.24, 5.08),
        "6": (-15.24, 2.54), "7": (-15.24, 0.0), "8": (-15.24, -20.32), "9": (-15.24, -22.86),
        "10": (15.24, 17.78), "11": (15.24, 15.24), "12": (-15.24, -2.54), "13": (15.24, 12.7),
        "14": (15.24, 10.16), "15": (-15.24, 10.16), "16": (15.24, -17.78), "17": (-15.24, -5.08),
        "18": (-15.24, -7.62), "19": (-15.24, -10.16), "20": (-15.24, -12.7), "21": (-15.24, -15.24),
        "22": (-15.24, -17.78), "23": (15.24, 7.62), "24": (15.24, -20.32), "25": (15.24, -22.86),
        "26": (15.24, -15.24), "27": (-15.24, 17.78), "28": (15.24, 5.08), "29": (15.24, 2.54),
        "30": (15.24, 0.0), "31": (15.24, -2.54), "32": (15.24, -5.08), "33": (15.24, -7.62),
        "34": (15.24, -10.16), "35": (15.24, -12.7), "36": (15.24, 20.32), "37": (15.24, 22.86),
        "38": (-15.24, 12.7), "39": (-15.24, 15.24), "40": (0.0, -27.94), "41": (0.0, -27.94),
    },
}

# fmt: on
ESP32_USED_PINS = {"1", "2", "3", "12", "13", "14", "17", "25", "27", "40", "41"}
ESP32_NC_PINS = sorted((set(_LIB_PINS["RF_Module:ESP32-S3-WROOM-1"]) - ESP32_USED_PINS), key=int)


def sheet_pin(lib_id: str, origin: Point, rotation: int, number: str) -> Point:
    """Absolute schematic endpoint of a library pin (the writer's transform, claim 3)."""
    px, py = _LIB_PINS[lib_id][number]
    sx, sy = origin
    if rotation == 0:
        return [_r(sx + px), _r(sy - py)]
    if rotation == 90:
        return [_r(sx - py), _r(sy - px)]
    if rotation == 180:
        return [_r(sx - px), _r(sy + py)]
    return [_r(sx + py), _r(sy + px)]


def expected_pins() -> dict[str, dict[str, Point]]:
    """ref -> pin number -> absolute schematic endpoint, for every part (the test checks the
    receipts' `pins[]` against this)."""
    out: dict[str, dict[str, Point]] = {}
    for ref, lib_id, _v, _f, _s, origin, rot, _p, _role in _PARTS:
        out[ref] = {n: sheet_pin(lib_id, origin, rot, n) for n in _LIB_PINS[lib_id]}
    return out


# ---- board pad tables (rotation 0) ----------------------------------------------------------------------
# fmt: off
_FP_PADS: dict[str, dict[str, tuple[float, float]]] = {
    "Resistor_SMD:R_0603_1608Metric": {"1": (-0.825, 0.0), "2": (0.825, 0.0)},
    "Capacitor_SMD:C_0603_1608Metric": {"1": (-0.775, 0.0), "2": (0.775, 0.0)},
    "LED_SMD:LED_0603_1608Metric": {"1": (-0.7875, 0.0), "2": (0.7875, 0.0)},
    "Package_TO_SOT_SMD:SOT-23-5": {
        "1": (-1.1375, -0.95), "2": (-1.1375, 0.0), "3": (-1.1375, 0.95), "4": (1.1375, 0.95), "5": (1.1375, -0.95)
    },
    "Package_TO_SOT_SMD:SOT-23-6": {
        "1": (-1.1375, -0.95), "2": (-1.1375, 0.0), "3": (-1.1375, 0.95), "4": (1.1375, 0.95), "5": (1.1375, 0.0),
        "6": (1.1375, -0.95),
    },
    "Package_TO_SOT_SMD:SOT-223-3_TabPin2": {"1": (-3.15, -2.3), "2": (-3.15, 0.0), "2T": (3.15, 0.0), "3": (-3.15, 2.3)},
    "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical": {"1": (0.0, 0.0), "2": (2.0, 0.0)},
    "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical": {"1": (0.0, 0.0), "2": (0.0, 2.54), "3": (0.0, 5.08), "4": (0.0, 7.62)},
    "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12": {
        "A1": (-3.25, -4.045), "A4": (-2.45, -4.045), "A5": (-1.25, -4.045), "A6": (-0.25, -4.045),
        "A7": (0.25, -4.045), "A8": (1.25, -4.045), "A9": (2.45, -4.045), "A12": (3.25, -4.045),
        "B1": (3.25, -4.045), "B4": (2.45, -4.045), "B5": (1.75, -4.045), "B6": (0.75, -4.045),
        "B7": (-0.75, -4.045), "B8": (-1.75, -4.045), "B9": (-2.45, -4.045), "B12": (-3.25, -4.045),
        "SH_L1": (-4.32, -3.13), "SH_L2": (-4.32, 1.05), "SH_R1": (4.32, -3.13), "SH_R2": (4.32, 1.05),
    },
    "Button_Switch_SMD:SW_SPST_TL3342": {"1": (-3.15, -1.9), "1R": (3.15, -1.9), "2": (-3.15, 1.9), "2R": (3.15, 1.9)},
    "RF_Module:ESP32-S3-WROOM-1": {"1": (-8.75, -5.26), "2": (-8.75, -3.99), "3": (-8.75, -2.72), "40": (8.75, -5.26)},
}
# fmt: on


def _placement(ref: str) -> tuple[str, Point]:
    for r, _l, _v, fpid, _s, _o, _rot, place, _role in _PARTS:
        if r == ref:
            return fpid, place
    raise KeyError(ref)


def board_pad(ref: str, number: str) -> Point:
    """Absolute board position of a pad at rotation 0 (`2T` = the SOT-223 tab, `SH_L1` etc. the
    USB-C shield pads, `1R`/`2R` the switch's second pad of a number)."""
    fpid, (x, y) = _placement(ref)
    px, py = _FP_PADS[fpid][number]
    return [_r(x + px), _r(y + py)]


def pad(ref: str, number: str) -> dict[str, Any]:
    """A routing point: the pad's centre as the board reports it (the test resolves it from
    kw_pcb_get; `near` disambiguates pads sharing a number: the SOT-223 tab, the USB-C shield)."""
    real = "SH" if number.startswith("SH_") else number.rstrip("TR")
    return {"pad": [ref, real], "near": board_pad(ref, number)}


def xy(x: float, y: float) -> dict[str, Any]:
    return {"at_mm": [_r(x), _r(y)]}


# ---- the plan --------------------------------------------------------------------------------------------


def build_plan() -> dict[str, Any]:
    parts = [
        {
            "ref": ref,
            "lib_id": lib_id,
            "value": value,
            "fpid": fpid,
            "sheet": sheet,
            "at_mm": origin,
            "rotation": rot,
            "place": {"at_mm": place, "rotation": 0},
            "role": role,
        }
        for ref, lib_id, value, fpid, sheet, origin, rot, place, role in _PARTS
    ]

    sheets = [
        {
            "name": "power",
            "file": "power",
            "at_mm": [25.4, 25.4],
            "size_mm": [38.1, 25.4],
            "pins": [
                {"name": "USB_DP", "side": "right", "kind": "bidirectional"},
                {"name": "USB_DN", "side": "right", "kind": "bidirectional"},
            ],
        },
        {
            "name": "mcu",
            "file": "mcu",
            "at_mm": [101.6, 25.4],
            "size_mm": [38.1, 38.1],
            "pins": [
                {"name": "USB_DP", "side": "left", "kind": "bidirectional"},
                {"name": "USB_DN", "side": "left", "kind": "bidirectional"},
                {"name": "EN", "side": "right", "kind": "input"},
                {"name": "IO0", "side": "right", "kind": "input"},
                {"name": "SDA", "side": "right", "kind": "bidirectional"},
                {"name": "SCL", "side": "right", "kind": "bidirectional"},
                {"name": "LED", "side": "right", "kind": "output"},
            ],
        },
        {
            "name": "io",
            "file": "io",
            "at_mm": [177.8, 25.4],
            "size_mm": [38.1, 38.1],
            "pins": [
                {"name": "EN", "side": "left", "kind": "output"},
                {"name": "IO0", "side": "left", "kind": "output"},
                {"name": "SDA", "side": "left", "kind": "bidirectional"},
                {"name": "SCL", "side": "left", "kind": "bidirectional"},
                {"name": "LED", "side": "left", "kind": "input"},
            ],
        },
    ]
    # root wires between sheet pins (resolved from the sheet receipts) with a local label each:
    # a root-sheet local label outranks the children's hierarchical labels, so the board nets are
    # /USB_DP, /EN, ... instead of /power/USB_DP
    root_wires = [
        {"from": ["power", "USB_DP"], "to": ["mcu", "USB_DP"], "label": "USB_DP"},
        {"from": ["power", "USB_DN"], "to": ["mcu", "USB_DN"], "label": "USB_DN"},
        {"from": ["mcu", "EN"], "to": ["io", "EN"], "label": "EN"},
        {"from": ["mcu", "IO0"], "to": ["io", "IO0"], "label": "IO0"},
        {"from": ["mcu", "SDA"], "to": ["io", "SDA"], "label": "SDA"},
        {"from": ["mcu", "SCL"], "to": ["io", "SCL"], "label": "SCL"},
        {"from": ["mcu", "LED"], "to": ["io", "LED"], "label": "LED"},
    ]

    wires: list[dict[str, Any]] = []

    def wire(sheet: str, *points: dict[str, Any]) -> None:
        wires.append({"sheet": sheet, "points": list(points)})

    # power sheet
    wire("power", pin("J1", "A5"), pin("R1", "1"))
    wire("power", pin("J1", "B5"), pin("R2", "1"))
    wire("power", pin("R1", "2"), at(68.58, 78.74), at(68.58, 83.82))
    wire("power", pin("R2", "2"), at(68.58, 81.28))
    wire("power", pin("J1", "A7"), at(58.42, 86.36), at(58.42, 88.9), pin("J1", "B7"))
    wire("power", at(58.42, 88.9), pin("U2", "1"))
    wire("power", pin("J1", "A6"), at(60.96, 91.44), at(60.96, 93.98), pin("J1", "B6"))
    wire("power", at(60.96, 91.44), pin("U2", "3"))
    wire("power", pin("J1", "A4"), at(60.96, 73.66))
    wire("power", pin("U3", "5"), at(99.06, 91.44), pin("R3", "1"))
    wire("power", pin("U3", "1"), pin("D1", "1"))
    wire("power", pin("D1", "2"), pin("R4", "2"))
    wire("power", pin("U4", "3"), at(162.56, 88.9))
    wire("power", pin("C3", "1"), at(165.1, 88.9))
    wire("power", pin("U4", "2"), at(193.04, 88.9))
    wire("power", pin("C4", "1"), at(190.5, 88.9))
    wire("power", pin("J1", "SH"), at(30.48, 116.84), at(38.1, 116.84), pin("J1", "A1"))
    # mcu sheet
    wire("mcu", pin("U1", "2"), at(101.6, 40.64), at(139.7, 40.64))
    wire("mcu", pin("C5", "1"), at(127.0, 40.64))
    wire("mcu", pin("C6", "1"), at(134.62, 40.64))
    wire("mcu", pin("U1", "3"), at(60.96, 53.34))
    wire("mcu", pin("R6", "2"), at(66.04, 53.34))
    wire("mcu", pin("C7", "1"), at(66.04, 53.34))
    wire("mcu", pin("U1", "27"), at(73.66, 58.42))
    wire("mcu", pin("U1", "12"), at(73.66, 78.74))
    wire("mcu", pin("U1", "17"), at(63.5, 81.28))
    wire("mcu", pin("R7", "2"), at(78.74, 78.74))
    wire("mcu", pin("R8", "2"), at(71.12, 81.28))
    wire("mcu", pin("U1", "13"), at(129.54, 63.5))
    wire("mcu", pin("U1", "14"), at(129.54, 66.04))
    wire("mcu", pin("U1", "25"), at(129.54, 99.06))
    # io sheet
    wire("io", pin("SW1", "1"), at(63.5, 50.8))
    wire("io", pin("SW2", "1"), at(63.5, 63.5))
    wire("io", at(63.5, 76.2), pin("R9", "1"))
    wire("io", pin("J3", "2"), at(93.98, 50.8))
    wire("io", pin("J3", "3"), at(93.98, 53.34))
    wire("io", pin("J4", "2"), at(93.98, 68.58))
    wire("io", pin("J4", "3"), at(93.98, 71.12))

    def pw(sheet: str, net: str, where: dict[str, Any], rotation: int = 0) -> dict[str, Any]:
        return {"sheet": sheet, "net": net, "rotation": rotation, **where}

    power = [
        pw("power", "VBUS", at(60.96, 73.66)),
        pw("power", "VBUS", pin("C1", "1")),
        pw("power", "VBUS", pin("U2", "5")),
        pw("power", "VBUS", pin("U3", "4")),
        pw("power", "VBUS", pin("R4", "1")),
        pw("power", "GND", at(68.58, 83.82)),
        pw("power", "GND", pin("U2", "2")),
        pw("power", "GND", pin("C1", "2")),
        pw("power", "GND", pin("U3", "2")),
        pw("power", "GND", pin("R3", "2")),
        pw("power", "GND", pin("C2", "2")),
        pw("power", "GND", pin("J2", "2")),
        pw("power", "GND", pin("U4", "1")),
        pw("power", "GND", pin("C3", "2")),
        pw("power", "GND", pin("C4", "2")),
        pw("power", "GND", pin("D2", "1")),
        pw("power", "GND", at(38.1, 116.84)),
        pw("power", "+BATT", pin("U3", "3")),
        pw("power", "+BATT", pin("C2", "1")),
        pw("power", "+BATT", pin("J2", "1")),
        pw("power", "+BATT", at(162.56, 88.9)),
        pw("power", "+3V3", at(193.04, 88.9)),
        pw("power", "+3V3", pin("R5", "1")),
        pw("mcu", "+3V3", at(139.7, 40.64)),
        pw("mcu", "+3V3", pin("R6", "1")),
        pw("mcu", "+3V3", pin("R7", "1")),
        pw("mcu", "+3V3", pin("R8", "1")),
        pw("mcu", "GND", pin("U1", "1")),
        pw("mcu", "GND", pin("C5", "2")),
        pw("mcu", "GND", pin("C6", "2")),
        pw("mcu", "GND", pin("C7", "2")),
        pw("io", "GND", pin("SW1", "2")),
        pw("io", "GND", pin("SW2", "2")),
        pw("io", "GND", pin("D3", "1")),
        pw("io", "+3V3", pin("J3", "1")),
        pw("io", "GND", pin("J3", "4")),
        pw("io", "+3V3", pin("J4", "1")),
        pw("io", "GND", pin("J4", "4")),
    ]
    pwr_flags = [
        {"sheet": "power", "net": "VBUS", "rotation": 0, **pin("J1", "A4")},
        {"sheet": "power", "net": "GND", "rotation": 180, **at(30.48, 116.84)},
    ]
    no_connects = [
        {"sheet": "power", "ref": "J1", "pins": ["A8", "B8"]},
        {"sheet": "power", "ref": "U2", "pins": ["4", "6"]},
        {"sheet": "mcu", "ref": "U1", "pins": ESP32_NC_PINS},
    ]

    def hl(sheet: str, text: str, kind: str, x: float, y: float, rotation: int) -> dict[str, Any]:
        return {"sheet": sheet, "text": text, "kind": kind, "at_mm": [x, y], "rotation": rotation}

    hier_labels = [
        # on the wires into the ESD array: the USBLC6 symbol does not join its two I/O1 (I/O2)
        # pins, so the lines are not passed through the part (pins 4 / 6 are no-connect)
        hl("power", "USB_DN", "bidirectional", 63.5, 88.9, 0),
        hl("power", "USB_DP", "bidirectional", 66.04, 91.44, 0),
        hl("mcu", "EN", "input", 60.96, 53.34, 180),
        hl("mcu", "IO0", "input", 73.66, 58.42, 180),
        hl("mcu", "SDA", "bidirectional", 73.66, 78.74, 180),
        hl("mcu", "SCL", "bidirectional", 63.5, 81.28, 180),
        hl("mcu", "USB_DN", "bidirectional", 129.54, 63.5, 0),
        hl("mcu", "USB_DP", "bidirectional", 129.54, 66.04, 0),
        hl("mcu", "LED", "output", 129.54, 99.06, 0),
        hl("io", "EN", "output", 63.5, 50.8, 180),
        hl("io", "IO0", "output", 63.5, 63.5, 180),
        hl("io", "LED", "input", 63.5, 76.2, 180),
        hl("io", "SDA", "bidirectional", 93.98, 50.8, 180),
        hl("io", "SCL", "bidirectional", 93.98, 53.34, 180),
        hl("io", "SDA", "bidirectional", 93.98, 68.58, 180),
        hl("io", "SCL", "bidirectional", 93.98, 71.12, 180),
    ]

    # ---- the intent: every net's pins (KiCad's netlist is compared against this) -------------------
    # fmt: off
    intent_nets: dict[str, list[list[str]]] = {
        "VBUS": [["J1", "A4"], ["J1", "A9"], ["J1", "B4"], ["J1", "B9"], ["C1", "1"], ["U2", "5"], ["U3", "4"], ["R4", "1"]],
        "GND": [
            ["J1", "A1"], ["J1", "A12"], ["J1", "B1"], ["J1", "B12"], ["J1", "SH"], ["R1", "2"], ["R2", "2"],
            ["U2", "2"], ["C1", "2"], ["U3", "2"], ["R3", "2"], ["C2", "2"], ["J2", "2"], ["U4", "1"],
            ["C3", "2"], ["C4", "2"], ["D2", "1"], ["U1", "1"], ["U1", "40"], ["U1", "41"], ["C5", "2"],
            ["C6", "2"], ["C7", "2"], ["SW1", "2"], ["SW2", "2"], ["D3", "1"], ["J3", "4"], ["J4", "4"],
        ],
        "+BATT": [["U3", "3"], ["C2", "1"], ["J2", "1"], ["U4", "3"], ["C3", "1"]],
        "+3V3": [
            ["U4", "2"], ["C4", "1"], ["R5", "1"], ["U1", "2"], ["C5", "1"], ["C6", "1"], ["R6", "1"],
            ["R7", "1"], ["R8", "1"], ["J3", "1"], ["J4", "1"],
        ],
        "/USB_DP": [["J1", "A6"], ["J1", "B6"], ["U2", "3"], ["U1", "14"]],
        "/USB_DN": [["J1", "A7"], ["J1", "B7"], ["U2", "1"], ["U1", "13"]],
        "/EN": [["U1", "3"], ["R6", "2"], ["C7", "1"], ["SW1", "1"]],
        "/IO0": [["U1", "27"], ["SW2", "1"]],
        "/SDA": [["U1", "12"], ["R7", "2"], ["J3", "2"], ["J4", "2"]],
        "/SCL": [["U1", "17"], ["R8", "2"], ["J3", "3"], ["J4", "3"]],
        "/LED": [["U1", "25"], ["R9", "1"]],
    }
    intent_unnamed = [
        [["J1", "A5"], ["R1", "1"]],
        [["J1", "B5"], ["R2", "1"]],
        [["U3", "5"], ["R3", "1"]],
        [["U3", "1"], ["D1", "1"]],
        [["D1", "2"], ["R4", "2"]],
        [["R5", "2"], ["D2", "2"]],
        [["R9", "2"], ["D3", "2"]],
    ]
    intent_nc = [["J1", "A8"], ["J1", "B8"], ["U2", "4"], ["U2", "6"]] + [["U1", n] for n in ESP32_NC_PINS]
    # fmt: on

    # ---- board ---------------------------------------------------------------------------------------
    board = {
        "w_mm": 50.0,
        "h_mm": 35.0,
        "layers": 2,
        # kw_pcb_setup preset (kiwrap.core.fab.jlcpcb PRESET_2LAYER); the explicit rules below are
        # the preset's numbers for a server without presets (JLCPCB 2-layer safe defaults: 5/5 mil,
        # 0.3/0.6 mm via, 0.2 mm min hole - the ESP32-S3-WROOM-1 footprint's thermal vias are
        # 0.2 mm - and 0.3 mm copper-to-edge)
        "preset": "jlcpcb-2layer",
        "rules": {
            "clearance_mm": 0.15,
            "track_width_mm": 0.25,
            "via_size_mm": 0.6,
            "via_drill_mm": 0.3,
            "min_track_mm": 0.127,
            "min_via_mm": 0.5,
            "min_via_annular_mm": 0.13,
            "min_hole_mm": 0.2,
            "hole_to_hole_mm": 0.45,
            "hole_clearance_mm": 0.25,
            "copper_edge_clearance_mm": 0.3,
            "silk_clearance_mm": 0.15,
        },
        "netclasses": {"Power": {"nets": ["GND", "+3V3", "+BATT", "VBUS"], "track_width_mm": 0.4}},
        # GND pours, solid pad connections (thermal false: the decoupling next to the module
        # boxes its GND pads in and KiCad's starved-thermal rule of 2 spokes would fail them).
        # `zones_before_routing` is the bottom plane the router works against (Specctra plane:
        # Freerouting stitches every top-side GND pad to it with a via instead of drawing GND
        # tracks); `zones_after_routing` adds the top pour once the copper is in. A both-sides
        # pour BEFORE routing does the opposite: the plane outline covers every F.Cu GND pad, so
        # the router treats them as connected and KiCad's real fill leaves them on islands
        # (verified on 10.0.6: 8 unconnected + 3 starved thermals).
        "zones_before_routing": [
            {
                "net": "GND",
                "layers": ["B.Cu"],
                "rect_mm": [0, 0, 50, 35],
                "clearance_mm": 0.3,
                "thermal": False,
            }
        ],
        "zones_after_routing": [
            {
                "net": "GND",
                "layers": ["F.Cu"],
                "rect_mm": [0, 0, 50, 35],
                "clearance_mm": 0.3,
                "thermal": False,
            }
        ],
        # placement review items the check is expected to list (courtyard-only overhangs, no copper)
        "expected_off_board_review": ["U1", "J1"],
    }

    # ---- routing: the autorouter when the server has it; else these power-net primitives -------------
    def track(net: str, layer: str, *points: dict[str, Any]) -> dict[str, Any]:
        return {"op": "track", "net": net, "layer": layer, "points": list(points)}

    def via(net: str, x: float, y: float) -> dict[str, Any]:
        return {"op": "via", "net": net, "at_mm": [_r(x), _r(y)]}

    fallback_ops = [
        # +3V3: a trunk at x = 11.4 down the decoupling column into the LDO tab; T-stubs to the caps
        track("+3V3", "F.Cu", pad("C5", "1"), xy(11.4, 2.76), xy(11.4, 23.0), pad("U4", "2T")),
        track("+3V3", "F.Cu", pad("C6", "1"), xy(11.4, 5.0)),
        track("+3V3", "F.Cu", pad("R6", "1"), xy(11.4, 7.5)),
        track("+3V3", "F.Cu", pad("R7", "1"), xy(11.4, 14.0)),
        track("+3V3", "F.Cu", pad("R8", "1"), xy(11.4, 16.5)),
        track("+3V3", "F.Cu", xy(11.4, 3.9), xy(14.8, 3.9), xy(14.8, 2.76), pad("U1", "2")),
        track("+3V3", "F.Cu", pad("U4", "2"), pad("U4", "2T")),
        track("+3V3", "F.Cu", pad("U4", "2T"), xy(9.225, 26.0), pad("C4", "1"), pad("R5", "1")),
        track("+3V3", "F.Cu", xy(11.4, 21.0), xy(37.0, 21.0), xy(37.0, 17.5), pad("J3", "1"), pad("J4", "1")),
        # VBUS: connector -> C1 -> ESD; a B.Cu run to the charger VDD and the CHG LED resistor
        track("VBUS", "F.Cu", pad("J1", "A4"), xy(22.55, 26.0), xy(16.225, 26.0), pad("C1", "1")),
        track("VBUS", "F.Cu", pad("U2", "5"), xy(27.45, 23.5), pad("J1", "A9")),
        # KiCad joins same-net pads of a footprint only through copper: tie the A4 and A9 runs
        track("VBUS", "F.Cu", xy(22.55, 26.0), xy(27.45, 26.0)),
        track("VBUS", "F.Cu", pad("C1", "1"), xy(14.5, 23.5)),
        via("VBUS", 14.5, 23.5),
        track("VBUS", "B.Cu", xy(14.5, 23.5), xy(14.5, 11.8), xy(7.5, 11.8)),
        via("VBUS", 7.5, 11.8),
        track("VBUS", "F.Cu", xy(7.5, 11.8), xy(7.5, 12.95), pad("U3", "4")),
        track("VBUS", "F.Cu", xy(7.5, 11.8), xy(7.5, 8.5), pad("R4", "1")),
        # +BATT: battery connector -> charger VBAT / C2 -> LDO input / C3 along the left edge
        track("+BATT", "F.Cu", pad("J2", "1"), xy(2.2, 4.8), xy(2.2, 25.3), pad("U4", "3")),
        track("+BATT", "F.Cu", pad("U3", "3"), xy(2.2, 12.95)),
        track("+BATT", "F.Cu", pad("U3", "3"), xy(3.8625, 14.2), xy(8.725, 14.2), pad("C2", "1")),
        track("+BATT", "F.Cu", pad("U4", "3"), xy(4.725, 27.0), pad("C3", "1")),
        # GND: the USB-C's A1/B12 and B1/A12 pads onto the shield pads (the pour cannot reach them)
        track("GND", "F.Cu", pad("J1", "A1"), pad("J1", "SH_L1")),
        track("GND", "F.Cu", pad("J1", "B1"), pad("J1", "SH_R1")),
    ]

    return {
        "name": "esp32s3_ref",
        "title": "kiwrap reference board: ESP32-S3 dev board (D5)",
        "spec": "spec.md",
        "parts": parts,
        "sheets": sheets,
        "root_wires": root_wires,
        "wires": wires,
        "power": power,
        "pwr_flags": pwr_flags,
        "no_connects": no_connects,
        "hier_labels": hier_labels,
        # `nets` / `rails` / `constraints` / `blocks` are what kw_intent stores (kw_sch_check then
        # carries KiCad's intent diff); `unnamed` / `no_connect` are the grader's extra checks
        "intent": {
            "blocks": [
                {"name": "usb", "function": "USB-C 2.0 sink, ESD", "parts": ["J1", "R1", "R2", "U2", "C1"]},
                {
                    "name": "charger",
                    "function": "MCP73831 LiPo charger",
                    "parts": ["U3", "R3", "D1", "R4", "C2", "J2"],
                },
                {"name": "ldo", "function": "AMS1117-3.3", "parts": ["U4", "C3", "C4", "R5", "D2"]},
                {
                    "name": "mcu",
                    "function": "ESP32-S3-WROOM-1",
                    "parts": ["U1", "C5", "C6", "R6", "C7", "R7", "R8"],
                },
                {
                    "name": "io",
                    "function": "buttons, user LED, I2C headers",
                    "parts": ["SW1", "SW2", "R9", "D3", "J3", "J4"],
                },
            ],
            "rails": [
                {"name": "VBUS", "voltage_v": 5.0, "net": "VBUS"},
                {"name": "+BATT", "voltage_v": 3.7, "net": "+BATT"},
                {"name": "+3V3", "voltage_v": 3.3, "current_a": 0.6, "net": "+3V3"},
            ],
            "interfaces": [
                {"type": "usb2", "connector": "J1", "signals": ["/USB_DP", "/USB_DN"]},
                {"type": "i2c", "connector": "J3", "signals": ["/SDA", "/SCL"]},
                {"type": "i2c", "connector": "J4", "signals": ["/SDA", "/SCL"]},
            ],
            "nets": intent_nets,
            "constraints": {"size_mm": [50, 35], "layers": 2, "fab": "jlcpcb", "strict_nets": True},
            "unnamed": intent_unnamed,
            "no_connect": intent_nc,
        },
        "board": board,
        "routing": {
            "power_nets": ["GND", "+3V3", "+BATT", "VBUS"],
            "auto": {"passes": 100, "timeout_s": 900},
            "fallback_ops": fallback_ops,
        },
        "fab": {"profile": "jlcpcb", "targets": ["gerbers", "drill", "pos", "bom"]},
        "budget": {"max_tool_calls": 400, "max_minutes": 45},
    }


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_plan(path: Path = PLAN_PATH) -> Path:
    path.write_text(json.dumps(build_plan(), indent=1) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(dump_plan())
