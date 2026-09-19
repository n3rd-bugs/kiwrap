"""Integration of the data-first components over the real MCP stdio transport (D16 KiCad-11-first,
D17 data-first): `kiwrap serve` is started as a subprocess with the mcp 2.2 client and driven the
way an agent drives it, against a copy of the ecc83 demo shipped with each installed KiCad.

    kw_env -> kw_project status -> kw_sch_get (symbols with pin nets) -> kw_pcb_get (footprints +
    pads + nets, chunked) -> kw_pcb_check -> kw_sch_stats -> kw_sch_check -> kw_pcb_drc ->
    kw_sch_diff (against a copy with one symbol moved)

Unlike the unit tests in `tests/test_core_datatools.py` (fakes for every backend contract) this
file requires the real backends: `kiwrap.backends.ipc` (headless api-server), `kiwrap.formats`
(board / schematic / analysis) and `kiwrap.backends.cli.structured` (sch diff). It does not skip
when one is missing - a missing component is an integration failure.

Two scenarios (verified 2026-09-19 on this machine):

* the nightly (`ipc.headless`): kw_sch_get / kw_pcb_get answer through `ipc11:<version>` (a
  headless `kicad-cli api-server` per call, closed by the tool), kw_pcb_check / kw_sch_stats
  through the file engine, kw_sch_diff through `cli:<version>`;
* KiCad 10.0 (no headless server, no `sch diff`): the planner skips `ipc11` on the capability
  predicate and answers from the file engine (`provider_used: file`), kw_pcb_check reports
  `ratsnest_estimated` in `degraded[]`, kw_sch_diff is E_UNSUPPORTED naming the fallback.

Process hygiene: every api-server the tools start must be gone when the session ends (checked by
command line, so concurrent kicad-cli runs of other tests are not counted) and the user's real
`%APPDATA%/kicad` hash is unchanged (CLAUDE.md, conformance T6).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.receipts import ReceiptOut

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

DEMO = ("share", "kicad", "demos", "ecc83")
PROJECT_NAME = "ecc83-pp"  # the folder also holds ecc83-pp_v2.*: name the project explicitly


# ---- helpers -------------------------------------------------------------------------------------------


def install_for(installs: list[Any], major_minor: str) -> Any:
    return next(i for i in installs if i.major_minor == major_minor)


def demo_copy(install: Any, dest: Path) -> Path:
    """Copy `<install>/share/kicad/demos/ecc83` to `dest`; returns the .kicad_pro path."""
    src = Path(install.root).joinpath(*DEMO)
    if not src.is_dir():
        pytest.skip(f"no ecc83 demo under {src}")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("*.lck", "*-backups"))
    return dest / f"{PROJECT_NAME}.kicad_pro"


def moved_copy(sch: Path, dest: Path) -> tuple[Path, float, float]:
    """A copy of `sch` with the first `ecc83-pp:R` symbol moved +2.54 mm in X; returns (path, x0, x1)."""
    raw = sch.read_bytes()
    m = re.search(rb'\(symbol\s*\(lib_id "ecc83-pp:R"\)\s*\(at ([\d.]+) ([\d.]+) (\d+)\)', raw)
    assert m, "the ecc83 demo no longer has an ecc83-pp:R symbol"
    x0 = float(m.group(1))
    x1 = round(x0 + 2.54, 2)
    dest.write_bytes(raw[: m.start(1)] + str(x1).encode() + raw[m.end(1) :])
    return dest, x0, x1


def server_params(home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"],
    )


def api_servers_mentioning(path: Path) -> list[str]:
    """Command lines of running `kicad-cli api-server` processes bound to files under `path`."""
    if sys.platform == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='kicad-cli.exe'\" | ForEach-Object { $_.CommandLine }",
        ]
    else:
        cmd = ["ps", "-eo", "args"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    needle = str(path).replace("\\", "/").lower()
    return [
        line
        for line in out.splitlines()
        if "api-server" in line and needle in line.replace("\\", "/").lower()
    ]


class Trace:
    """Every tool call with its receipt (`provider_used`, `degraded`, the data) for the summary."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, tool: str, args: dict[str, Any], result: Any) -> dict[str, Any]:
        sc = result.structured_content
        entry = {
            "tool": tool,
            "args": {k: v for k, v in args.items() if k != "project"},
            "is_error": result.is_error,
            "provider_used": None if result.is_error else sc.get("provider_used"),
            "degraded": [] if result.is_error else sc.get("degraded", []),
            "code": sc.get("code") if result.is_error else None,
            "sc": sc,
        }
        self.calls.append(entry)
        return entry

    def summary(self) -> str:
        rows = [
            f"  {c['tool']:<14} -> {c['code'] if c['is_error'] else c['provider_used']:<16} degraded={c['degraded']}"
            for c in self.calls
        ]
        return "\n".join(rows)


