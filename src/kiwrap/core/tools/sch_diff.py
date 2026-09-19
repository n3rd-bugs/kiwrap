"""kw_sch_diff: a structured diff of two schematic files, computed by KiCad.

Backend: `kicad-cli sch diff --format json FILE1 FILE2` (planner operation `diff`, capability
`cli.has_sch_diff` / subcommand "sch diff"). Verified on the installed nightly against the
ecc83 demo (scratchpad datatools/probe_cli.py, 2026-09-19): exit code 0 = identical, 5 =
differences found (non-zero otherwise, 3 = a file failed to load); JSON on stdout or `-o`:

    {"docType": "kicad_sch", "path": "<FILE2>",
     "changes": [{"kind": "added|removed|modified", "typeName": "SCH_SYMBOL|SCH_LINE|SCH_JUNCTION|...",
                  "id": "/<sheet uuid>/<item uuid>", "refdes": "R1" (symbols only),
                  "bbox": [x, y, w, h] (eeschema internal units: 1 IU = 0.1 um, so 10000 per mm;
                  R1 sits at 157.48 mm in the file and its "Position X" reads 1574800),
                  "properties": [{"name": "Position X", "before": {"type": "int", "v": 1574800},
                                  "after": {"type": "int", "v": 1600200}}, ...],
                  "children": []}]}

Property `type` is one of none | bool | int | string | enum (enum carries `label`). A removed
item lists every property with `after: {type: none}`; a modified one only the changed
properties. Files with regenerated UUIDs diff as removed + added pairs (the ecc83 v1/v2 demo).

This tool converts `bbox` to `bbox_mm` `[x1, y1, x2, y2]` and adds `before_mm`/`after_mm` next
to integer position/size/width/length/offset properties (IU / 10000), keeps everything else
verbatim, and
chunks the changes. On a KiCad without `sch diff` it is E_UNSUPPORTED with the fallback
"kw_sch_get on both + compare".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec
from kiwrap.core.tools._data import DEFAULT_LIMIT, MAX_LIMIT, chunk_by_budget, count_by, pick, rec, ref_key

ChangeKind = Literal["added", "removed", "modified"]
#: eeschema internal units per millimetre (schIUScale; verified against the ecc83 demo, see above)
SCH_IU_PER_MM = 10_000
_MM_PROPERTY = re.compile(r"position|size|width|height|length|offset|thickness|radius|diameter", re.I)


def iu_to_mm(v: Any) -> float | None:
    try:
        return round(int(v) / SCH_IU_PER_MM, 4)
    except (TypeError, ValueError):
        return None


def norm_property(p: dict[str, Any]) -> dict[str, Any]:
    out = {"name": p.get("name"), "before": p.get("before"), "after": p.get("after")}
    if _MM_PROPERTY.search(str(p.get("name") or "")):
        for side in ("before", "after"):
            val = p.get(side) or {}
            if isinstance(val, dict) and val.get("type") == "int":
                out[f"{side}_mm"] = iu_to_mm(val.get("v"))
    return out


def norm_change(c: dict[str, Any]) -> dict[str, Any]:
    box = c.get("bbox")
    bbox_mm = None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        x, y, w, h = (v / SCH_IU_PER_MM for v in box)
        bbox_mm = [round(x, 4), round(y, 4), round(x + w, 4), round(y + h, 4)]
    ident = str(c.get("id") or "")
    parts = [p for p in ident.split("/") if p]
    props = [norm_property(p) for p in (c.get("properties") or []) if isinstance(p, dict)]
    props.sort(key=lambda p: str(p["name"]))
    return {
        "kind": c.get("kind"),
        "type": c.get("typeName"),
        "id": ident,
        "sheet_uuid": parts[0] if len(parts) > 1 else None,
        "uuid": parts[-1] if parts else None,
        "ref": c.get("refdes") or None,
        "bbox_mm": bbox_mm,
        "properties": props,
        "changed_properties": [p["name"] for p in props] if c.get("kind") == "modified" else [],
        "children": len(c.get("children") or []),
    }


KIND_ORDER = {"modified": 0, "added": 1, "removed": 2}


def _value(v: Any) -> Any:
    """A `DiffValue` dataclass (type, value, label) back to the CLI's `{type, v, label?}` shape."""
    if v is None or isinstance(v, dict):
        return v
    r = rec(v)
    out = {"type": pick(r, "type", default="none")}
    if "value" in r and r["value"] is not None:
        out["v"] = r["value"]
    if r.get("label") is not None:
        out["label"] = r["label"]
    return out


def norm_change_obj(c: Any) -> dict[str, Any]:
    """An `ItemChange` from `kiwrap.formats.reports.schdiff` (already parsed, mm computed)."""
    r = rec(c)
    box = pick(r, "bbox")
    bbox_mm = None
    if box is not None:
        br = rec(box)
        x, y, w, h = (float(pick(br, k, default=0.0)) for k in ("x_mm", "y_mm", "w_mm", "h_mm"))
        bbox_mm = [round(x, 4), round(y, 4), round(x + w, 4), round(y + h, 4)]
    ident = str(pick(r, "id", default=""))
    parts = [p for p in ident.split("/") if p]
    props = []
    for pd in pick(r, "properties", default=()) or ():
        pr = rec(pd)
        item = {"name": pr.get("name"), "before": _value(pr.get("before")), "after": _value(pr.get("after"))}
        for side in ("before_mm", "after_mm"):
            if pr.get(side) is not None:
                item[side] = round(float(pr[side]), 4)
        props.append(item)
    props.sort(key=lambda p: str(p["name"]))
    kind = pick(r, "kind")
    return {
        "kind": kind,
        "type": pick(r, "type_name", "typeName"),
        "id": ident,
        "sheet_uuid": parts[0] if len(parts) > 1 else None,
        "uuid": parts[-1] if parts else None,
        "ref": pick(r, "refdes") or None,
        "bbox_mm": bbox_mm,
        "properties": props,
        "changed_properties": [p["name"] for p in props] if kind == "modified" else [],
        "children": len(pick(r, "children", default=()) or ()),
    }


