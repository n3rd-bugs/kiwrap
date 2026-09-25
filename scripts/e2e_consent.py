"""Manual end-to-end check of the D24 format-upgrade consent with a real Claude Code login.

    uv run python scripts/e2e_consent.py prepare [--source SPEC] [--target SPEC] [--storage-home DIR]
    uv run python scripts/e2e_consent.py verify-deny     [--run DIR] [--design-only]
    uv run python scripts/e2e_consent.py verify-approve  [--run DIR]
    uv run python scripts/e2e_consent.py subagent --scenario task|workflow|bypass --card yes|no [--run DIR]
    uv run python scripts/e2e_consent.py headless        [--run DIR] [--result FILE]
    uv run python scripts/e2e_consent.py revert-check    [--run DIR] [--workspace DIR ...]

docs/DECISIONS.md D24 makes `kw_project_upgrade` the only conversion of a project to a newer KiCad
file format and relies on Claude Code's `anthropic/requiresUserInteraction` gate to ask the user
(docs/VERIFICATION.md claim 13, verified statically). This script is the run-time check the claim
leaves open. It is MANUAL ONLY (D15): it never starts Claude, never calls the API and spends
nothing - it prepares a scratch project, prints the exact prompts and commands for the owner to
run, and then checks what happened on disk.

`prepare` copies `<source install>/share/kicad/demos/pic_programmer` (the source is the newest
install whose file formats are older than the target's; `--source` overrides) to
`Platform.temp_dir/kiwrap-e2e-consent/<utc>/`, pins it in process with
`kw_project(action='retarget')` to the newest install that writes a newer format (`--target`), reads
`kw_project(action='status')` for `upgrade_args`, writes `manifest.json` (sha256 of every file,
format stamps, the snapshot HEAD, state.json, the permission-settings baseline) and prints the
checklist. Each scenario needs a fresh `prepare` (an approved run upgrades the copy).

`verify-deny` / `verify-approve` / `subagent` / `headless --result` / `revert-check` check the
outcome against the manifest and exit 0 (PASS) or 1 (FAIL); 2 is a usage error. Only the stdlib
and kiwrap are imported; every KiCad process runs inside kiwrap's sandbox, and nothing is written
outside the run directory, the scratch project and kiwrap's storage home.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.server import build_context, build_server
from kiwrap.core.state.project import Project
from kiwrap.core.state.snapshots import git_executable, head, is_owned_file, list_snapshots, store_dir
from kiwrap.platform import current

REPO = Path(__file__).resolve().parents[1]
DEMO = "pic_programmer"
UPGRADE_TOOL = "kw_project_upgrade"
MCP_TOOL_NAME = "mcp__kiwrap__kw_project_upgrade"
ALLOW_RULE = "mcp__kiwrap__*"
#: docs/VERIFICATION.md claim 13 / code.claude.com/docs/en/mcp "Require approval for a specific tool"
#: (a Claude Code version, not a KiCad one; kiwrap.core.tools._base.CLAUDE_CODE_MIN_CONSENT)
MIN_CLAUDE_CODE = (2, 1, 199)
EXPECTED_GATE = "anthropic/requiresUserInteraction"
EXPECTED_VIA = "client_permission_flow"
VSCODE_BYPASS_SETTING = "claudeCode.allowDangerouslySkipPermissions"
STATE_REL = ".kiwrap/state.json"


# ---- run directory and manifest ----------------------------------------------------------------------


def base_dir() -> Path:
    return current().temp_dir / "kiwrap-e2e-consent"


@dataclass
class Manifest:
    """What `prepare` recorded; every verify step compares against it."""

    created_utc: str
    run_dir: str
    project_dir: str
    storage_home: str
    source: dict[str, Any]
    target: dict[str, Any]
    files: dict[str, str]  # project-relative posix path -> sha256 (everything but .kiwrap/)
    stamps: dict[str, dict[str, Any]]  # design file -> {kind, stamp}
    snapshot_head: str | None
    state_sha256: str | None
    upgrade_args: dict[str, Any] | None
    expected_files: list[str]  # the files the approved call must convert
    to_format: dict[str, int]
    settings_baseline: list[str] = field(default_factory=list)  # pre-existing mcp__kiwrap lines
    vscode_bypass_setting: str | None = None
    claude_bundle: str | None = None

    @property
    def project(self) -> Path:
        return Path(self.project_dir)

    @property
    def run(self) -> Path:
        return Path(self.run_dir)

    def save(self) -> None:
        (self.run / "manifest.json").write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, run: Path) -> Manifest:
        return cls(**json.loads((run / "manifest.json").read_text(encoding="utf-8")))


def resolve_run(arg: str | None) -> Path:
    if arg:
        run = Path(arg).expanduser().resolve()
    else:
        latest = base_dir() / "latest.txt"
        if not latest.is_file():
            raise SystemExit(f"no run yet: {latest} is missing - run `prepare` first (or pass --run)")
        run = Path(latest.read_text(encoding="utf-8").strip())
    if not (run / "manifest.json").is_file():
        raise SystemExit(f"{run} has no manifest.json - run `prepare` first (or pass --run)")
    return run


# ---- files, hashes, stamps ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(project_dir: Path) -> dict[str, str]:
    """sha256 of every file under the project except kiwrap's own `.kiwrap/` directory."""
    out: dict[str, str] = {}
    for path in sorted(project_dir.rglob("*")):
        rel = path.relative_to(project_dir).as_posix()
        if rel == ".kiwrap" or rel.startswith(".kiwrap/") or not path.is_file():
            continue
        out[rel] = sha256_file(path)
    return out