async def drive(
    params: StdioServerParameters, pro: Path, version: str, sch_a: Path, sch_b: Path, trace: Trace
) -> None:
    """The agent's data-first flow over stdio. Every receipt is validated against ReceiptOut."""
    project = str(pro)

    async with Client(params, mode="auto") as c:

        async def call(tool: str, **args: Any) -> dict[str, Any]:
            res = await c.call_tool(tool, args)
            entry = trace.record(tool, args, res)
            if not res.is_error:
                ReceiptOut.model_validate(res.structured_content)
            return entry

        env = await call("kw_env")
        assert not env["is_error"], env["sc"]
        assert any(i["version"] == version for i in env["sc"]["data"]["installs"]), env["sc"]["data"]

        status = await call("kw_project", action="status", project=project)
        assert not status["is_error"], status["sc"]
        assert status["sc"]["data"]["target_kicad"] == version

        # schematic: symbols with absolute pin endpoints and nets, plus the netlist singleton
        sch = await call("kw_sch_get", project=project, kinds=["symbols", "netlist"], limit=500)
        assert not sch["is_error"], sch["sc"]
        symbols = [i for i in sch["sc"]["data"]["items"] if i["kind"] == "symbol"]
        assert len(symbols) >= 10 and all(s["ref"] for s in symbols)
        pins = [p for s in symbols for p in s["pins"]]
        assert pins and all(p["x_mm"] is not None and p["y_mm"] is not None for p in pins)
        assert sum(1 for p in pins if p.get("net")) >= len(pins) // 2, "most pins must carry a net"
        r1 = next(s for s in symbols if s["ref"] == "R1")
        assert r1["x_mm"] == 157.48 and any(p["net"] for p in r1["pins"])
        assert sch["sc"]["data"]["netlist"]["net_count"] > 0

        # board: footprints + pads + nets, walked page by page
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await call(
                "kw_pcb_get",
                project=project,
                kinds=["footprints", "pads", "nets"],
                detail="normal",
                limit=25,
                **({"cursor": cursor} if cursor else {}),
            )
            assert not page["is_error"], page["sc"]
            data = page["sc"]["data"]
            items.extend(data["items"])
            pages += 1
            cursor = data["next_cursor"]
            if not cursor:
                break
            assert pages < 50, "runaway paging"
        assert pages >= 2, "the chunked read must span several pages at limit=25"
        assert len(items) == data["total"]
        kinds = {i["kind"] for i in items}
        assert kinds == {"footprint", "pad", "net"}, kinds
        fps = [i for i in items if i["kind"] == "footprint"]
        assert len(fps) == data["counts"]["footprint"] >= 10
        assert all(i["x_mm"] is not None and i["bbox_mm"] for i in fps)
        c1 = next(f for f in fps if f["ref"] == "C1")
        assert (c1["x_mm"], c1["y_mm"]) == (141.605, 99.695) and c1["layer"] == "F.Cu"
        pads = [i for i in items if i["kind"] == "pad"]
        assert pads and any(p["net"] for p in pads) and all(p["ref"] for p in pads)
        assert any(n["name"] for n in items if n["kind"] == "net")

        # the checks the agent acts on: overlaps / off-board / ratsnest estimate
        chk = await call("kw_pcb_check", project=project)
        assert not chk["is_error"], chk["sc"]
        assert set(chk["sc"]["data"]["summary"]) >= {"courtyard_overlap", "off_board", "unrouted"}
        # no KiCad exports airwires: either KiCad's connectivity clusters (ipc11, end points guessed)
        # or the file estimate answered, and the receipt says which
        assert {"ratsnest_estimated", "airwire_endpoints_estimated"} & set(chk["degraded"]), chk["degraded"]
        assert chk["sc"]["data"]["summary"]["unrouted"] == 0, "the ecc83 demo is fully routed"

        stats = await call("kw_sch_stats", project=project)
        assert not stats["is_error"], stats["sc"]
        sd = stats["sc"]["data"]
        assert sd["components"] >= 10 and sd["nets"] > 0

        erc = await call("kw_sch_check", project=project)
        assert not erc["is_error"], erc["sc"]
        assert erc["sc"]["provider_used"] == f"cli:{version}"
        assert erc["sc"]["data"]["netlist"]["components"] >= 10

        drc = await call("kw_pcb_drc", project=project)
        assert not drc["is_error"], drc["sc"]
        assert drc["sc"]["provider_used"] == f"cli:{version}"
        assert isinstance(drc["sc"]["data"]["must_fix"], int)

        # the diff is the only cli-only operation; on KiCad without `sch diff` it names the fallback
        await call("kw_sch_diff", a=str(sch_a), b=str(sch_b), target_kicad=version)


