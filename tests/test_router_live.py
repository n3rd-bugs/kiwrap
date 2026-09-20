"""Live routing on the installed KiCad 10.0.x with the REAL Freerouting under the managed JRE
(scripts/bootstrap.ps1 installed both under %LOCALAPPDATA%/kiwrap; skipped when absent).

KiCad is the judge (task brief "VALIDATION IS KICAD"): the DSN/SES round trip of these tests is
KiCad's own exporter / importer - `pcbnew.ExportSpecctraDSN(board, path)` / `ImportSpecctraSES(board,
path)` in the install's bundled python (docs/VERIFICATION.md claim 7: headless on 8.0+; the
oracle the codec component is certified against). It is bound as the planner's `swig` provider of
`routing.autoroute` through the test-only driver below (the shipped SWIG driver lives in
`swigdriver/`, another component), so the tools run their whole loop: lock -> export -> Freerouting
-> import -> refill -> kicad-cli DRC. Every KiCad / JVM process is sandboxed (`ConfigRoot.env()`),
started and waited for by the test, and the user's `%APPDATA%/kicad` hash is asserted unchanged.

Boards: the spike DSNs (engine wrapper alone), the ecc83 demo with 50 % of its copper stripped
(2 layers; 4-layer variant through `BoardWriter.setup(layers=4)`), and kw_pcb_fix on the
stripped board (unconnected items are must_fix -> re-route). Marked slow (~1-3 min).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.backends.router import Router, RouteRules, discover_engines
from kiwrap.core.server import build_server
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.state.stages import record_stage
from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[1]
SPIKE_DSN = REPO / "spikes" / "specctra-verify" / "out_10.0.dsn"
MEDIUM_DSN = REPO / "spikes" / "autorouting" / "med10" / "medium.dsn"

# The test-only SWIG driver (claim 7): the bundled python exports / imports; `SaveBoard` writes the
# result. Runs sandboxed and with a watchdog (a malformed outline pops a modal wxLogWarning).
ORACLE = r"""
import sys, pcbnew
op, board_path, path = sys.argv[1], sys.argv[2], sys.argv[3]
b = pcbnew.LoadBoard(board_path)
if op == "export":
    ok = pcbnew.ExportSpecctraDSN(b, path)
else:
    ok = pcbnew.ImportSpecctraSES(b, path)
    if ok:
        pcbnew.SaveBoard(sys.argv[4], b)
