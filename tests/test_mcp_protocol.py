"""MCP protocol tests: start `kiwrap serve` over stdio with the mcp 2.2 client (the pattern from
spikes/verify-mcp22/test_client.py, docs/VERIFICATION.md claim 11) and check the catalog, the
schemas, kw_help, kw_env and an error round trip - in both the modern (auto) and legacy modes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

from kiwrap.core.cli_main import CONFORMANCE_TESTS, bearer_guard, build_parser, main
from kiwrap.core.tools import TOOL_NAMES


@pytest.fixture(scope="module")
def mcp_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One storage home for the module so the second session reuses the capability cache."""
    return tmp_path_factory.mktemp("kiwrap-mcp-home")


def server_params(home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"],
    )


async def _session(params: StdioServerParameters, mode: str, tmp_path: Path) -> dict:
    out: dict = {}
    async with Client(params, mode=mode) as c:
        out["protocol"] = c.protocol_version
        listed = await c.list_tools()
        out["tools"] = [t.name for t in listed.tools]
        out["schemas"] = {t.name: (t.input_schema, t.output_schema, t.annotations) for t in listed.tools}
        listed2 = await c.list_tools()
        out["tools_again"] = [t.name for t in listed2.tools]
        r = await c.call_tool("kw_help", {"topic": "E_GATE"})
        out["help"] = (r.is_error, r.structured_content, r.content[0].text)
        r = await c.call_tool("kw_env", {})
        out["env"] = (r.is_error, r.structured_content, r.content[0].text)
        r = await c.call_tool("kw_project", {"action": "status", "project": str(tmp_path / "does-not-exist")})
        out["error"] = (r.is_error, r.structured_content, r.content[0].text)
        r = await c.call_tool(
            "kw_fab_export", {"project": str(tmp_path / "x"), "targets": ["gerbers"], "force": {}}
        )
        out["error2"] = (r.is_error, r.structured_content)
        # schema-level rejections the SDK makes before our code runs, and an unknown tool name
        r = await c.call_tool("kw_project", {})
        out["schema_error"] = (r.is_error, r.structured_content, r.content[0].text)
        r = await c.call_tool("kw_pcb_render", {"project": "x", "view": "iso"})
        out["enum_error"] = (r.is_error, r.structured_content)
        r = await c.call_tool("kw_report", {})
        out["unknown_tool"] = (r.is_error, r.structured_content)
    return out


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_catalog_help_env_and_error_round_trip(tmp_path: Path, mcp_home: Path, mode: str):
    out = anyio.run(_session, server_params(mcp_home), mode, tmp_path)
    assert out["protocol"] in ("2026-07-28", "2025-11-25", "2025-06-18")
    assert out["tools"] == list(TOOL_NAMES) == out["tools_again"]  # deterministic
    for name, (inp, outp, ann) in out["schemas"].items():
        assert inp["type"] == "object", name
        assert outp is not None and "stage" in outp["properties"], name
        assert ann is not None and ann.read_only_hint is not None, name
    assert out["schemas"]["kw_env"][2].read_only_hint is True
    assert out["schemas"]["kw_snapshot"][2].destructive_hint is True

    is_err, sc, text = out["help"]
    assert not is_err and sc["data"]["markdown"].startswith("# E_GATE") and "E_GATE" in text

    is_err, sc, text = out["env"]
    assert not is_err and isinstance(sc["data"]["installs"], list) and "storage" in sc["data"]
    json.loads(text.split("\n", 1)[1])  # the text body after the summary line is JSON

    is_err, sc, text = out["error"]
    assert (
        is_err
        and sc["code"] == "E_PROJECT_NOT_FOUND"
        and sc["fix"]
        and text.startswith("E_PROJECT_NOT_FOUND:")
    )
    is_err, sc = out["error2"]
    assert is_err and sc["code"] in ("E_BAD_ARGS", "E_PROJECT_NOT_FOUND")

    is_err, sc, text = out["schema_error"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and "action" in sc["message"] and sc["fix"]
    assert text.startswith("E_BAD_ARGS:")
    is_err, sc = out["enum_error"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and "view" in sc["message"]
    is_err, sc = out["unknown_tool"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and sc["candidates"] == list(TOOL_NAMES)


# ---- CLI --------------------------------------------------------------------------------------------


def test_cli_version_and_certify_list(capsys: pytest.CaptureFixture[str]):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith("kiwrap ")
    # --list: the repo's conformance/tests has T1/T4/T6 today, so the suite is incomplete -> 2
    assert main(["certify", "--list"]) == 2
    out = capsys.readouterr().out
    assert all(tid in out for tid, _ in CONFORMANCE_TESTS)
    assert "t1_cst_roundtrip.py" in out and "MISSING" in out


def test_cli_certify_runs_present_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """certify runs the t<N>_*.py files under --conformance-dir with pytest: green + incomplete -> 2,
    a failing file -> 1, no directory -> 2."""
    conf = tmp_path / "conformance"
    (conf / "tests").mkdir(parents=True)
    (conf / "tests" / "t1_fake.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    assert main(["certify", "--conformance-dir", str(conf)]) == 2
    assert "suite incomplete" in capsys.readouterr().out
    (conf / "tests" / "t2_fake.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    assert main(["certify", "--conformance-dir", str(conf)]) == 1
    assert "FAILED" in capsys.readouterr().out
    assert main(["certify", "--conformance-dir", str(tmp_path / "nowhere")]) == 2


def test_cli_env_prints_json(mcp_home: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["--storage-home", str(mcp_home), "env"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "installs" in data and data["storage"]["home"] == str(mcp_home.resolve())


def test_cli_parser_serve_flags():
    ns = build_parser().parse_args(["serve", "--http", "--port", "9999", "--token", "abc"])
    assert ns.http and ns.port == 9999 and ns.token == "abc" and ns.token_file is None
    ns = build_parser().parse_args(["serve", "--http", "--token-file", "t.txt"])
    assert ns.token_file == "t.txt"


def test_http_token_prefers_file_then_env_over_the_command_line(tmp_path: Path):
    """Finding: `--token` puts the secret on the command line (readable by every local process)."""
    from kiwrap.core.cli_main import resolve_http_token

    f = tmp_path / "token.txt"
    f.write_text("  from-file  ")
    assert resolve_http_token("cli", str(f), "env") == ("from-file", "--token-file")
    assert resolve_http_token("cli", None, "env") == ("env", "KIWRAP_HTTP_TOKEN")
    assert resolve_http_token("cli", None, None) == ("cli", "--token")
    token, source = resolve_http_token(None, None, "")
    assert source == "generated" and len(token) >= 32
    f.write_text("   ")
    with pytest.raises(SystemExit):
        resolve_http_token(None, str(f), None)


def test_bearer_guard_rejects_without_token_and_bad_origin():
    calls: list[str] = []

    async def app(scope, receive, send):
        calls.append("app")

    guarded = bearer_guard(app, "secret")

    async def run(headers: list[tuple[bytes, bytes]]) -> int | None:
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        await guarded({"type": "http", "headers": headers}, None, send)
        return sent[0]["status"] if sent else None

    assert anyio.run(run, []) == 401
    assert anyio.run(run, [(b"authorization", b"Bearer wrong")]) == 401
    assert anyio.run(run, [(b"authorization", b"Bearer secret"), (b"origin", b"http://evil.example")]) == 403
    assert (
        anyio.run(run, [(b"authorization", b"Bearer secret"), (b"origin", b"http://127.0.0.1:5173")]) is None
    )
    assert anyio.run(run, [(b"authorization", b"Bearer secret")]) is None
    assert calls == ["app", "app"]