# ---- scenarios -----------------------------------------------------------------------------------------


def _run_scenario(tmp_path: Path, storage_home: Path, install: Any) -> Trace:
    from kiwrap import sandbox

    before = sandbox.user_config_hash(install)
    work = tmp_path / "ecc83"
    pro = demo_copy(install, work)
    Project.from_path(pro).save_state(ProjectState(target_kicad=install.version))
    sch_b, _, _ = moved_copy(work / f"{PROJECT_NAME}.kicad_sch", tmp_path / "moved.kicad_sch")
    trace = Trace()
    try:
        anyio.run(
            drive,
            server_params(storage_home),
            pro,
            install.version,
            pro.with_suffix(".kicad_sch"),
            sch_b,
            trace,
        )
    finally:
        print(f"\nprovider_used per call on KiCad {install.version}:\n{trace.summary()}")
        (tmp_path / "trace.json").write_text(
            json.dumps([{k: v for k, v in c.items() if k != "sc"} for c in trace.calls], indent=1),
            encoding="utf-8",
        )
    assert api_servers_mentioning(work) == [], "an api-server the tools started is still running"
    assert sandbox.user_config_hash(install) == before, "the user's KiCad config was modified"
    assert not list(work.glob("*.kicad_prl")), "kicad-cli's .kicad_prl side file must be cleaned up"
    return trace


@pytest.mark.kicad_version("10.99")
def test_nightly_data_first_flow_over_stdio(tmp_path: Path, storage_home: Path, installs: list[Any]):
    """On the nightly every read goes through the headless api-server (ipc11) and the diff through
    kicad-cli sch diff; the ratsnest stays an estimate (no KiCad exposes airwires)."""
    install = install_for(installs, "10.99")
    trace = _run_scenario(tmp_path, storage_home, install)
    by_tool = {c["tool"]: c for c in trace.calls}
    ipc = f"ipc11:{install.version}"
    assert by_tool["kw_sch_get"]["provider_used"] == ipc and by_tool["kw_sch_get"]["degraded"] == []
    assert by_tool["kw_pcb_get"]["provider_used"] == ipc and by_tool["kw_pcb_get"]["degraded"] == []
    assert all(c["provider_used"] == ipc for c in trace.calls if c["tool"] == "kw_pcb_get")
    check = by_tool["kw_pcb_check"]
    assert check["sc"]["data"]["providers"]["unrouted"] == ipc, check["sc"]["data"]["providers"]
    assert check["degraded"] == ["airwire_endpoints_estimated"]
    assert by_tool["kw_sch_stats"]["provider_used"] in ("file", ipc)
    diff = by_tool["kw_sch_diff"]
    assert not diff["is_error"], diff["sc"]
    assert diff["provider_used"] == f"cli:{install.version}"
    d = diff["sc"]["data"]
    assert d["identical"] is False and d["counts"] == {"modified": 1} and d["by_type"] == {"SCH_SYMBOL": 1}
    assert d["changes"][0]["changed_properties"] == ["Position X"]


@pytest.mark.kicad_version("10.0")
def test_kicad10_falls_back_to_file_and_cli_over_stdio(
    tmp_path: Path, storage_home: Path, installs: list[Any]
):
    """KiCad 10.0 has no headless server: the planner skips ipc11 on the capability predicate, the
    file engine answers the reads with the skip recorded in the plan, the checks say what is
    estimated, and kw_sch_diff is a teaching E_UNSUPPORTED that names the fallback."""
    install = install_for(installs, "10.0")
    trace = _run_scenario(tmp_path, storage_home, install)
    by_tool = {c["tool"]: c for c in trace.calls}
    for tool in ("kw_sch_get", "kw_pcb_get", "kw_sch_stats"):
        assert by_tool[tool]["provider_used"] == "file", (tool, by_tool[tool]["provider_used"])
    assert by_tool["kw_pcb_check"]["sc"]["data"]["providers"]["unrouted"] == "file"
    skipped = {s["provider"]: s["reason"] for s in by_tool["kw_pcb_get"]["sc"]["data"]["plan"]["skipped"]}
    assert "ipc.headless" in skipped.get("ipc11", ""), skipped
    assert by_tool["kw_pcb_check"]["degraded"] == ["ratsnest_estimated"]
    assert any(c["provider_used"] == f"cli:{install.version}" for c in trace.calls)  # ERC / DRC / netlist
    diff = by_tool["kw_sch_diff"]
    assert diff["is_error"] and diff["code"] == "E_UNSUPPORTED", diff["sc"]
    assert "kw_sch_get" in (diff["sc"].get("fix") or "") or "kw_sch_get" in str(diff["sc"].get("data"))