def stamps(project_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        s.path: {"kind": s.kind, "stamp": s.stamp} for s in Project.from_path(project_dir).format_stamps()
    }


def state_sha(project_dir: Path) -> str | None:
    path = project_dir / STATE_REL
    return sha256_file(path) if path.is_file() else None


def read_state(project_dir: Path) -> dict[str, Any]:
    path = project_dir / STATE_REL
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@dataclass
class TreeDiff:
    changed: list[str]
    missing: list[str]
    added: list[str]

    @property
    def clean(self) -> bool:
        return not (self.changed or self.missing or self.added)


def diff_tree(expected: dict[str, str], actual: dict[str, str], ignore: Iterable[str] = ()) -> TreeDiff:
    skip = set(ignore)
    return TreeDiff(
        changed=sorted(p for p in expected if p in actual and actual[p] != expected[p] and p not in skip),
        missing=sorted(p for p in expected if p not in actual and p not in skip),
        added=sorted(p for p in actual if p not in expected and p not in skip),
    )


# ---- the private snapshot store (read-only git) ------------------------------------------------------


def store_git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """A read-only git command against `<project>/.kiwrap/snapshots.git` (never an enclosing repo)."""
    argv = [git_executable(), f"--git-dir={store_dir(project_dir)}", f"--work-tree={project_dir}", *args]
    return subprocess.run(argv, capture_output=True, check=False)


def blob_sha256(project_dir: Path, ref: str, rel: str) -> str | None:
    proc = store_git(project_dir, "show", f"{ref}:{rel}")
    return hashlib.sha256(proc.stdout).hexdigest() if proc.returncode == 0 else None


# ---- permission settings (for the allow-rule and bypass steps) ---------------------------------------


def settings_files(workspaces: Iterable[Path] = ()) -> list[Path]:
    dirs = [Path.home() / ".claude", REPO / ".claude", *(Path(w) / ".claude" for w in workspaces)]
    seen: list[Path] = []
    for d in dirs:
        for p in sorted(d.glob("settings*.json")):
            if p.resolve() not in seen:
                seen.append(p.resolve())
    return seen


