"""Manual eval runner: Claude Code (`claude -p`) builds the reference board from `spec.md` over
the repo's `.mcp.json` kiwrap server, then `grader.py` grades the project with KiCad.

Run by hand only (docs/DECISIONS.md D15: no scheduled agent evals; D5: three consecutive green
runs, no human edits, is the MVP acceptance). Nothing in the test suite calls this.

    uv run python -m evals.reference_board.runner_claude --out C:/work/ref-evals --runs 3
    uv run python -m evals.reference_board.runner_claude --out ... --target-kicad 10.0 --max-turns 300

Each run gets an isolated `CLAUDE_CONFIG_DIR` (no user settings, memory or hooks), the repo root
as cwd (so `.mcp.json` resolves `${CLAUDE_PROJECT_DIR}`), the kiwrap MCP tools plus `Read`
allowed, a turn cap, and JSON output (cost, turns, duration). The prompt is `spec.md` with the
project path and target KiCad appended; the agent is pointed at `AGENTS.md` for the tool protocol.
Outputs per run under `<out>/run-<n>/`: `prompt.md`, `claude.json`, `claude.stderr`, the project
directory and `grade.json`; `<out>/summary.json` aggregates the runs. Exit status 0 = every run
passed the grade.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from evals.reference_board import grader
from evals.reference_board.plan import PLAN_PATH, load_plan

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SPEC = HERE / "spec.md"

ALLOWED_TOOLS = ("mcp__kiwrap", "Read")


def build_prompt(project_dir: Path, target_kicad: str | None, plan: dict[str, Any]) -> str:
    spec = SPEC.read_text(encoding="utf-8")
    target = f"KiCad {target_kicad}" if target_kicad else "the newest installed KiCad"
    return (
        f"{spec}\n\n"
        "## Instructions\n\n"
        f"- Read `{(REPO / 'AGENTS.md').as_posix()}` first: it is the protocol for the kiwrap MCP tools "
        "(`kw_*`), the stage gates and the receipts.\n"
        f"- Create the project with `kw_project(action='create', dir='{project_dir.as_posix()}', "
        f"name='{plan['name']}', target_kicad=...)` targeting {target}; board size 50 x 35 mm.\n"
        "- Search the libraries (`kw_lib_search` / `kw_lib_info`) before every lib_id / footprint; never guess "
        "pin positions - use the absolute pin endpoints the receipts return.\n"
        "- Validate with `kw_sch_check` (0 errors), `kw_pcb_check`, `kw_pcb_drc` (0 must_fix) and finish with "
        "`kw_fab_export(profile='jlcpcb')`. Never edit `.kicad_*` files directly, never force a gate.\n"
        "- When done, reply with the final numbers: ERC errors, unrouted connections, must_fix DRC "
        "violations and the fabrication files written.\n"
    )


def run_once(
    n: int,
    out: Path,
    *,
    claude: str,
    target_kicad: str | None,
    max_turns: int,
    model: str | None,
    timeout_s: float,
    plan: dict[str, Any],
) -> dict[str, Any]:
    run_dir = out / f"run-{n}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    config_dir = run_dir / "claude-config"
    config_dir.mkdir()
    project_dir = run_dir / "project"
    prompt = build_prompt(project_dir, target_kicad, plan)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    cmd = [
        claude,
        "-p",
        "--output-format",
        "json",
        "--mcp-config",
        str(REPO / ".mcp.json"),
        "--strict-mcp-config",
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
        "--max-turns",
        str(max_turns),
    ]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_PROJECT_DIR"] = str(REPO)
    started = time.time()
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
        env=env,
        timeout=timeout_s,
        check=False,
    )
    duration = round(time.time() - started, 1)
    (run_dir / "claude.stdout").write_text(proc.stdout, encoding="utf-8")
    (run_dir / "claude.stderr").write_text(proc.stderr, encoding="utf-8")
    result: dict[str, Any] | None
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    (run_dir / "claude.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    grade: dict[str, Any] | None = None
    if project_dir.is_dir():
        facts = grader.collect_facts(project_dir, tool_calls=(result or {}).get("num_turns"))
        grade = grader.grade(facts, plan).as_dict()
    (run_dir / "grade.json").write_text(json.dumps(grade, indent=1, default=str), encoding="utf-8")
    return {
        "run": n,
        "exit_code": proc.returncode,
        "duration_s": duration,
        "num_turns": (result or {}).get("num_turns"),
        "cost_usd": (result or {}).get("total_cost_usd"),
        "is_error": (result or {}).get("is_error"),
        "passed": bool(grade and grade["passed"]),
        "failures": (grade or {}).get("failures"),
        "dir": str(run_dir),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="run Claude Code on the reference-board spec and grade it (manual)"
    )
    ap.add_argument("--out", type=Path, required=True, help="directory for the runs (created)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--claude", default="claude", help="the Claude Code executable")
    ap.add_argument("--target-kicad", default=None, help="major.minor to target (default: newest)")
    ap.add_argument("--max-turns", type=int, default=None, help="turn cap (default: the plan's budget)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout-s", type=float, default=None, help="per run (default: the plan's budget)")
    ap.add_argument("--plan", type=Path, default=PLAN_PATH)
    args = ap.parse_args(argv)
    plan = load_plan(args.plan)
    max_turns = args.max_turns or int(plan["budget"]["max_tool_calls"])
    timeout_s = args.timeout_s or float(plan["budget"]["max_minutes"]) * 60
    if shutil.which(args.claude) is None and not Path(args.claude).exists():
        print(f"claude executable not found: {args.claude!r} (pass --claude <path>)", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for n in range(1, args.runs + 1):
        row = run_once(
            n,
            args.out,
            claude=args.claude,
            target_kicad=args.target_kicad,
            max_turns=max_turns,
            model=args.model,
            timeout_s=timeout_s,
            plan=plan,
        )
        rows.append(row)
        print(json.dumps(row, default=str))
    summary = {"runs": rows, "all_passed": all(r["passed"] for r in rows), "consecutive_green": _streak(rows)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}))
    return 0 if summary["all_passed"] else 1


def _streak(rows: list[dict[str, Any]]) -> int:
    best = cur = 0
    for r in rows:
        cur = cur + 1 if r["passed"] else 0
        best = max(best, cur)
    return best


if __name__ == "__main__":
    sys.exit(main())
