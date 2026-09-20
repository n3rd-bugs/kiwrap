# Specctra codec fixtures

`blinker_10/` — the Phase 1b LDO + 555 blinker board (`tests/test_e2e_authoring.py`) as the agent
flow left it on KiCad 10.0.6: 11 footprints (SMD + THT), 8 nets, 24 routed segments, a GND pour on
both layers, netclass patterns in the `.kicad_pro` (whose `net_settings.meta.version` is 0, so
KiCad drops the patterns on load — see `kiwrap.formats.specctra.rules`). Used by
`tests/test_specctra_*.py` offline and by `conformance/tests/t7_specctra_codec.py` as the board
that is stripped, routed by Freerouting, applied through the codec and judged by kicad-cli DRC.

KiCad's own demo boards (ecc83, kit-dev-coldfire-xilinx_5213, openair-max) are copied from
`<install>/share/kicad/demos` at test time and are not committed (conformance corpus rule).
