# Reference board eval — ESP32-S3 dev board (D5)

The MVP acceptance of kiwrap (docs/DECISIONS.md D5): an agent builds this board from the spec,
three consecutive runs, no human edits, with ERC 0 / empty intent diff / 0 unrouted / 0 must-fix
DRC / a complete fabrication bundle. Everything here is graded by KiCad, never by the model.

```
evals/reference_board/
  spec.md            the natural-language brief an agent receives (parts, structure, board, acceptance)
  plan.py            the design plan as data: parts + lib ids, schematic coordinates, wires, power,
                     no-connects, hierarchical labels, the intent (every net's pins), board rules,
                     placement, routing fallback   ->  `uv run python -m evals.reference_board.plan`
  design_plan.json   the generated plan (committed; a fast test keeps it in sync with plan.py)
  grader.py          the rubric: grade(facts) pure, collect_facts(project) via an in-process server,
                     `python -m evals.reference_board.grader <project>` CLI
  runner_claude.py   MANUAL runner: `claude -p` with an isolated CLAUDE_CONFIG_DIR over .mcp.json, graded
```

The scripted conversation that follows the plan is `tests/test_e2e_reference_board.py` (MCP stdio,
one scenario per primary KiCad: the 10.99 nightly with ipc11 writes, 10.0.6 with file writes).
The routing step is pluggable: with `kw_pcb_route_auto` on the server and the managed engines
installed (`scripts/bootstrap.ps1`; the test hands the real `%LOCALAPPDATA%/kiwrap` engines to the
stdio server through `KIWRAP_JAVA` / `KIWRAP_FREEROUTING_JAR`, since the session's storage home is
a throw-away directory) the board is autorouted and the full D5 acceptance is asserted; without
them the power nets are routed with `kw_pcb_route` primitives from the plan's `fallback_ops`, the
other stages are graded, and the test skips with the number of expected-unrouted signal connections
(`KIWRAP_E2E_NO_ROUTER=1` forces that path on a machine that has the engines).

Pour order matters for Freerouting (learned on 10.0.6): the B.Cu GND plane goes in BEFORE routing
(the codec exports it as a Specctra plane; the router stitches every top-side GND pad to it with a
via and routes nothing for GND), the F.Cu pour AFTER. A both-sides pour first makes the router treat
every F.Cu GND pad as already connected (the plane outline covers them) and KiCad's real fill then
leaves them on islands; no pour first makes the router draw 28 GND connections as tracks and run
out of room. With the plan's order the router finished in 5 passes with 0 unrouted and DRC 0 while
KiCad ignored the project's `Power` netclass (a Phase 1b `.kicad_pro` bug kiwrap fixed on
2026-09-20); with the 0.4 mm power tracks the netclass now gives, Freerouting 2.4.1 stops at a
score plateau after ~18 passes with ONE signal connection open (/SDA or /EN, run-dependent), and
`kw_pcb_fix` closes it: the violating net alone re-routed first, then - when that stalls because
the header pad is boxed in by its neighbours' fixed copper - the unlocked copper within 2 mm of the
violation ripped up and re-routed with it (the `scope` of every round is in the receipt). The
integrated runs of 2026-09-20: 10.0.6 route 70 -> 1 unrouted, fix 1 round -> 0; 10.99 route
76 -> 1, fix 2 rounds (nets, neighbourhood) -> 0; both graded passed with 18 JLCPCB files.

## The board

ESP32-S3-WROOM-1, USB-C 2.0 receptacle (5.1 k CC pulldowns, USBLC6 ESD), MCP73831 LiPo charger
(500 mA, CHG LED, JST-PH battery connector), AMS1117-3.3 LDO (power LED), RESET / BOOT buttons,
two I2C headers with 4.7 k pull-ups, a user LED on IO48. 29 parts, three child sheets (`power`,
`mcu`, `io`), 2 layers, 50 x 35 mm, JLCPCB 2-layer rules (`kw_pcb_setup preset="jlcpcb-2layer"`),
GND pour on both sides, `kw_fab_export profile="jlcpcb"`.

Every lib_id / footprint is a stock KiCad library entry, verified present in KiCad 10.0.6 and the
10.99 nightly with `kiwrap.formats.libs.LibResolver` (the libraries are identical in both). Names
that do NOT exist and were replaced: `Connector:Conn_01x02_Male` -> `Connector_Generic:Conn_01x02`;
`Button_Switch_SMD:SW_SPST_PTS645`, `SW_Push_1P1T_NO_6x6mm_H9.5mm` -> `SW_SPST_TL3342`;
`Device:Ferrite_Bead` (dropped). Two design notes the libraries forced: the `USBLC6-2SC6` symbol
does not join its two I/O1 (I/O2) pins, so the lines are not passed through the array (pins 4 / 6
are no-connect); the ESP32-S3-WROOM-1 footprint's thermal vias are 0.2 mm, which the JLCPCB
2-layer preset allows (0.2 mm minimum hole).

## Grading

`grader.grade(Facts)` -> `Grade{passed, failures, erc_errors, intent{missing, extra, misnamed,
unexpected_nets, connected_nc}, unrouted, must_fix, review, parity_violations, fab{present, complete},
counts, degraded, notes}`. The intent diff compares KiCad's netlist (`kw_sch_get kinds=[netlist]`)
with `design_plan.json["intent"]`: named nets (power symbols, labels: `GND`, `+3V3`, `/USB_DP`, ...)
must carry the plan's name and exactly the plan's pins; unlabelled nets are matched as pin groups
regardless of KiCad's `Net-(R3-Pad1)` naming; every open pin must be one the plan marks no-connect.

```
uv run python -m evals.reference_board.grader C:/work/ref-evals/run-1/project      # JSON grade, exit 1 on failure
```

## Running the agent (manual only, D15)

```
uv run python -m evals.reference_board.runner_claude --out C:/work/ref-evals --runs 3 [--target-kicad 10.99] [--claude <path>]
```

Each run: isolated `CLAUDE_CONFIG_DIR`, cwd = repo root (so `.mcp.json` starts `uv run kiwrap serve`),
`--allowedTools mcp__kiwrap,Read`, `--max-turns` from the plan's budget (400), `--output-format json`
for cost / turns, then `grader.collect_facts` on the produced project. `summary.json` reports
`consecutive_green`. Costs real money; nothing schedules it.

## Regenerating the plan

Edit `plan.py`, run `uv run python -m evals.reference_board.plan`, and run
`uv run pytest tests/test_e2e_reference_board.py -k "plan or grader"` (fast, no KiCad). The
schematic coordinates are derived from the library pin offsets with the writer's transform
(docs/VERIFICATION.md claim 3) and the e2e test checks every receipt pin against them, so a wrong
coordinate fails early with the offending pin named.