print("KIWRAP_ORACLE", "OK" if ok else "FAIL")
sys.exit(0 if ok else 4)
"""


def real_engines() -> Any:
    home = Path(os.environ.get("LOCALAPPDATA", "")) / "kiwrap"
    engines = discover_engines(home)
    if not engines.available:
        pytest.skip("managed JRE / Freerouting not installed (scripts/bootstrap.ps1)")
    return engines


def swig_oracle(install: Any, config: Any, op: str, board: Path, path: Path, out: Path | None = None) -> None:
    python = getattr(install, "python", None)
    if not python or not Path(python).is_file():
        pytest.skip("the install has no bundled python (SWIG)")
    script = Path(board).parent / ".kiwrap" / "tmp" / "oracle.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(ORACLE, encoding="utf-8")
    argv = [str(python), str(script), op, str(board), str(path)] + ([str(out)] if out else [])
    with config.lock():
        env = config.env()
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=300, check=False, cwd=str(script.parent)
        )
        config.restore_seed()
    if "KIWRAP_ORACLE OK" not in proc.stdout:
        raise RuntimeError(
            f"SWIG {op} failed: rc={proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )


def swig_export_dsn(install: Any, config: Any, board: Path, out: Path, caps: Any = None) -> Path:
    swig_oracle(install, config, "export", Path(board), Path(out))
    return Path(out)


def swig_import_ses(install: Any, config: Any, board: Path, ses: Path, out: Path, caps: Any = None) -> Path:
    swig_oracle(install, config, "import", Path(board), Path(ses), Path(out))
    return Path(out)


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory, install10: Any) -> dict[str, Any]:
    from kiwrap import sandbox

    engines = real_engines()
    home = tmp_path_factory.mktemp("kiwrap-route-home")
    server = build_server(home)
    ctx = server.kiwrap_context  # type: ignore[attr-defined]
    ctx.services.router_engines = lambda **kw: engines
    ctx.services.swig_export_dsn = swig_export_dsn
    ctx.services.swig_import_ses = swig_import_ses
    return {
        "server": server,
        "ctx": ctx,
        "work": tmp_path_factory.mktemp("kiwrap-route-work"),
        "install": install10,
        "engines": engines,
        "hash_before": sandbox.user_config_hash(install10),
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


def demo_project(live: dict[str, Any], name: str, *, layers: int | None = None, strip_every: int = 2) -> Path:
    """The ecc83 demo copied as a kiwrap project at stage `placed`, with every `strip_every`-th
    unlocked segment / via deleted (50 % of the copper by default) and, optionally, re-set up as
    a `layers`-layer board."""
    install = live["install"]
    demo = install.share / "demos" / "ecc83"
    if not (demo / "ecc83-pp.kicad_pcb").is_file():
        pytest.skip("ecc83 demo missing")
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
    ok(live["server"], "kw_project", action="status", project=str(d))  # adopts the project (.kiwrap state)
    model = BoardFile.load(d / f"{name}.kicad_pcb")
    w = BoardWriter(d, epoch={"pcb": model.stamp or 20260206}, lib_resolver=None)
    w.load(d / f"{name}.kicad_pcb")
    if layers:
        w.setup(layers=layers)
    doomed = [
        t.uuid for i, t in enumerate([*model.tracks, *model.vias]) if i % strip_every == 0 and not t.locked
    ]
    for uid in doomed:
        w.delete(uid)
    w.save()
    project = Project.from_path(str(d))
    state = project.load_state()
    state.target_kicad = install.version  # status adopted the newest install; this test targets 10.0.x
    record_stage(project, state, "placed")
    project.save_state(state)
    return d


def tracks_of(path: Path) -> tuple[int, int]:
    b = BoardFile.load(path)
    return len(b.tracks) + len(b.arcs), len(b.vias)


# ---- the engine wrapper alone ---------------------------------------------------------------------------------


@pytest.mark.kicad_version("10.0")
def test_engine_routes_the_spike_dsns(tmp_path: Path, live: dict[str, Any]):
    router = Router(live["engines"], temp_dir=tmp_path / "runs")
    assert router.version().startswith("2.4")
    res = router.route(
        SPIKE_DSN, tmp_path / "spike.ses", rules=RouteRules(copper_to_edge_mm=0.25), passes=20, timeout_s=120
    )
    assert res.ok and res.exit_code == 0 and res.state == "COMPLETED" and res.result_json is not None
    assert res.unrouted_at_start == 3 and res.passes >= 1 and (tmp_path / "spike.ses").stat().st_size > 500
    assert "-mt" in res.argv and "--router.fanout.enabled=false" in res.argv
    if MEDIUM_DSN.is_file():
        # the 30-net board of claim 8: fanout off -> everything routes (spike: 46/46 in ~3.5 s)
        res2 = router.route(
            MEDIUM_DSN,
            tmp_path / "medium.ses",
            rules=RouteRules(copper_to_edge_mm=0.5),
            passes=100,
            timeout_s=300,
        )
        assert res2.ok and res2.unrouted == 0, res2.to_dict()
        assert res2.violations == 0 and res2.to_dict()["vias"] is not None


# ---- kw_pcb_route_auto on ecc83 (2 and 4 layers), KiCad's own exporter / importer, kicad-cli DRC ----------------


def route_then_fix(
    live: dict[str, Any], d: Path, *, provider: str, passes: int = 60
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """kw_pcb_route_auto, then kw_pcb_fix when it left must_fix items (the designed loop: Freerouting
    stops at a score plateau with a few connections open when the board carries partial routing;
    the fix rips those nets up and routes them alone). Returns both receipts."""
    sc = ok(
        live["server"],
        "kw_pcb_route_auto",
        project=str(d),
        run_async=False,
        passes=passes,
        timeout_s=300,
        provider=provider,
    )
    data = sc["data"]
    assert data["router"]["ok"] and data["router"]["exit_code"] == 0, data["router"]
    assert data["drc"] is not None and data["drc"]["refilled"] is True, data["drc"]  # the GND pour: claim 8
    fix = None
    if data["drc"]["must_fix"]:
        assert sc["next_suggested"] == "kw_pcb_fix" and data["ok"] is False
        fix = ok(
            live["server"],
            "kw_pcb_fix",
            project=str(d),
            run_async=False,
            passes=passes,
            timeout_s=300,
            provider=provider,
        )
        assert fix["data"]["ok"] and fix["data"]["must_fix"]["after"] == 0, fix["data"]
        assert fix["data"]["iterations"] and fix["stage"] == "drc_clean"
    else:
        assert data["ok"] is True and sc["stage"] == "drc_clean" and sc["next_suggested"] == "kw_fab_export"
    final = ok(live["server"], "kw_pcb_drc", project=str(d))
    assert final["data"]["must_fix"] == 0 and final["data"]["unconnected"] == 0, final["data"]
    return sc, fix


@pytest.mark.kicad_version("10.0")
def test_ecc83_strip_half_and_reroute_with_kicads_own_codec(live: dict[str, Any]):
    """Provider `swig` = KiCad's ExportSpecctraDSN / ImportSpecctraSES (the oracle)."""
    d = demo_project(live, "ecc2")
    board = d / "ecc2.kicad_pcb"
    tracks_before, _ = tracks_of(board)
    assert 20 <= tracks_before <= 40  # ~half of the demo's 59 segments
    sc, _fix = route_then_fix(live, d, provider="swig")
    data = sc["data"]
    assert sc["provider_used"] == f"swig:{live['install'].version}", sc["provider_used"]
    assert (
        data["dsn"]["provider"] == "swig"
        and len(data["dsn"]["layers"]) == 2
        and data["dsn"]["planes"] == ["GND"]
    )
    assert data["rules"]["fanout"] is False and data["rules"]["plane_nets"] == ["GND"]
    assert data["routed"]["cleaned"]["tracks"] > 0  # the stubs of the half-stripped polylines
    assert data["routed"]["tracks_added"] > 0 and data["router"]["passes"] >= 1
    tracks_after, _ = tracks_of(board)
    assert tracks_after > tracks_before
    assert not any(t.locked for t in BoardFile.load(board).tracks)  # nothing left locked behind
    # kw_pcb_fix on a clean board is a no-op
    sc3 = ok(live["server"], "kw_pcb_fix", project=str(d), run_async=False)
    assert sc3["data"]["ok"] and sc3["data"]["iterations"] == [] and sc3["data"]["must_fix"]["after"] == 0