def kiwrap_rule_lines(workspaces: Iterable[Path] = ()) -> list[str]:
    """`<file>: <line>` for every settings line that mentions an `mcp__kiwrap` rule."""
    out: list[str] = []
    for path in settings_files(workspaces):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "mcp__kiwrap" in line:
                out.append(f"{path}: {line.strip()}")
    return out


def vscode_settings_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Code" / "User" / "settings.json"
    return Path.home() / ".config" / "Code" / "User" / "settings.json"


def vscode_bypass_setting() -> str | None:
    """The raw value of `claudeCode.allowDangerouslySkipPermissions` in VS Code's user settings
    (JSONC, so read with a regex), or None when it is not set."""
    path = vscode_settings_path()
    if not path.is_file():
        return None
    m = re.search(
        r'"' + re.escape(VSCODE_BYPASS_SETTING) + r'"\s*:\s*(true|false)',
        path.read_text(encoding="utf-8", errors="replace"),
    )
    return m.group(1) if m else None


# ---- the Claude Code bundle --------------------------------------------------------------------------

_BUNDLE_VERSION = re.compile(r"anthropic\.claude-code-(\d+)\.(\d+)\.(\d+)")


def claude_binary() -> Path | None:
    """The bundled claude.exe: `CLAUDE_CODE_EXECPATH` (the running one), else the newest VS Code
    extension bundle. There is no `claude` on PATH on the owner's machine (D24 key evidence)."""
    running = os.environ.get("CLAUDE_CODE_EXECPATH")
    if running and Path(running).is_file():
        return Path(running)
    found = list(
        Path.home().glob(".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe")
    )

    def key(p: Path) -> tuple[int, ...]:
        m = _BUNDLE_VERSION.search(p.as_posix())
        return tuple(int(x) for x in m.groups()) if m else (-1,)

    return max(found, key=key) if found else None


def parse_version(text: str | None) -> tuple[int, ...] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


# ---- reporting ---------------------------------------------------------------------------------------


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.failures: list[str] = []
        print(f"== {title}")

    def check(self, ok: bool, text: str) -> bool:
        print(f"  [{'ok' if ok else 'FAIL'}] {text}")
        if not ok:
            self.failures.append(text)
        return ok

    def info(self, text: str) -> None:
        print(f"  [info] {text}")

    def finish(self) -> int:
        print(f"== {self.title}: {'PASS' if not self.failures else 'FAIL'}")
        return 0 if not self.failures else 1


def report_tree(rep: Report, diff: TreeDiff, what: str) -> None:
    rep.check(
        not diff.changed, f"{what}: no file changed" + (f" (changed: {diff.changed})" if diff.changed else "")
    )
    rep.check(
        not diff.missing, f"{what}: no file removed" + (f" (removed: {diff.missing})" if diff.missing else "")
    )
    rep.check(not diff.added, f"{what}: no file added" + (f" (added: {diff.added})" if diff.added else ""))


# ---- prepare -----------------------------------------------------------------------------------------


