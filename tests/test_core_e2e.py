"""End-to-end Phase 0 flow on a real KiCad 10 through the server (no MCP transport):

    kw_project create -> kw_sch_check -> kw_pcb_drc -> kw_pcb_render -> kw_sch_render -> kw_fab_export

Skips unless KiCad 10.0 is installed and the installation/capability/sandbox/backends components
are implemented. Asserts the user's real KiCad config is untouched (docs/ARCHITECTURE.md §11,
conformance T6) via `kiwrap.sandbox.user_config_hash`.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.core.server import build_server
from kiwrap.core.state.receipts import ReceiptOut

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.0"), pytest.mark.slow]


def _implemented() -> str | None:
    try:
        from kiwrap import sandbox
        from kiwrap.backends.cli import drc, erc, export, netlist, render  # noqa: F401
        from kiwrap.capability import capabilities_for  # noqa: F401
        from kiwrap.installation import discover  # noqa: F401
    except ImportError as exc:
        return f"lower layer missing: {exc}"
    try:
        sandbox.user_config_hash.__code__  # noqa: B018
    except AttributeError:
        return "sandbox.user_config_hash missing"
    return None


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory, install10: Any) -> dict[str, Any]:
    reason = _implemented()
    if reason:
        pytest.skip(reason)
    from kiwrap import sandbox

    home = tmp_path_factory.mktemp("kiwrap-e2e-home")
    server = build_server(home)
    try:
        hash_before = sandbox.user_config_hash(install10)
    except NotImplementedError:
        pytest.skip("sandbox.user_config_hash not implemented")
    return {
        "server": server,
        "work": tmp_path_factory.mktemp("kiwrap-e2e-work"),
        "install": install10,
        "hash_before": hash_before,
    }


def call(server: Any, tool: str, **args: Any) -> tuple[bool, dict[str, Any], Any]:
    r = anyio.run(server.call_tool, tool, args)
    if not r.is_error:
        ReceiptOut.model_validate(r.structured_content)
    return r.is_error, r.structured_content, r


def test_full_flow_on_kicad_10(e2e: dict[str, Any]):
    server, work, install = e2e["server"], e2e["work"], e2e["install"]
    err, sc, _ = call(
        server,
        "kw_project",
        action="create",
        dir=str(work / "flow"),
        name="flow",
        target_kicad=install.version,
        board_size_mm=[40, 25],
    )
    assert not err, sc
    proj = sc["data"]["dir"]
    assert sc["data"]["target_kicad"] == install.version and sc["stage"] == "created" and sc["snapshot"]

    err, sc, _ = call(server, "kw_sch_check", project=proj)
    assert not err, sc
    assert sc["ok"] and sc["data"]["errors"] == 0 and sc["stage"] == "erc_clean"
    assert sc["provider_used"] == f"cli:{install.version}" and sc["data"]["netlist"]["components"] == 0

    err, sc, _ = call(server, "kw_pcb_drc", project=proj)
    assert not err, sc
    assert sc["ok"] and sc["data"]["must_fix"] == 0 and sc["stage"] == "drc_clean"

    err, sc, r = call(server, "kw_pcb_render", project=proj)
    assert not err, sc
    img = [c for c in r.content if type(c).__name__ == "ImageContent"]
    assert img and base64.b64decode(img[0].data)[:8] == b"\x89PNG\r\n\x1a\n"
    assert (
        sc["image_info"]["width"] <= 1024
        and sc["image_info"]["height"] <= 1024
        and sc["image_info"]["bytes"] < 100_000
    )

    err, sc, r = call(server, "kw_sch_render", project=proj)
    assert not err, sc
    if "render_unavailable" not in sc["degraded"]:
        assert sc["image_info"]["width"] <= 1024 and any(
            type(c).__name__ == "ImageContent" for c in r.content
        )

    err, sc, _ = call(server, "kw_fab_export", project=proj, targets=["gerbers", "drill", "pos", "bom"])
    assert not err, sc
    assert (
        sc["ok"]
        and sc["stage"] == "exported"
        and set(sc["data"]["files"]) == {"gerbers", "drill", "pos", "bom"}
    )
    assert all(Path(f).exists() for fs in sc["data"]["files"].values() for f in fs)
    assert any(f.endswith(".gm1") or "Edge" in f for f in sc["data"]["files"]["gerbers"])

    err, sc, _ = call(server, "kw_snapshot", project=proj, action="list")
    assert not err and [s["message"].split()[1] for s in sc["data"]["snapshots"]] == [
        "kw_fab_export",
        "kw_pcb_drc",
        "kw_sch_check",
        "kw_project",
    ]
    assert not list(Path(proj).glob("*.kicad_prl")), "kicad-cli's .kicad_prl side file must be cleaned up"


def test_external_edit_downgrades_on_real_files(e2e: dict[str, Any]):
    server, work = e2e["server"], e2e["work"]
    proj = str(work / "flow")
    sch = Path(proj) / "flow.kicad_sch"
    sch.write_text(sch.read_text(encoding="utf-8").replace('(paper "A4")', '(paper "A3")'), encoding="utf-8")
    err, sc, _ = call(server, "kw_project", action="status", project=proj)
    assert not err and "external_edit" in sc["degraded"] and sc["stage"] == "sch_written"
    err, sc, _ = call(server, "kw_fab_export", project=proj, targets=["gerbers"])
    assert err and sc["code"] == "E_GATE"
    err, sc, _ = call(server, "kw_sch_check", project=proj)
    assert not err and sc["stage"] == "erc_clean"


def test_user_kicad_config_untouched(e2e: dict[str, Any]):
    from kiwrap import sandbox

    assert sandbox.user_config_hash(e2e["install"]) == e2e["hash_before"]