@pytest.mark.kicad_version("10.0")
def test_ecc83_as_a_4_layer_board_with_kiwraps_codec(live: dict[str, Any]):
    """Provider `codec` = kiwrap.formats.specctra (another component) - KiCad's DRC judges the result."""
    d = demo_project(live, "ecc4", layers=4)
    board = d / "ecc4.kicad_pcb"
    assert len(BoardFile.load(board).copper_layers) == 4
    if importlib.util.find_spec("kiwrap.formats.specctra") is None:
        pytest.skip("kiwrap's Specctra codec is not built yet")
    sc, _fix = route_then_fix(live, d, provider="codec")
    data = sc["data"]
    assert (
        sc["provider_used"] == "codec"
        and len(data["dsn"]["layers"]) == 4
        and data["rules"]["copper_layers"] == 4
    )


@pytest.mark.kicad_version("10.0")
def test_kw_pcb_fix_reroutes_unconnected_nets(live: dict[str, Any]):
    d = demo_project(live, "eccfix", strip_every=3)
    board = d / "eccfix.kicad_pcb"
    before = ok(live["server"], "kw_pcb_drc", project=str(d))
    assert before["data"]["unconnected"] > 0  # stripped copper = unconnected_items = must_fix
    sc = ok(live["server"], "kw_pcb_fix", project=str(d), run_async=False, passes=60, timeout_s=300)
    data = sc["data"]
    assert data["must_fix"]["before"] > 0 and data["must_fix"]["after"] == 0, data
    assert len(data["iterations"]) >= 1 and data["iterations"][0]["nets"]
    assert data["ok"] is True and sc["provider_used"] in ("codec", f"swig:{live['install'].version}")
    assert BoardFile.load(board).tracks