def call_tool(server: Any, name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """One tool call through the in-process server (`KiwrapServer.call_tool`, the wire's path)."""
    result = asyncio.run(server.call_tool(name, args))
    return bool(getattr(result, "is_error", False)), dict(getattr(result, "structured_content", None) or {})


def _formats(ctx: Any, install: Any) -> tuple[int, int]:
    caps = ctx.services.caps(install)
    return int(caps["formats.sch"]), int(caps["formats.pcb"])


def pick_installs(ctx: Any, source_spec: str | None, target_spec: str | None) -> tuple[Any, Any]:
    """(source, target): the target is the newest install that can run `sch/pcb upgrade` and writes
    the highest formats; the source is the newest install writing strictly older formats that ships
    the demo. No KiCad version is named here: both come from the capability records."""
    installs = list(ctx.services.installs())
    if target_spec:
        target = ctx.services.install(target_spec)
    else:
        able = [i for i in installs if ctx.services.caps(i).has("cli.has_sch_pcb_upgrade")]
        if not able:
            raise SystemExit("no installed KiCad has cli.has_sch_pcb_upgrade; kw_project_upgrade cannot run")
        best = max(_formats(ctx, i) for i in able)
        target = next(i for i in able if _formats(ctx, i) == best)
    tf = _formats(ctx, target)
    if source_spec:
        source = ctx.services.install(source_spec)
    else:
        older = [
            i
            for i in installs
            if all(a < b for a, b in zip(_formats(ctx, i), tf, strict=True))
            and (Path(i.share) / "demos" / DEMO).is_dir()
        ]
        if not older:
            raise SystemExit(
                f"no install writes an older format than {target.version} and ships demos/{DEMO}"
            )
        source = older[0]
    return source, target


def _install_record(ctx: Any, install: Any) -> dict[str, Any]:
    sch, pcb = _formats(ctx, install)
    return {"version": install.version, "root": str(install.root), "formats": {"sch": sch, "pcb": pcb}}


def _make_writable(tree: Path) -> None:
    for path in tree.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)


def cmd_prepare(args: argparse.Namespace) -> int:
    storage = Path(args.storage_home).expanduser().resolve() if args.storage_home else None
    ctx = build_context(storage)
    server = build_server(context=ctx)
    source, target = pick_installs(ctx, args.source, args.target)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run = base_dir() / stamp
    project_dir = run / DEMO
    run.mkdir(parents=True, exist_ok=False)
    shutil.copytree(Path(source.share) / "demos" / DEMO, project_dir)
    _make_writable(project_dir)
    print(f"copied {Path(source.share) / 'demos' / DEMO} -> {project_dir}")

    err, sc = call_tool(
        server,
        "kw_project",
        {"action": "retarget", "project": str(project_dir), "target_kicad": str(target.root)},
    )
    if err:
        print(json.dumps(sc, indent=2))
        raise SystemExit("kw_project retarget failed (above)")
    err, status = call_tool(server, "kw_project", {"action": "status", "project": str(project_dir)})
    if err:
        print(json.dumps(status, indent=2))
        raise SystemExit("kw_project status failed (above)")
    data = status.get("data") or {}
    if data.get("epoch_match") is not False:
        raise SystemExit(
            f"the pinned KiCad writes the files' format (epoch_match={data.get('epoch_match')!r})"
        )
    upgrade_args = data.get("upgrade_args")
    tfmt = _formats(ctx, target)
    to_format = dict((upgrade_args or {}).get("to_format") or {"sch": tfmt[0], "pcb": tfmt[1]})
    current_stamps = stamps(project_dir)
    expected = (
        sorted(upgrade_args["files"])
        if upgrade_args and upgrade_args.get("files")
        else sorted(p for p, s in current_stamps.items() if s["stamp"] != to_format.get(s["kind"]))
    )
    bundle = claude_binary()
    manifest = Manifest(
        created_utc=stamp,
        run_dir=str(run),
        project_dir=str(project_dir),
        storage_home=str(ctx.services.storage_home),
        source=_install_record(ctx, source),
        target=_install_record(ctx, target),
        files=hash_tree(project_dir),
        stamps=current_stamps,
        snapshot_head=head(project_dir),
        state_sha256=state_sha(project_dir),
        upgrade_args=upgrade_args,
        expected_files=expected,
        to_format={k: int(v) for k, v in to_format.items()},
        settings_baseline=kiwrap_rule_lines(),
        vscode_bypass_setting=vscode_bypass_setting(),
        claude_bundle=str(bundle) if bundle else None,
    )
    manifest.save()
    write_headless_files(manifest)
    (base_dir() / "latest.txt").write_text(str(run), encoding="utf-8")
    print_checklist(manifest)
    return 0


