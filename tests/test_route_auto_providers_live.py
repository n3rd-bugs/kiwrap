"""kw_pcb_route_auto / kw_pcb_fix through the REAL DSN/SES providers, KiCad as the judge
(task brief "VALIDATION IS KICAD"; docs/ARCHITECTURE.md §8; docs/VERIFICATION.md claim 7):

- the patched kicad-cli (`pcb export specctra` / `pcb import-ses`, fork patch `specctra-jobs`,
  bench build registered as an explicit install) is the first provider of `routing.autoroute`:
  `provider_used` is `cli:<version>+specctra-jobs`, the receipt carries KiCad's own track counts
  from the import, and kicad-cli DRC after the refill is 0 must_fix / 0 unconnected on the ecc83
  demo with its copper removed (2 layers, and re-set up as 4 layers);
- kiwrap's codec (`kiwrap.formats.specctra`) on the SAME build writes a DSN that is semantically
  equal to the patched CLI's export (0 differences at 5 um; KiCad 11's `protect` / `(wire
  (polygon …))` dialect normalised by `compare_dsn`), routes the same board to 0/0 and the
  receipt says `provider_used: codec` with `cli` listed as the skipped preference;
- the stock nightly (no SWIG, no patch) routes through the codec with `cli` skipped for its
  missing capability and `swig` skipped by its predicate;
- KiCad 10.0.x routes through the codec ranked ahead of the (driverless) `swig` provider.

Every KiCad process runs under a sandbox root (`ConfigRoot.env()`), Freerouting from the managed
JRE with a per-run `--user_data_path` (telemetry-off profile), and the user's `%APPDATA%/kicad`
hash is asserted by the session fixture. The patched-build tests need the bench build in the
session's discovery (`KIWRAP_KICAD_ROOTS=C:\\kiwrap-builds\\6dd893f7d1-5`: pytest's throw-away
`KIWRAP_HOME` has no installs.json, docs/dev/kicad-source-build.md) and auto-skip otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.backends.router import discover_engines
from kiwrap.core.server import build_server
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.state.stages import record_stage
from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.specctra import DsnWriter, compare_dsn, parse_dsn
from tests._fakes import user_config_untouched  # noqa: F401 - session fixture

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

PATCH = "specctra-jobs"
PASSES = 60
TIMEOUT_S = 300


def real_engines() -> Any:
    engines = discover_engines(Path(os.environ.get("LOCALAPPDATA", "")) / "kiwrap", fallback=True)
    if not engines.available:
        pytest.skip("managed JRE / Freerouting not installed (scripts/bootstrap.ps1)")
    return engines


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory, installs: Any) -> dict[str, Any]:
    engines = real_engines()
    home = tmp_path_factory.mktemp("kiwrap-providers-home")
    server = build_server(home)
    ctx = server.kiwrap_context  # type: ignore[attr-defined]
    ctx.services.router_engines = lambda **kw: engines
    return {
        "server": server,
        "ctx": ctx,
        "work": tmp_path_factory.mktemp("kiwrap-providers-work"),
        "installs": installs,
        "engines": engines,
    }


def call(server: Any, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
    r = anyio.run(server.call_tool, tool, args)
    if not r.is_error:
        ReceiptOut.model_validate(r.structured_content)
    return r.is_error, r.structured_content


def ok(server: Any, tool: str, **args: Any) -> dict[str, Any]:
    err, sc = call(server, tool, **args)
    assert not err, sc
    return sc


def demo_source(installs: list[Any]) -> Path:
    for inst in installs:
        demo = inst.share / "demos" / "ecc83"
        if (demo / "ecc83-pp.kicad_pcb").is_file():
            return demo
    pytest.skip("ecc83 demo missing")


def demo_project(
    live: dict[str, Any],
    name: str,
    target: Any,
    *,
    layers: int | None = None,
    keep_nets: set[str] = frozenset(),
) -> Path:
    """The ecc83 demo as a kiwrap project at stage `placed`, targeted at `target` (an install:
    its root path is the `target_kicad`, which distinguishes a bench build from the stock install
    of the same semver), with the copper of every net but `keep_nets` removed (whole nets: a stub
    ending inside an SMD pad is one Freerouting refuses to connect to)."""
    demo = demo_source(live["installs"])
    d = live["work"] / name
    d.mkdir()
    (d / f"{name}.kicad_pro").write_text(
        (demo / "ecc83-pp.kicad_pro").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (d / f"{name}.kicad_pcb").write_text(
        (demo / "ecc83-pp.kicad_pcb").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for extra in ("fp-lib-table", "sym-lib-table"):
        if (demo / extra).is_file():
            (d / extra).write_text((demo / extra).read_text(encoding="utf-8"), encoding="utf-8")
    ok(live["server"], "kw_project", action="status", project=str(d))
    model = BoardFile.load(d / f"{name}.kicad_pcb")
    w = BoardWriter(d, epoch={"pcb": model.stamp or 20260206}, lib_resolver=None)
    w.load(d / f"{name}.kicad_pcb")
    if layers:
        w.setup(layers=layers)
    for item in [*model.tracks, *model.arcs, *model.vias]:
        if not item.locked and (item.net is None or item.net.name not in keep_nets):
            w.delete(item.uuid)
    w.save()
    project = Project.from_path(str(d))
    state = project.load_state()
    state.target_kicad = str(target.root)
    record_stage(project, state, "placed")
    project.save_state(state)
    return d


def route(live: dict[str, Any], d: Path, **args: Any) -> dict[str, Any]:
    sc = ok(
        live["server"],
        "kw_pcb_route_auto",
        project=str(d),
        run_async=False,
        passes=PASSES,
        timeout_s=TIMEOUT_S,
        **args,
    )
    data = sc["data"]
    assert data["router"]["ok"] and data["router"]["exit_code"] == 0, data["router"]
    assert data["drc"] is not None and data["drc"]["refilled"] is True, data["drc"]
    assert data["router"]["history"] and data["router"]["history"][0]["pass"] == 1, data["router"]["history"]
    return sc


def settle(live: dict[str, Any], d: Path, sc: dict[str, Any], **args: Any) -> dict[str, Any]:
    """kw_pcb_fix when the router left must_fix items (the designed closure); the final DRC."""
    if sc["data"]["drc"]["must_fix"]:
        assert sc["next_suggested"] == "kw_pcb_fix"
        fix = ok(
            live["server"],
            "kw_pcb_fix",
            project=str(d),
            run_async=False,
            passes=PASSES,
            timeout_s=TIMEOUT_S,
            **args,
        )
        assert fix["data"]["ok"] and fix["data"]["must_fix"]["after"] == 0, fix["data"]
    final = ok(live["server"], "kw_pcb_drc", project=str(d))
    assert final["data"]["must_fix"] == 0 and final["data"]["unconnected"] == 0, final["data"]
    return final


def stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"stock KiCad {major_minor} not installed")
    return found[0]


# ---- the patched kicad-cli provider ---------------------------------------------------------------------------


@pytest.mark.kicad_patch(PATCH)
def test_patched_cli_provider_routes_ecc83_and_equals_the_codec(live: dict[str, Any], patched_installs: Any):
    patched = patched_installs(PATCH)[0]
    d = demo_project(live, "cli2", patched)
    sc = route(live, d)
    data = sc["data"]
    assert sc["provider_used"] == f"cli:{patched.version}+{PATCH}", sc["provider_used"]
    assert data["dsn"]["provider"] == "cli" and data["plan"]["candidates"][0] == "cli"
    assert data["ses"]["kicad_tracks_before"] == 0 and data["ses"]["kicad_tracks_after"] > 20, data["ses"]
    assert data["ses"]["tracks_added"] > 20 and data["routed"]["tracks_added"] == data["ses"]["tracks_added"]
    assert data["rules"]["fanout"] is False and data["rules"]["plane_nets"] == ["GND"]
    settle(live, d, sc)
    assert not any(t.locked for t in BoardFile.load(d / "cli2.kicad_pcb").tracks)

    # the codec on the same build: its DSN is KiCad's DSN (0 differences at 5 um after the
    # KiCad 11 dialect normalisation) and the route ends 0/0 as well
    d2 = demo_project(live, "codec2", patched)
    board = d2 / "codec2.kicad_pcb"
    ours = DsnWriter(BoardFile.load(board), name="codec2.dsn", host_version=patched.version).write()
    sc2 = route(live, d2, provider="codec")
    assert sc2["provider_used"] == "codec" and sc2["data"]["dsn"]["provider"] == "codec"
    theirs = Path(data["dsn"]["path"]).read_text(encoding="utf-8")
    diff = compare_dsn(parse_dsn(ours), parse_dsn(theirs))
    assert diff.ok, diff.summary() + "\n" + "\n".join(diff.differences[:10])
    assert diff.checked["plane"] == 1 and diff.checked["pins"] > 20 and diff.checked["placements"] == 15
    settle(live, d2, sc2)


@pytest.mark.kicad_patch(PATCH)
def test_patched_cli_provider_on_a_4_layer_board(live: dict[str, Any], patched_installs: Any):
    patched = patched_installs(PATCH)[0]
    d = demo_project(live, "cli4", patched, layers=4)
    sc = route(live, d)
    data = sc["data"]
    assert sc["provider_used"].startswith("cli:") and sc["provider_used"].endswith("+" + PATCH)
    assert len(data["dsn"]["layers"]) == 4 and data["rules"]["copper_layers"] == 4
    settle(live, d, sc)


@pytest.mark.kicad_patch(PATCH)
def test_patched_cli_fix_reroutes_the_unconnected_nets(live: dict[str, Any], patched_installs: Any):
    """kw_pcb_fix alone on a board with two nets missing: routing.fix goes through the patched
    CLI too (the same provider chain), rips nothing up that is routed and closes the gaps."""
    patched = patched_installs(PATCH)[0]
    demo = demo_source(live["installs"])
    nets = sorted({t.net.name for t in BoardFile.load(demo / "ecc83-pp.kicad_pcb").tracks if t.net})
    keep = set(nets) - set(nets[:2])
    d = demo_project(live, "clifix", patched, keep_nets=keep)
    before = ok(live["server"], "kw_pcb_drc", project=str(d))
    assert before["data"]["unconnected"] > 0
    sc = ok(live["server"], "kw_pcb_fix", project=str(d), run_async=False, passes=PASSES, timeout_s=TIMEOUT_S)
    data = sc["data"]
    assert data["ok"] and data["must_fix"]["after"] == 0, data
    assert sc["provider_used"] == f"cli:{patched.version}+{PATCH}"
    assert data["iterations"] and set(data["iterations"][0]["nets"]) <= set(nets[:2]) | {"GND"}, data[
        "iterations"
    ]


# ---- stock installs: the codec ---------------------------------------------------------------------------------


@pytest.mark.kicad_version("10.99")
def test_stock_nightly_routes_through_the_codec(live: dict[str, Any]):
    nightly = stock(live["installs"], "10.99")
    d = demo_project(live, "nightly", nightly)
    sc = route(live, d)
    assert sc["provider_used"] == "codec", sc["provider_used"]
    skipped = {s["provider"]: s["reason"] for s in sc["data"]["plan"]["skipped"]}
    assert "cli" in skipped and "cli.has_specctra_export" in skipped["cli"], skipped
    assert "swig" in skipped and "swig" in skipped["swig"], skipped
    settle(live, d, sc)


@pytest.mark.kicad_version("10.0")
def test_kicad_10_routes_through_the_codec_ahead_of_swig(live: dict[str, Any]):
    ten = stock(live["installs"], "10.0")
    d = demo_project(live, "ten", ten)
    sc = route(live, d)
    assert sc["provider_used"] == "codec", sc["provider_used"]
    plan = sc["data"]["plan"]
    assert plan["candidates"] == ["codec", "swig"], plan  # cli skipped by its predicate, codec answers first
    settle(live, d, sc)


def test_no_jvm_of_this_server_left_running(live: dict[str, Any]):
    import subprocess

    run_root = str(live["ctx"].services.storage_home / "tmp" / "freerouting").replace("\\", "/").lower()
    if os.name == "nt":
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"name='java.exe'\" | ForEach-Object { $_.CommandLine }",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        assert run_root not in out.replace("\\", "/").lower(), out
