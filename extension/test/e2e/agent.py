"""The "agent" of the end-to-end test: a SECOND kiwrap MCP client, exactly the way Claude Code
talks to `kiwrap serve` (stdio, the `mcp` client library) - a separate server process from the
one the VS Code extension runs. It never sees the extension: the live GUI session is found
through the session record under the shared storage home (docs/EXTENSION.md section 6).

    uv run --project <repo> python agent.py --project <dir> get
    uv run --project <repo> python agent.py --project <dir> place --ref C1 --dx 5
    uv run --project <repo> python agent.py --project <dir> live-status
    uv run --project <repo> python agent.py --project <dir> live-screenshot --out shot.png

Every call prints one JSON line `{"event": ..., "t": <epoch ms>, ...}` per step on stdout so the
suite can time "tool call sent" against "frame arrived in the tab"; the last line is the result.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "t": int(time.time() * 1000), **fields}, default=str), flush=True)


def server_params(repo: Path) -> StdioServerParameters:
    # the same command line the extension's Configure Claude Code writes into .mcp.json
    return StdioServerParameters(
        command="uv", args=["run", "--project", str(repo), "kiwrap", "serve"], cwd=str(repo)
    )


async def main_async(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    project = str(Path(args.project).resolve())
    emit("client-start", repo=str(repo), project=project)
    async with Client(server_params(repo), mode="auto") as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        emit("connected", tools=len(tools), has_live="kw_live" in tools)

        async def call(tool: str, params: dict[str, Any]) -> dict[str, Any]:
            t0 = time.time()
            emit("call", tool=tool, args=params)
            r = await c.call_tool(tool, params)
            sc = r.structured_content or {}
            emit(
                "result",
                tool=tool,
                ms=round((time.time() - t0) * 1000),
                is_error=bool(r.is_error),
                provider=sc.get("provider_used"),
                degraded=sc.get("degraded"),
                warnings=sc.get("warnings"),
            )
            if r.is_error:
                raise SystemExit(
                    json.dumps(
                        {
                            "event": "error",
                            "tool": tool,
                            "code": sc.get("code"),
                            "message": sc.get("message"),
                            "fix": sc.get("fix"),
                        }
                    )
                )
            return sc

        async def footprints() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
            sc = await call(
                "kw_pcb_get", {"project": project, "kinds": ["footprints"], "detail": "summary", "limit": 500}
            )
            items = [it for it in (sc.get("data", {}).get("items") or []) if it.get("kind") == "footprint"]
            return sc, {str(it.get("ref")): it for it in items}

        if args.action == "get":
            sc, fps = await footprints()
            emit(
                "done",
                provider=sc.get("provider_used"),
                footprints=[
                    {
                        "ref": r,
                        "x_mm": f.get("x_mm"),
                        "y_mm": f.get("y_mm"),
                        "rotation_deg": f.get("rotation_deg"),
                        "layer": f.get("layer"),
                    }
                    for r, f in sorted(fps.items())
                ],
            )
            return 0
        if args.action == "place":
            sc, fps = await footprints()
            fp = fps[args.ref]
            x, y = float(fp["x_mm"]), float(fp["y_mm"])
            target = [round(x + args.dx, 3), round(y + args.dy, 3)]
            emit("place-start", ref=args.ref, from_=[x, y], to=target, read_provider=sc.get("provider_used"))
            sc2 = await call(
                "kw_pcb_place", {"project": project, "placements": [{"ref": args.ref, "at_mm": target}]}
            )
            emit("place-end", provider=sc2.get("provider_used"), degraded=sc2.get("degraded"))
            sc3, after = await footprints()
            emit(
                "done",
                provider=sc2.get("provider_used"),
                before=[x, y],
                to=target,
                after=[after[args.ref].get("x_mm"), after[args.ref].get("y_mm")],
                read_provider=sc3.get("provider_used"),
                warnings=sc2.get("warnings"),
            )
            return 0
        if args.action == "live-status":
            sc = await call("kw_live", {"action": "status", "project": project})
            d = sc.get("data", {})
            emit(
                "done",
                session_id=d.get("session_id"),
                pid=d.get("pid"),
                remote=d.get("remote"),
                owner_pid=d.get("owner_pid"),
                mode_used=d.get("mode_used"),
                cloaked=d.get("cloaked"),
                helper=d.get("helper"),
                editors=d.get("editors"),
            )
            return 0
        if args.action == "live-screenshot":
            r = await c.call_tool(
                "kw_live", {"action": "screenshot", "project": project, "max_px": args.max_px}
            )
            sc = r.structured_content or {}
            if r.is_error:
                raise SystemExit(
                    json.dumps(
                        {
                            "event": "error",
                            "tool": "kw_live",
                            "code": sc.get("code"),
                            "message": sc.get("message"),
                        }
                    )
                )
            img = next((b for b in r.content if getattr(b, "type", "") == "image"), None)
            out = Path(args.out)
            if img is not None:
                out.write_bytes(base64.b64decode(img.data))
            d = sc.get("data", {})
            emit(
                "done",
                source=d.get("source"),
                mode=d.get("mode"),
                width=d.get("width"),
                height=d.get("height"),
                bytes=d.get("bytes"),
                saved=str(out) if img else None,
                degraded=sc.get("degraded"),
            )
            return 0
        raise SystemExit(f"unknown action {args.action}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]))
    ap.add_argument("--project", required=True)
    ap.add_argument("action", choices=["get", "place", "live-status", "live-screenshot"])
    ap.add_argument("--ref", default="C1")
    ap.add_argument("--dx", type=float, default=5.0)
    ap.add_argument("--dy", type=float, default=0.0)
    ap.add_argument("--out", default="live-screenshot.png")
    ap.add_argument("--max-px", type=int, default=1024)
    args = ap.parse_args()
    try:
        return anyio.run(main_async, args)
    except SystemExit as exc:
        if isinstance(exc.code, str) and exc.code.startswith("{"):
            print(exc.code, flush=True)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