def lead_prompt(m: Manifest) -> str:
    return (
        f"Using only the kiwrap MCP tools: the KiCad project at {m.project.as_posix()} is pinned to a KiCad that "
        "writes a newer file format. Read kw_project(action='status') for it, tell me what an upgrade would "
        "change, then convert the files with kw_project_upgrade, passing the status's upgrade_args verbatim. "
        "If that call is denied, stop and tell me; do not change any file in another way."
    )


def print_checklist(m: Manifest) -> None:
    me = f"uv run python {Path(__file__).relative_to(REPO).as_posix()}"
    args = (
        json.dumps(m.upgrade_args, indent=2) if m.upgrade_args else "(status returned none - is S3 landed?)"
    )
    print(
        f"""
== D24 consent E2E - run {m.created_utc}
project : {m.project}
from    : KiCad {m.source["version"]} format ({m.source["root"]})
to      : KiCad {m.target["version"]} ({m.target["root"]}), formats {m.to_format}
files   : {m.expected_files}
upgrade_args (kw_project status):
{args}
Claude Code bundle: {m.claude_bundle or "not found"}
VS Code {VSCODE_BYPASS_SETTING} now: {m.vscode_bypass_setting}

Nothing below is run by this script; each step is yours (D15). Re-run `prepare` before every scenario.

 0. Restart the kiwrap servers so the panel's server has kw_project_upgrade: VS Code "Developer: Reload
    Window" (or /mcp -> kiwrap -> reconnect). Close every KiCad window on the project.
 1. Add the TEMPORARY allow rule "{ALLOW_RULE}" to {REPO / ".claude" / "settings.local.json"}:
      {{"permissions": {{"allow": ["{ALLOW_RULE}"]}}}}
    (it proves the flag beats an allow rule). In the Claude Code panel switch to Auto mode.
 2. LEAD SCENARIO, deny. Paste into a new panel conversation:
      {lead_prompt(m)}
    Tick: [ ] a permission card appeared for kw_project_upgrade
          [ ] it shows from_kicad, to_kicad and files
          [ ] it has NO "always allow" / "don't ask again" option
    Click No / Deny. Then:  {me} verify-deny
 3. LEAD SCENARIO, approve: `{me} prepare` again, paste the new prompt, click Yes. Then:
      {me} verify-approve
 4. Optional, Bypass mode: set VS Code "{VSCODE_BYPASS_SETTING}": true, `prepare`, switch the panel to
    Bypass permissions, paste the prompt. Then:  {me} subagent --scenario bypass --card yes|no
    Put the setting back to {m.vscode_bypass_setting!r} afterwards.
 5. Optional, foreground Task subagent: `prepare`, then ask "Use a subagent (the Task tool) to do this: "
    + the prompt. Then:  {me} subagent --scenario task --card yes|no
 6. Optional, workflow subagent: `prepare`, run the prompt from a workflow agent. Then:
      {me} subagent --scenario workflow --card yes|no
    (4-6 pass when a card appeared in the panel or the call was denied; they fail if the files changed
    without a card.)
 7. Optional, SPENDS TOKENS: `prepare`, then  {me} headless   (prints the command; you run it)
 8. Remove the allow rule from step 1, then:  {me} revert-check
"""
    )


# ---- verify-deny / verify-approve / subagent ---------------------------------------------------------


def check_unchanged(rep: Report, m: Manifest, *, design_only: bool = False) -> None:
    report_tree(rep, diff_tree(m.files, hash_tree(m.project)), "project files vs manifest")
    now_head, now_state = head(m.project), state_sha(m.project)
    if design_only:
        rep.info(
            f"snapshot HEAD {m.snapshot_head} -> {now_head}; state.json changed: {now_state != m.state_sha256}"
        )
        return
    rep.check(now_head == m.snapshot_head, f"snapshot HEAD unchanged ({m.snapshot_head} -> {now_head})")
    rep.check(now_state == m.state_sha256, "`.kiwrap/state.json` unchanged")


