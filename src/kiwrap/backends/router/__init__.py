"""Autorouting backend: Freerouting 2.4.1 under kiwrap's managed JRE 25 (docs/ARCHITECTURE.md §8,
docs/VERIFICATION.md claim 8, docs/research/04-autorouting.md).

Modules and their public surface (the contract `kiwrap.core.tools.pcb_route_auto` codes against):

    jre.py          discover_engines(storage_home=None) -> Engines(java, jar, profile, ...)
                    EngineMissing (RuntimeError) when the JRE / jar are absent: the fix is
                    `scripts/bootstrap.ps1` (docs/DECISIONS.md D11: engines are downloaded into the
                    storage home at bootstrap time, never redistributed - they are GPL-3.0, D8)
    rules.py        RouteRules + rules_from_board(board_file, caps) -> RouteRules: the Freerouting
                    settings derived from the board's own rules (`min_track_width`, `min_clearance`,
                    `min_via_*`, `min_copper_edge_clearance` from the .kicad_pro), BGA detection
                    (fanout on only then), plane nets (copper zones) and netclasses made only of
                    plane nets (`-inc`)
    dsn_post.py     the DSN post-processing hooks: a small Specctra s-expression parser (KiCad's
                    `(string_quote ")` defeats the KiCad CST) + `restrict_nets` (removes the nets
                    NOT to be routed from the network and fixes their wiring: 2.4.1 headless
                    ignores `-inc`), and the read-only `dsn_summary`
    freerouting.py  Router(engines).route(dsn, ses_out, *, rules, nets=None, passes=..., timeout_s,
                    progress_cb, cancel_event) -> RouteResult(passes, unrouted, log, duration_s,
                    exit_code, ...): the verified CLI invocation (`--gui.enabled=false`,
                    `--router.fanout.enabled=<rules.fanout>`, `--router.copper_to_edge_clearance_um`,
                    `-mt 1` + `--router.optimizer.max_threads=1`, `-mp N`, `--router.result_json`,
                    a per-run `--user_data_path` seeded with the telemetry-off profile), progress
                    parsing from the log lines, cooperative cancel (process-tree kill) and timeouts

The backend never imports `kiwrap.core` (import-linter layer contract) and never looks at
`sys.platform` (it asks `kiwrap.platform.current()`).
"""

from __future__ import annotations

from kiwrap.backends.router.freerouting import (
    DEFAULT_PASSES,
    DEFAULT_TIMEOUT_S,
    RouteProgress,
    Router,
    RouterError,
    RouteResult,
    RouterTimeout,
)
from kiwrap.backends.router.jre import EngineMissing, Engines, discover_engines
from kiwrap.backends.router.rules import RouteRules, rules_from_board

__all__ = [
    "DEFAULT_PASSES",
    "DEFAULT_TIMEOUT_S",
    "EngineMissing",
    "Engines",
    "RouteProgress",
    "RouteResult",
    "RouteRules",
    "Router",
    "RouterError",
    "RouterTimeout",
    "discover_engines",
    "rules_from_board",
]