def normalise(raw: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Raw CLI JSON (`{docType, path, changes[]}`) or the parsed `SchDiff` dataclass."""
    if not isinstance(raw, dict):
        r = rec(raw)
        changes = [norm_change_obj(c) for c in (pick(r, "changes", default=()) or ())]
        changes.sort(
            key=lambda c: (KIND_ORDER.get(str(c["kind"]), 9), str(c["type"]), ref_key(c["ref"]), c["id"])
        )
        return changes, {"doc_type": pick(r, "doc_type", "docType"), "path": pick(r, "comparison", "path")}
    changes = [norm_change(c) for c in (raw.get("changes") or []) if isinstance(c, dict)]
    changes.sort(
        key=lambda c: (KIND_ORDER.get(str(c["kind"]), 9), str(c["type"]), ref_key(c["ref"]), c["id"])
    )
    meta = {"doc_type": raw.get("docType"), "path": raw.get("path")}
    return changes, meta


class SchDiffArgs(BaseModel):
    a: str = Field(description="reference .kicad_sch file (the 'before')")
    b: str = Field(description="comparison .kicad_sch file (the 'after')")
    target_kicad: str | None = Field(
        default=None, description="which installed KiCad runs the diff (default: the newest that can)"
    )
    kinds: list[ChangeKind] | None = Field(default=None, description="only these change kinds")
    types: list[str] | None = Field(
        default=None, description="only these item types (SCH_SYMBOL, SCH_LINE, ...)"
    )
    refs: list[str] | None = Field(default=None, description="only symbol changes with these refs")
    cursor: str | None = Field(default=None, description="continue a chunked listing")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="changes per page")


class SchDiffData(BaseModel):
    ok: bool = True
    identical: bool
    a: str
    b: str
    doc_type: str | None = None
    counts: dict[str, int] = Field(description="changes per kind (added/removed/modified) after filters")
    by_type: dict[str, int]
    total: int
    returned: int
    next_cursor: str | None = None
    changes: list[dict[str, Any]]
    plan: dict[str, Any]


class SchDiffOut(ReceiptOut):
    data: SchDiffData  # type: ignore[assignment]


def _resolve(path: str, label: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"{label}: {p} is not a file",
            fix="pass the path of an existing .kicad_sch",
        )
    if p.suffix != ".kicad_sch":
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{label}: {p.name} is not a .kicad_sch file",
            fix="kw_sch_diff compares schematic files",
        )
    return p.resolve()


def _pick_install(ctx: ToolContext, spec: str | None) -> tuple[Any, Any]:
    """`target_kicad` when given; else the newest install whose record can diff (falls back to
    the newest so the planner produces the teaching error with candidates)."""
    if spec:
        install = ctx.services.install(spec)
        return install, ctx.services.caps(install)
    first: tuple[Any, Any] | None = None
    for inst in ctx.services.installs():
        caps = ctx.services.caps(inst)
        first = first or (inst, caps)
        if planner.plan("diff", caps).candidates:
            return inst, caps
    if first is None:
        install = ctx.services.install(None)
        return install, ctx.services.caps(install)
    return first


def sch_diff(ctx: ToolContext, args: SchDiffArgs) -> Receipt:
    a, b = _resolve(args.a, "a"), _resolve(args.b, "b")
    install, caps = _pick_install(ctx, args.target_kicad)
    config = ctx.services.config_root(install)
    outcome = planner.run(
        "diff", caps, {planner.CLI: lambda: ctx.services.sch_diff(install, config, a, b)}, install=install
    )
    raw = outcome.value
    if raw is None or isinstance(raw, (str, bytes, list)):
        raise KiwrapError(
            "E_INTERNAL", "the diff backend returned no diff object", data={"type": type(raw).__name__}
        )
    changes, meta = normalise(raw)
    all_changes = raw.get("changes") if isinstance(raw, dict) else pick(rec(raw), "changes")
    if args.kinds:
        changes = [c for c in changes if c["kind"] in args.kinds]
    if args.types:
        changes = [c for c in changes if c["type"] in args.types]
    if args.refs:
        changes = [c for c in changes if c["ref"] in args.refs]
    paged = chunk_by_budget(changes, args.cursor, args.limit)
    page, nxt = paged.items, paged.next_cursor
    data = {
        "ok": True,
        "identical": not all_changes,
        "a": a.as_posix(),
        "b": b.as_posix(),
        "doc_type": meta["doc_type"],
        "counts": count_by(changes, "kind"),
        "by_type": count_by(changes, "type"),
        "total": len(changes),
        "returned": len(page),
        "next_cursor": nxt,
        "changes": page,
        "plan": outcome.plan.to_dict(),
    }
    return Receipt(
        provider_used=outcome.provider_used(install),
        degraded=outcome.degraded,
        warnings=outcome.warnings,
        data=data,
    )


SPEC = ToolSpec(
    name="kw_sch_diff",
    title="Schematic diff",
    description=(
        "Structured diff of two .kicad_sch files computed by KiCad (kicad-cli sch diff --format json): "
        "added/removed/modified items with type, ref, bbox_mm and changed properties (positions also in mm). "
        "Needs a KiCad with `sch diff`; elsewhere E_UNSUPPORTED with the fallback kw_sch_get on both + compare."
    ),
    input_model=SchDiffArgs,
    output_model=SchDiffOut,
    handler=sch_diff,
    annotations=READ_ONLY,
)