def cmd_verify_deny(args: argparse.Namespace) -> int:
    m = Manifest.load(resolve_run(args.run))
    rep = Report(f"verify-deny {m.created_utc}")
    check_unchanged(rep, m, design_only=args.design_only)
    rep.info("also tick the card boxes from the checklist (appeared; from/to/files shown; no always-allow)")
    return rep.finish()


def last_upgrade_entry(state: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """(the newest kw_project_upgrade history entry, whether it is the last entry of all)."""
    history = list(state.get("history") or [])
    for idx in range(len(history) - 1, -1, -1):
        if history[idx].get("tool") == UPGRADE_TOOL:
            return history[idx], idx == len(history) - 1
    return None, False


def check_consent(rep: Report, entry: dict[str, Any] | None) -> None:
    consent = dict((entry or {}).get("consent") or {})
    client = dict(consent.get("client") or {})
    rep.check(
        consent.get("gate") == EXPECTED_GATE,
        f"consent.gate == {EXPECTED_GATE!r} (got {consent.get('gate')!r})",
    )
    rep.check(
        consent.get("via") == EXPECTED_VIA, f"consent.via == {EXPECTED_VIA!r} (got {consent.get('via')!r})"
    )
    rep.check(
        client.get("name") == "claude-code",
        f"consent.client.name == 'claude-code' (got {client.get('name')!r})",
    )
    version = parse_version(client.get("version"))
    rep.check(
        version is not None and version >= MIN_CLAUDE_CODE,
        f"consent.client.version >= {'.'.join(map(str, MIN_CLAUDE_CODE))} (got {client.get('version')!r})",
    )
    rep.info(f"consent.entrypoint = {consent.get('entrypoint')!r}")


def pin_forms(m: Manifest) -> list[str]:
    """The specs an approved upgrade may pin (D24): the target's version, or its install root when
    installs sharing that version write different formats (`project._install_spec`, which both
    `upgrade_args.to_kicad` and the pin use) - review finding 11: a bench build next to the stock
    nightly pins the root, and a version-only check reported a false FAIL."""
    forms = [str(m.target.get("version") or ""), str(m.target.get("root") or "")]
    forms.append(str((m.upgrade_args or {}).get("to_kicad") or ""))
    return [f for n, f in enumerate(forms) if f and f not in forms[:n]]


def _spec_key(spec: str) -> str:
    """A version compares as itself; a path after normalising separators (and case on Windows)."""
    return os.path.normcase(os.path.normpath(spec)) if ("/" in spec or "\\" in spec) else spec


def same_spec(pinned: Any, accepted: Iterable[str]) -> bool:
    return isinstance(pinned, str) and bool(pinned) and _spec_key(pinned) in {_spec_key(a) for a in accepted}


def check_approved(rep: Report, m: Manifest) -> None:
    now = stamps(m.project)
    for rel in m.expected_files:
        want = m.to_format.get(str(m.stamps.get(rel, {}).get("kind")))
        got = now.get(rel, {}).get("stamp")
        rep.check(got == want, f"{rel}: stamp {m.stamps.get(rel, {}).get('stamp')} -> {got} (want {want})")
    report_tree(rep, diff_tree(m.files, hash_tree(m.project), ignore=m.expected_files), "every other file")

    snaps = list_snapshots(m.project, 200)
    upgrade = [
        s for s in snaps if s.message.startswith(f"[kiwrap] {UPGRADE_TOOL}") and "FAILED" not in s.message
    ]
    if not rep.check(bool(upgrade), f"a '[kiwrap] {UPGRADE_TOOL}' snapshot exists"):
        return
    state = read_state(m.project)
    entry, is_last = last_upgrade_entry(state)
    before = str((entry or {}).get("snapshot_before") or "")
    if not before:
        parent = store_git(m.project, "rev-parse", f"{upgrade[0].sha}^")
        before = parent.stdout.decode().strip() if parent.returncode == 0 else ""
    rep.info(f"snapshot_before = {before or None} (manifest HEAD {m.snapshot_head})")
    owned = [rel for rel in m.files if is_owned_file(rel)]
    bad = [rel for rel in owned if blob_sha256(m.project, before, rel) != m.files[rel]] if before else owned
    rep.check(not bad, "snapshot_before's tree equals the manifest" + (f" (differs: {bad})" if bad else ""))

    pinned = state.get("target_kicad")
    accepted = pin_forms(m)
    rep.check(
        state.get("target_source") == "pinned" and same_spec(pinned, accepted),
        f"state target pinned to {' or '.join(accepted)} (got {pinned!r} / {state.get('target_source')!r})",
    )
    rep.check(entry is not None, f"a {UPGRADE_TOOL} history entry exists")
    if entry is not None and not is_last:
        rep.info(f"later calls followed the upgrade; checked the newest {UPGRADE_TOOL} entry")
    check_consent(rep, entry)


def cmd_verify_approve(args: argparse.Namespace) -> int:
    m = Manifest.load(resolve_run(args.run))
    rep = Report(f"verify-approve {m.created_utc}")
    check_approved(rep, m)
    return rep.finish()


def cmd_subagent(args: argparse.Namespace) -> int:
    m = Manifest.load(resolve_run(args.run))
    rep = Report(f"{args.scenario} scenario {m.created_utc} (card: {args.card})")
    diff = diff_tree(m.files, hash_tree(m.project))
    if diff.clean:
        rep.check(True, "the design files are unchanged: the call was denied or never reached kiwrap")
        rep.info(f"state.json changed: {state_sha(m.project) != m.state_sha256}")
    elif args.card == "no":
        rep.check(
            False, f"the files changed WITHOUT a permission card: {diff.changed + diff.added + diff.missing}"
        )
    else:
        rep.check(True, "a card appeared and the files changed after the approval")
        check_approved(rep, m)
    return rep.finish()


# ---- headless (spends tokens only when the owner runs the printed command) ---------------------------


def headless_paths(m: Manifest) -> tuple[Path, Path, Path]:
    d = m.run / "headless"
    return d / "mcp.json", d / "prompt.txt", d / "result.json"


def write_headless_files(m: Manifest) -> None:
    mcp_json, prompt, _ = headless_paths(m)
    mcp_json.parent.mkdir(parents=True, exist_ok=True)
    server = {"command": "uv", "args": ["run", "--project", str(REPO), "kiwrap", "serve"], "env": {}}
    mcp_json.write_text(json.dumps({"mcpServers": {"kiwrap": server}}, indent=2), encoding="utf-8")
    call = json.dumps(m.upgrade_args) if m.upgrade_args else "the upgrade_args of kw_project(action='status')"
    prompt.write_text(
        f"Call the kiwrap tool {UPGRADE_TOOL} with exactly these arguments: {call}. Then report the result.",
        encoding="utf-8",
    )


def cmd_headless(args: argparse.Namespace) -> int:
    m = Manifest.load(resolve_run(args.run))
    mcp_json, prompt, result = headless_paths(m)
    if not args.result:
        exe = claude_binary()
        if exe is None:
            print("no bundled claude.exe found (CLAUDE_CODE_EXECPATH or ~/.vscode/extensions)")
            return 2
        write_headless_files(m)
        me = f"uv run python {Path(__file__).relative_to(REPO).as_posix()}"
        print(
            f"""== headless check (OPTIONAL - it SPENDS TOKENS; this script never runs it, D15)
Run in PowerShell from {m.run}:

& "{exe}" -p (Get-Content -Raw "{prompt}") --output-format json --permission-mode bypassPermissions `
    --allowedTools {MCP_TOOL_NAME} --strict-mcp-config --mcp-config "{mcp_json}" `
    | Out-File -Encoding utf8 "{result}"

then:  {me} headless --result "{result}"
PASS = result.permission_denials names {MCP_TOOL_NAME} and every file hash is unchanged."""
        )
        return 0
    rep = Report(f"headless {m.created_utc}")
    text = Path(args.result).read_text(encoding="utf-8-sig", errors="replace")
    payload: Any = json.loads(text)
    if isinstance(payload, list):  # a stream: take the final result message
        payload = next(
            (x for x in reversed(payload) if isinstance(x, dict) and x.get("type") == "result"), {}
        )
    denials = [d for d in (payload.get("permission_denials") or []) if isinstance(d, dict)]
    names = [str(d.get("tool_name")) for d in denials]
    rep.check(MCP_TOOL_NAME in names, f"result.permission_denials names {MCP_TOOL_NAME} (got {names})")
    check_unchanged(rep, m)
    return rep.finish()


# ---- revert-check ------------------------------------------------------------------------------------


def cmd_revert_check(args: argparse.Namespace) -> int:
    rep = Report("revert-check")
    baseline: list[str] = []
    bypass_before: str | None = None
    try:
        m = Manifest.load(resolve_run(args.run))
        baseline, bypass_before = list(m.settings_baseline), m.vscode_bypass_setting
    except SystemExit:
        rep.info("no manifest: every mcp__kiwrap rule counts as left over")
    lines = kiwrap_rule_lines(Path(w) for w in args.workspace or [])
    leftover = [line for line in lines if line not in baseline]
    rep.check(
        not leftover,
        "no temporary mcp__kiwrap allow rule remains" + "".join(f"\n      {x}" for x in leftover),
    )
    for line in baseline:
        rep.info(f"pre-existing (not ours): {line}")
    now = vscode_bypass_setting()
    rep.info(f"VS Code {VSCODE_BYPASS_SETTING} = {now!r} ({vscode_settings_path()})")
    if bypass_before is not None or now is not None:
        rep.check(
            now == bypass_before,
            f"{VSCODE_BYPASS_SETTING} is back to its prepare-time value {bypass_before!r}",
        )
    return rep.finish()


# ---- entry point -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="copy the demo, pin a newer KiCad, write the manifest, print the checklist"
    )
    prep.add_argument(
        "--source", help="install spec whose demo is copied (default: newest older-format install)"
    )
    prep.add_argument(
        "--target", help="install spec to pin (default: newest install writing the newest format)"
    )
    prep.add_argument(
        "--storage-home", help="kiwrap storage home (default: the platform's, as the panel uses)"
    )
    for name, helptext in (
        ("verify-deny", "after a denied card: nothing changed"),
        ("verify-approve", "after an approved card: exactly the listed files converted, consent recorded"),
        ("subagent", "Bypass / Task / workflow scenarios: a card appeared or the call was denied"),
        ("headless", "print the headless command, or check its --result"),
        ("revert-check", "no temporary allow rule left; the VS Code bypass setting restored"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--run", help="run directory (default: the latest prepare)")
        if name == "verify-deny":
            sp.add_argument(
                "--design-only", action="store_true", help="report, not fail, state.json / HEAD changes"
            )
        if name == "subagent":
            sp.add_argument("--scenario", choices=["task", "workflow", "bypass"], required=True)
            sp.add_argument(
                "--card", choices=["yes", "no"], required=True, help="did a permission card appear?"
            )
        if name == "headless":
            sp.add_argument("--result", help="the JSON the printed command wrote")
        if name == "revert-check":
            sp.add_argument("--workspace", action="append", help="another workspace whose .claude/ to search")
    return p


COMMANDS = {
    "prepare": cmd_prepare,
    "verify-deny": cmd_verify_deny,
    "verify-approve": cmd_verify_approve,
    "subagent": cmd_subagent,
    "headless": cmd_headless,
    "revert-check": cmd_revert_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
