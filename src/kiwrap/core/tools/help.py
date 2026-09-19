"""kw_help: markdown pages for the stage protocol, every tool and every error code.

Every `KiwrapError.code` has a page here (docs/ARCHITECTURE.md §11: "every code has a kw_help
page"); `test_core_help.py` asserts that against the code list in `kiwrap.core.state`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kiwrap.core.state import STAGES, Receipt
from kiwrap.core.state.gates import GATES
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec

ERROR_PAGES: dict[str, str] = {
    "E_NO_KICAD": (
        "No installed KiCad matches the request. `kw_env` lists what was discovered (registry, "
        "`C:/Program Files/KiCad/*`, PATH, distro packages, Flatpak). Fix: install KiCad (the newest "
        "release is recommended) or pass `target_kicad` as a major (`'N'`), major.minor (`'N.M'`), a full "
        "semver, or the root/kicad-cli path of an install `kw_env` lists (a path never selects anything "
        "discovery did not find)."
    ),
    "E_UNSUPPORTED": (
        "The selected KiCad (or this kiwrap build) lacks the capability. The error's `data` names "
        "`oldest_kicad_with_it`; `candidates` lists the installed KiCads that have it, and `fix` is the "
        "`kw_project(action='create', target_kicad=...)` call that retargets. Examples: ERC/DRC via "
        "kicad-cli need `cli.has_erc`/`cli.has_drc`; 3D renders need `cli.has_render` (the tool then "
        "returns the 2d layer plot itself); `kw_project upgrade` is a Phase 1 feature."
    ),
    "E_PROJECT_NOT_FOUND": (
        "`project` must be an existing directory holding exactly one `.kicad_pro`, or the `.kicad_pro` path "
        "itself. Create one with `kw_project(action='create', dir=..., name=...)`."
    ),
    "E_BAD_ARGS": (
        "The arguments do not satisfy the tool's input schema or its semantic checks (missing `ref`, "
        "invalid name, `force` without a reason, ...). The message names the field; `example` shows a "
        "valid call."
    ),
    "E_KICAD_FAILED": (
        "kicad-cli exited with an error. `data.stderr`/`data.returncode` carry the details. Exit codes "
        "(verified): 0 ok, 1 bad arguments, 2 unknown, 3 invalid input file (also: file written by a newer "
        "KiCad), 4 output conflict, 5 ERC/DRC violations with --exit-code-violations (handled, not an error), "
        "6 jobset failed; 7/8 report a newer file as 127 / -1. Check `kw_project(action='status')`: the "
        "`epochs` must not exceed what `target_kicad` writes."
    ),
    "E_FILE_LOCKED": (
        "Either KiCad holds a `~<file>.lck` next to a design file (close it in KiCad, or delete the stale "
        "lock if KiCad is not running) or another kiwrap call holds `<project>/.kiwrap/lock` (wait for it; "
        "delete the file only if no kiwrap process is running)."
    ),
    "E_GATE": (
        "A stage gate is not satisfied: "
        + "; ".join(f"`{t}` needs `{s}`" for t, s in GATES.items())
        + ". `ok` means zero *errors* (warnings never gate). Reach the stage with the validator "
        "(`kw_sch_check`, `kw_pcb_drc`) or override with `force={'reason': '...'}` - the reason is written "
        "into the snapshot commit message."
    ),
    "E_EXTERNAL_EDIT": (
        "A `.kicad_*` file changed outside kiwrap since the last validated stage. Tools normally just "
        "downgrade the stage and add `external_edit` to `degraded`; this code is raised only when a tool "
        "cannot proceed on the edited file. Re-run the validators (`kw_sch_check`, `kw_pcb_drc`)."
    ),
    "E_TIMEOUT": (
        "A KiCad process exceeded its time budget. Retry with `run_async=true` where offered and poll "
        "`kw_job`, or reduce the input (fewer targets, smaller render)."
    ),
    "E_INTERNAL": (
        "An unexpected exception inside kiwrap (git failure, unreadable state file, bug). `data.traceback` "
        "is included; report it. `kw_snapshot(action='restore')` undoes any half-applied change."
    ),
}

TOOL_PAGES: dict[str, str] = {
    "kw_env": "Installed KiCads + capabilities + engines + storage paths. `refresh=true` re-probes.",
    "kw_project": (
        "`create(dir, name, target_kicad?, board_size_mm?)` writes a minimal valid project in the target's "
        "format and starts snapshots; `status(project)` returns stage/hashes/epochs/external edits; "
        "`upgrade` is Phase 1."
    ),
    "kw_snapshot": "`list` / `diff(ref, to?)` / `create(message?)` / `restore(ref)` over the project's git history.",
    "kw_job": "`status` / `wait(wait_s)` / `cancel` / `list` for background jobs.",
    "kw_help": "This page. Topics: `overview`, `stages`, `errors`, a tool name or an error code.",
    "kw_sch_check": (
        "kicad-cli ERC (positions corrected to mm) + netlist export. Returns ok (zero errors), errors, "
        "warnings, violations with uuid/pos/sheet, and a netlist summary. Passing sets stage `erc_clean`."
    ),
    "kw_pcb_drc": (
        "kicad-cli DRC with optional `schematic_parity` and `refill_zones`. Violations are classified "
        "must_fix / review / cosmetic (must_fix gates fabrication). Passing sets stage `drc_clean`."
    ),
    "kw_sch_render": (
        "Schematic sheet as PNG (<= `max_px`, default 1024) via kicad-cli `sch export pdf` + pypdfium2. "
        "Args: `sheet` (file name; default the root schematic), `page` (1-based; hierarchical designs export one "
        "page per sheet - an out-of-range page is E_BAD_ARGS with the valid range), `max_px`. Without the "
        "`render` extra the receipt carries the PDF path and `degraded: ['render_unavailable']`."
    ),
    "kw_pcb_render": (
        "Board as PNG (<= `max_px`, default 1024). `view='3d'` (default) uses kicad-cli `pcb render` where the "
        "install has `cli.has_render` (sizes 1024x768 down to 480x360, or the request's own 4:3 size below "
        "that, until the PNG is under ~100 KB); otherwise, or with `view='2d'`, the 2d layer plot "
        "(`pcb export pdf` of `layers` + pypdfium2; default layers copper + silk + Edge.Cuts of `side`). "
        "Args: `side` (top|bottom), `view`, `layers` (2d only; isolates layers - there is no region crop in "
        "Phase 0), `max_px`. Without a rasteriser the receipt carries the SVG files and `degraded`."
    ),
    "kw_fab_export": (
        "Fabrication outputs: `targets` from gerbers, drill, pos, pdf, step, bom (plus svg/dxf/stats/ipc2581/odb "
        "where the KiCad supports them). Gated on `drc_clean` unless `force`. Returns the file list."
    ),
}


def overview() -> str:
    return (
        "# kiwrap - agent protocol\n\n"
        "1. `kw_env` -> pick a KiCad. 2. `kw_project create` -> minimal project + snapshots. "
        "3. `kw_sch_check` after every schematic change (ok = 0 errors). 4. `kw_pcb_drc` after every board "
        "change (must_fix = 0). 5. `kw_fab_export` (gated on drc_clean).\n\n"
        "Rules: never edit `.kicad_*` files by hand (kiwrap re-hashes and downgrades the stage with "
        "`external_edit`); positions are millimetres; every mutating tool returns a Receipt "
        "(changed/validators/provider_used/degraded/snapshot/stage/next_suggested); errors are "
        "`isError` with `{code, message, why, fix, candidates, example}`; `kw_snapshot restore` is undo.\n\n"
        "Stages: " + " -> ".join(STAGES) + "\n\n"
        "Gates: " + "; ".join(f"{t} needs {s}" for t, s in GATES.items()) + "\n\n"
        "Topics: " + ", ".join(["overview", "stages", "errors", *TOOL_PAGES, *ERROR_PAGES])
    )


GENERAL_TOPICS = ("overview", "stages", "errors")


def normalise_topic(topic: str) -> str | None:
    """The canonical page key for `topic` (whitespace and case are forgiven: ' E_GATE ', 'e_gate',
    'KW_ENV'), or None when no page matches."""
    key = topic.strip()
    if key in ("", "help"):
        return "overview"
    by_lower = {t.lower(): t for t in [*GENERAL_TOPICS, *ERROR_PAGES, *TOOL_PAGES]}
    return by_lower.get(key.lower())


def page(topic: str) -> tuple[str, list[str]]:
    key = normalise_topic(topic)
    if key == "overview":
        return overview(), []
    if key == "stages":
        return "# Stages\n\n" + " -> ".join(STAGES) + "\n\nGates:\n" + "\n".join(
            f"- {t}: {s}" for t, s in GATES.items()
        ), []
    if key == "errors":
        return "# Error codes\n\n" + "\n".join(f"## {c}\n{t}\n" for c, t in ERROR_PAGES.items()), []
    if key in ERROR_PAGES:
        return f"# {key}\n\n{ERROR_PAGES[key]}", []
    if key in TOOL_PAGES:
        return f"# {key}\n\n{TOOL_PAGES[key]}", []
    lowered = topic.strip().lower()
    cands = [t for t in [*TOOL_PAGES, *ERROR_PAGES, *GENERAL_TOPICS] if lowered and lowered in t.lower()]
    return overview(), cands


class HelpArgs(BaseModel):
    topic: str = Field(
        default="overview",
        description="overview | stages | errors | a tool name (kw_...) | an error code (E_...)",
    )


def run(ctx: ToolContext, args: HelpArgs) -> Receipt:
    text, cands = page(args.topic)
    key = normalise_topic(args.topic)  # the same normalisation page() uses, so the warning never lies
    warnings = [] if key is not None else [f"unknown topic {args.topic!r}; showing overview"]
    return Receipt(
        provider_used="docs",
        data={"ok": True, "topic": key or args.topic, "markdown": text, "candidates": cands},
        warnings=warnings,
    )


SPEC = ToolSpec(
    name="kw_help",
    title="Help",
    description="Markdown help: the stage protocol (overview), stages, every error code (E_...) and every tool (kw_...).",
    input_model=HelpArgs,
    handler=run,
    annotations=READ_ONLY,
)