@pytest.mark.kicad_version("10.0")
def test_async_route_with_progress_and_cancel(live: dict[str, Any]):
    d = demo_project(live, "eccjob", strip_every=1)  # from scratch: the router finishes with 0 open
    sc = ok(live["server"], "kw_pcb_route_auto", project=str(d), passes=60, timeout_s=300)
    job_id = sc["data"]["job_id"]
    done = ok(live["server"], "kw_job", job_id=job_id, action="wait", wait_s=300)
    assert done["data"]["job"]["state"] == "done", done["data"]["job"]
    result = done["data"]["job"]["result"]
    assert result["data"]["drc"]["must_fix"] == 0 and result["data"]["routed"]["unrouted"] == 0, result[
        "data"
    ]["drc"]
    assert result["data"]["routed"]["tracks_added"] > 20


@pytest.mark.kicad_version("10.0")
def test_net_subset_routes_only_those_nets(live: dict[str, Any]):
    """Phase 1c finding 9: Freerouting 2.4.1 headless ignores `-inc`, so `nets=[...]` used to route
    the whole board. The subset now lives in the DSN (the other nets leave the network): on the
    copper-free ecc83 the session carries exactly the requested nets, the others stay open."""
    d = demo_project(live, "eccsub", strip_every=1)  # 0 tracks, every net open
    board = d / "eccsub.kicad_pcb"
    model = BoardFile.load(board)
    assert not model.tracks and not model.vias
    signal = sorted(n.name for n in model.nets if n.name and n.name != "GND")[:2]
    sc = ok(
        live["server"],
        "kw_pcb_route_auto",
        project=str(d),
        run_async=False,
        nets=signal,
        passes=40,
        timeout_s=300,
        provider="codec",
    )
    data = sc["data"]
    assert data["routed"]["nets"] == signal
    assert sorted(data["ses"]["nets_routed"]) == signal, data["ses"]
    assert sorted(data["router"]["ignored_nets"]) == sorted(
        n.name for n in model.nets if n.name and n.name not in signal
    )
    assert data["router"]["ignore_classes"] == []  # no -inc class: the DSN carries the restriction
    routed = BoardFile.load(board)
    assert {t.net.name for t in routed.tracks if t.net} <= set(signal)
    # KiCad still sees the other nets open (they were never handed to the router)
    assert data["drc"]["unconnected"] > 0 and data["routed"]["unrouted"] > 0
    assert sc["next_suggested"] == "kw_pcb_fix"


@pytest.mark.kicad_version("10.99")
def test_kicad_11_preview_has_no_swig_provider(live: dict[str, Any], installs: Any):
    """On the nightly the `swig` provider is refused by its predicate; the codec (another
    component) answers when built, else the teaching error names kw_pcb_route as the fallback."""
    nightly = next(i for i in installs if i.major_minor == "10.99")
    d = demo_project(live, "ecc11")
    ok(live["server"], "kw_project", action="status", project=str(d))
    project = Project.from_path(str(d))
    state = project.load_state()
    state.target_kicad = nightly.version
    project.save_state(state)
    err, sc = call(
        live["server"], "kw_pcb_route_auto", project=str(d), run_async=False, passes=60, timeout_s=300
    )
    if err:
        assert sc["code"] == "E_UNSUPPORTED" and "kw_pcb_route" in sc["fix"], sc
        assert any(s["provider"] == "swig" for s in sc["data"]["plan"]["skipped"])
        return
    assert sc["provider_used"] == "codec" or sc["provider_used"].startswith("cli:"), sc["provider_used"]
    assert any(s["provider"] == "swig" for s in sc["data"]["plan"]["skipped"])
    if sc["data"]["drc"]["must_fix"]:
        assert sc["next_suggested"] == "kw_pcb_fix"
        fix = ok(live["server"], "kw_pcb_fix", project=str(d), run_async=False, passes=60, timeout_s=300)
        assert fix["data"]["ok"] and fix["data"]["must_fix"]["after"] == 0, fix["data"]
    final = ok(live["server"], "kw_pcb_drc", project=str(d))
    assert final["data"]["must_fix"] == 0 and final["data"]["unconnected"] == 0, final["data"]


@pytest.mark.kicad_version("10.0")
def test_process_hygiene_and_user_config(live: dict[str, Any]):
    from kiwrap import sandbox

    assert sandbox.user_config_hash(live["install"]) == live["hash_before"]
    # no JVM this server started is left running (every route waited for its process tree); JVMs
    # of other kiwrap sessions on this machine are not ours to judge, so match the run directory
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
