# ESP32-S3 development board — design brief

Build a small ESP32-S3 development board as a complete KiCad project ready for JLCPCB: schematic,
board, and fabrication files. Use the kiwrap tools only (never edit `.kicad_*` files by hand);
KiCad's own ERC / DRC are the acceptance judges.

## What the board does

- **MCU:** an `ESP32-S3-WROOM-1` module. USB data goes straight to the module's native USB pins
  (`USB_D+` / `USB_D-`), so there is no USB-serial bridge.
- **USB-C:** a USB 2.0 Type-C receptacle (16 pins) at the bottom edge, used as a sink: 5.1 kΩ from
  each of CC1 and CC2 to GND. Both D+ pads and both D- pads of the receptacle are tied together.
  SBU1 / SBU2 unused. The shield goes to GND. A USB ESD protection array (e.g. USBLC6-2SC6) sits on
  the D+ / D- lines and VBUS.
- **Charger:** an `MCP73831` single-cell LiPo charger powered from VBUS, 500 mA charge current
  (2 kΩ on PROG), 10 µF on VBUS and on the battery output, a charge-status LED (with a 1 kΩ series
  resistor from VBUS) on STAT, and a 2-pin JST-PH battery connector at the left edge. The charger
  output is the battery rail (`+BATT`).
- **Regulator:** an `AMS1117-3.3` from `+BATT` to `+3V3` with 10 µF on input and output, and a
  power LED (1 kΩ) on `+3V3`. Without a battery the charger output feeds the LDO.
- **Module support:** 10 µF + 100 nF decoupling at the module's 3V3 pin; EN pulled up with 10 kΩ
  and delayed with 100 nF to GND; a RESET button from EN to GND; a BOOT button from IO0 to GND
  (IO0 has the module's internal pull-up).
- **I2C:** SDA on IO8, SCL on IO9, 4.7 kΩ pull-ups to `+3V3`, two identical 4-pin 2.54 mm headers
  (3V3, SDA, SCL, GND) at the right edge.
- **User LED** on IO48 through 1 kΩ to GND.
- Every unused module pin and the unused connector pins carry a no-connect flag.

## Schematic structure

Three child sheets under the root: `power` (USB-C, ESD, charger, LDO, LEDs), `mcu` (module,
decoupling, EN circuit, I2C pull-ups) and `io` (buttons, user LED, headers). The sheets exchange
`USB_DP`, `USB_DN`, `EN`, `IO0`, `SDA`, `SCL` and `LED` through hierarchical labels / sheet pins
wired on the root sheet with a local label of the same name on each wire (so the board nets are
`/USB_DP`, `/EN`, ...). Power rails are the global power symbols `VBUS`, `+BATT`, `+3V3`, `GND`
with a `PWR_FLAG` on VBUS and on GND.

## Parts

Stock KiCad symbols and footprints only (all present in KiCad 10.0.6 and the 10.99 nightly):

| ref | symbol | footprint |
|---|---|---|
| J1 | `Connector:USB_C_Receptacle_USB2.0_16P` | `Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12` |
| U1 | `RF_Module:ESP32-S3-WROOM-1` | `RF_Module:ESP32-S3-WROOM-1` |
| U2 | `Power_Protection:USBLC6-2SC6` | `Package_TO_SOT_SMD:SOT-23-6` |
| U3 | `Battery_Management:MCP73831-2-OT` | `Package_TO_SOT_SMD:SOT-23-5` |
| U4 | `Regulator_Linear:AMS1117-3.3` | `Package_TO_SOT_SMD:SOT-223-3_TabPin2` |
| J2 | `Connector_Generic:Conn_01x02` | `Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical` |
| J3, J4 | `Connector_Generic:Conn_01x04` | `Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical` |
| SW1, SW2 | `Switch:SW_Push` | `Button_Switch_SMD:SW_SPST_TL3342` |
| R1–R9 | `Device:R` | `Resistor_SMD:R_0603_1608Metric` |
| C1–C7 | `Device:C` | `Capacitor_SMD:C_0603_1608Metric` |
| D1–D3 | `Device:LED` | `LED_SMD:LED_0603_1608Metric` |

29 parts. Substitutes noted while checking the libraries: `Connector:Conn_01x02_Male` does not
exist (use `Connector_Generic:Conn_01x02`); `Button_Switch_SMD:SW_SPST_PTS645` / `SW_Push_1P1T_NO_6x6mm_H9.5mm`
do not exist (use `SW_SPST_TL3342`); `Device:Ferrite_Bead` does not exist (not needed).

## Board

- 2 layers, 50 × 35 mm rectangle, JLCPCB 2-layer rules (`kw_pcb_setup preset="jlcpcb-2layer"`:
  0.15 mm clearance, 0.25 mm track, 0.3/0.6 mm via, 0.2 mm minimum hole, 0.3 mm copper-to-edge).
  Netclass `Power` (GND, +3V3, +BATT, VBUS) at 0.4 mm.
- The module sits at the top edge with its antenna over the edge (its keep-out courtyard is
  off-board; nothing else goes in that band). The USB-C receptacle is centred on the bottom edge,
  the battery connector on the left edge, the two I2C headers and the buttons on the right.
- Decoupling and pull-ups next to the module pins they serve; charger and LDO on the left.
- Fully routed (autorouter), GND pour on both copper layers, DRC clean with schematic parity.
- Fabrication: JLCPCB profile — gerbers, drill, pick-and-place, BOM.

## Acceptance (graded by KiCad)

ERC 0 errors, intent diff empty (every net has exactly the pins above), 0 unrouted connections,
0 must-fix DRC violations after zone refill, fabrication bundle present.
