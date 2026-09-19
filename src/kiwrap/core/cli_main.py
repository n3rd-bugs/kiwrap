"""`kiwrap` command line: `serve`, `env`, `certify`, `version`.

`kiwrap serve` speaks MCP over stdio (default). `--http` binds streamable HTTP to 127.0.0.1 with
a bearer token printed once on stderr and an Origin/Host check (docs/ARCHITECTURE.md §9
security model: "HTTP only on 127.0.0.1 with a bearer token and origin policy"). The token
check is a small ASGI wrapper around the SDK's Starlette app; the SDK's own DNS-rebinding
protection (Host/Origin allow-lists for localhost) stays enabled underneath.

Token sources, in order: `--token-file <path>` (read, stripped), the `KIWRAP_HTTP_TOKEN`
environment variable, `--token <value>` (kept for convenience, with a warning: every local
process can read another process's command line), else a fresh `secrets.token_urlsafe(32)`
printed once.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kiwrap import __version__

ASGIApp = Callable[..., Awaitable[None]]  # (scope, receive, send) -> None; Starlette apps satisfy it

LOCAL_ORIGINS = (
    "http://127.0.0.1",
    "http://localhost",
    "http://[::1]",
    "https://127.0.0.1",
    "https://localhost",
)


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # non-browser clients send no Origin header
    o = origin.strip().lower()
    return any(o == base or o.startswith(base + ":") for base in LOCAL_ORIGINS)


def bearer_guard(app: ASGIApp, token: str) -> ASGIApp:
    """Reject HTTP requests without `Authorization: Bearer <token>` or with a non-local Origin."""

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        status: int | None = None
        if not _origin_allowed(headers.get("origin")):
            status = 403
        elif not secrets.compare_digest(headers.get("authorization", ""), f"Bearer {token}"):
            status = 401
        if status is None:
            await app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized" if status == 401 else "forbidden origin"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return guarded


def cmd_serve(args: argparse.Namespace) -> int:
    from kiwrap.core.server import build_server

    mcp = build_server(Path(args.storage_home) if args.storage_home else None)
    if not args.http:
        mcp.run("stdio")
        return 0
    import anyio
    import uvicorn

    token, source = resolve_http_token(args.token, args.token_file, os.environ.get(TOKEN_ENV))
    app = bearer_guard(mcp.streamable_http_app(host="127.0.0.1", stateless_http=True), token)
    print(f"kiwrap MCP over HTTP: http://127.0.0.1:{args.port}/mcp", file=sys.stderr)
    if source == "generated":
        print(f"Authorization: Bearer {token}", file=sys.stderr)
        print(
            "(the token is printed once; pass it in the Authorization header of every request)",
            file=sys.stderr,
        )
    else:
        print(f"bearer token taken from {source}", file=sys.stderr)
    if source == "--token":
        print(
            f"warning: --token is visible to every local process (ps / Win32_Process); prefer {TOKEN_ENV} "
            "or --token-file",
            file=sys.stderr,
        )
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    anyio.run(uvicorn.Server(config).serve)
    return 0


TOKEN_ENV = "KIWRAP_HTTP_TOKEN"


def resolve_http_token(
    cli_token: str | None, token_file: str | None, env_token: str | None
) -> tuple[str, str]:
    """(token, source) with source in {'--token-file', 'KIWRAP_HTTP_TOKEN', '--token', 'generated'}."""
    if token_file:
        value = Path(token_file).read_text(encoding="utf-8").strip()
        if not value:
            raise SystemExit(f"error: {token_file} is empty")
        return value, "--token-file"
    if env_token and env_token.strip():
        return env_token.strip(), TOKEN_ENV
    if cli_token:
        return cli_token, "--token"
    return secrets.token_urlsafe(32), "generated"


def cmd_env(args: argparse.Namespace) -> int:
    from kiwrap.core.server import build_context
    from kiwrap.core.tools import env as env_tool

    ctx = build_context(Path(args.storage_home) if args.storage_home else None)
    receipt = env_tool.run(ctx, env_tool.EnvArgs(refresh=args.refresh))
    print(json.dumps(receipt.data, indent=2, default=str))
    return 0


#: The T1-T9 certification suite (docs/ARCHITECTURE.md §6); each row is (id, description).
#: A test lives in `conformance/tests/t<N>_<topic>.py` (several files may share an id, e.g. T6).
CONFORMANCE_TESTS = (
    ("T1", "CST round trip: dumps(loads(file)) is byte-identical for corpus + every shipped demo"),
    ("T2", "epoch certification: files written in each epoch load on every install >= that epoch"),
    ("T3", "negative gates: a future stamp / unknown token is rejected exactly as KiCad rejects it"),
    ("T4", "`kicad-cli --help` tree snapshot per installed version"),
    ("T5", "IPC handler snapshot per KiCad version"),
    ("T6", "quirk probes: ERC x100, gerbers outdir, .kicad_prl, help exit code, sandbox seeding"),
    ("T7", "SWIG worker: symbols, shims, consecutive saves, DSN/SES"),
    ("T8", "end-to-end golden flow"),
    ("T9", "drift canaries: sch/pcb upgrade on a copy + semantic diff, pcbnew.py API diff"),
)
CERTIFY_EXIT_INCOMPLETE = 2


def default_conformance_dir() -> Path | None:
    """`<repo>/conformance` when kiwrap runs from a source checkout (the suite is not packaged)."""
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        cand = base / "conformance" / "tests"
        if cand.is_dir():
            return cand.parent
    return None


def conformance_files(conformance_dir: Path) -> dict[str, list[Path]]:
    """`{"T1": [t1_cst_roundtrip.py], "T6": [t6_cli_quirks.py, t6_sandbox_quirks.py], ...}`."""
    found: dict[str, list[Path]] = {}
    for f in sorted((conformance_dir / "tests").glob("t[0-9]*_*.py")):
        tid = "T" + f.name[1:].split("_", 1)[0]
        found.setdefault(tid, []).append(f)
    return found


def cmd_certify(args: argparse.Namespace) -> int:
    """Run every T-file present under `conformance/tests` with pytest (a fresh `KIWRAP_HOME` is
    picked by the repository's root conftest, so the real storage home is never touched).

    Exit 0 = every T1-T9 file present and green; 1 = a present test failed;
    2 = the suite is incomplete (some T-ids have no file yet) or no conformance directory found.
    """
    import subprocess

    conf = Path(args.conformance_dir) if args.conformance_dir else default_conformance_dir()
    if conf is None or not (conf / "tests").is_dir():
        print(
            "kiwrap certify: no conformance/tests directory found (run from a source checkout or pass --conformance-dir)"
        )
        return CERTIFY_EXIT_INCOMPLETE
    present = conformance_files(conf)
    print(f"kiwrap certify: conformance suite under {conf}")
    for tid, desc in CONFORMANCE_TESTS:
        files = ", ".join(f.name for f in present.get(tid, [])) or "MISSING"
        print(f"  {tid:3}  {desc}")
        print(f"       {files}")
    missing = [tid for tid, _ in CONFORMANCE_TESTS if tid not in present]
    if args.list:
        return CERTIFY_EXIT_INCOMPLETE if missing else 0
    targets = [str(f) for files in present.values() for f in files]
    if not targets:
        return CERTIFY_EXIT_INCOMPLETE
    if importlib.util.find_spec("pytest") is None:
        print("kiwrap certify: pytest is not installed in this environment (uv sync --all-extras)")
        return CERTIFY_EXIT_INCOMPLETE
    cmd = [sys.executable, "-m", "pytest", "-q", *targets, *(args.pytest_args or [])]
    print("  running:", " ".join(cmd[2:]), flush=True)
    rc = subprocess.run(cmd, cwd=conf.parent).returncode
    if rc != 0:
        print(f"kiwrap certify: FAILED (pytest exit {rc})")
        return 1
    if missing:
        print(f"kiwrap certify: present tests passed; suite incomplete, missing {', '.join(missing)}")
        return CERTIFY_EXIT_INCOMPLETE
    print("kiwrap certify: PASSED (T1-T9 all present and green)")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"kiwrap {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiwrap", description="AI-agent-facing wrapper over installed KiCad binaries"
    )
    p.add_argument(
        "--storage-home",
        help="override kiwrap's state directory (default: KIWRAP_HOME or the platform default)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    s.add_argument("--http", action="store_true", help="streamable HTTP on 127.0.0.1 instead of stdio")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument(
        "--token",
        help="bearer token (convenience only: visible to every local process; prefer KIWRAP_HTTP_TOKEN or "
        "--token-file; default: generated and printed once)",
    )
    s.add_argument("--token-file", help="file holding the bearer token (read once, whitespace stripped)")
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser("env", help="print installed KiCads and their capabilities as JSON")
    e.add_argument("--refresh", action="store_true")
    e.set_defaults(func=cmd_env)

    c = sub.add_parser(
        "certify",
        help="run the T1-T9 conformance suite (exit 0 certified, 1 failures, 2 incomplete)",
    )
    c.add_argument("--conformance-dir", help="the conformance/ directory (default: this checkout's)")
    c.add_argument("--list", action="store_true", help="list which T-files exist without running them")
    c.add_argument("pytest_args", nargs="*", help="extra pytest arguments, e.g. -k t1 or -x")
    c.set_defaults(func=cmd_certify)

    v = sub.add_parser("version", help="print the kiwrap version")
    v.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
