"""Tripwire over the installed Claude Code bundle: the client-side half of the D24 consent gate.

docs/DECISIONS.md D24 makes `kw_project_upgrade` the only format conversion and relies on Claude
Code, not kiwrap, to ask the user: the tool is published with
`_meta["anthropic/requiresUserInteraction"]: true`. docs/VERIFICATION.md claim 13 records what that
flag does inside the bundle (read statically from claude.exe 2.1.280 / 2.1.281 / 2.1.282). This
module re-checks it on every local and D14 self-hosted run:

* **Literal anchors FAIL when they disappear** (`LITERAL_ANCHORS`): the `_meta` key, the gate's
  `reason:"requiresUserInteraction"`, the `--permission-prompt-tool` deny message and the client
  name `claude-code` that `consent_source` records. A missing literal means the gate was renamed or
  removed and D24 must be revisited before the next release.
* **Structural checks only WARN and SKIP** (`panel_drift`): the VS Code panel's `spawnClaude`
  passing `onElicitation` (then kiwrap's claude-vscode elicitation skip should be revisited) and
  `requestToolPermission` auto-allowing anything besides `mcp__claude-in-chrome__` (then the panel
  gate should be revisited). Minified code moves between releases, so these can never fail a run.
* No bundle -> the `claude_code` tests skip. The bundle is the running one (`CLAUDE_CODE_EXECPATH`),
  else the newest `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe`;
  it is read with `mmap` (about 240 MB, never loaded whole). The panel host is `extension.js` two
  directories above the binary.

The helper tests at the bottom run everywhere: they exercise the detection logic on synthetic
bundles, and check that kiwrap's own agent-facing docs still teach the D24 route.
"""

from __future__ import annotations

import mmap
import os
import re
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

#: Literals whose disappearance fails the run (D24 "tests/test_claude_code_bundle.py"; claim 13 items 1, 2, 5, 6)
LITERAL_ANCHORS: tuple[bytes, ...] = (
    b"anthropic/requiresUserInteraction",
    b'reason:"requiresUserInteraction"',
    b"MCP tool requires user interaction; not supported via --permission-prompt-tool",
    b'name:"claude-code"',
)

#: The only tool prefix the panel may auto-allow (claim 13 item 7)
PANEL_AUTO_ALLOW = "mcp__claude-in-chrome__"

_BUNDLE_GLOB = ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/"
_BINARY_NAMES = ("claude.exe", "claude")
_VERSION_RE = re.compile(r"anthropic\.claude-code-(\d+)\.(\d+)\.(\d+)")
#: A method body is never this long; past it the scanner gives up (structure changed -> warn + skip)
_MAX_BODY_CHARS = 200_000


class ClaudeCodeBundleDrift(UserWarning):
    """The panel's structure moved away from what D24 / claim 13 assumed; revisit, nothing broke."""


@dataclass(frozen=True)
class Bundle:
    """One installed Claude Code bundle: the native binary and its VS Code host script."""

    binary: Path
    version: tuple[int, ...] | None

    @property
    def extension_js(self) -> Path:
        # <ext>/resources/native-binary/claude.exe -> <ext>/extension.js
        return self.binary.parents[2] / "extension.js"

    @property
    def label(self) -> str:
        return ".".join(map(str, self.version)) if self.version else str(self.binary)


def bundle_version(path: Path) -> tuple[int, ...] | None:
    """(major, minor, patch) from the extension directory name, or None when the path has none."""
    m = _VERSION_RE.search(path.as_posix())
    return tuple(int(x) for x in m.groups()) if m else None


def find_bundle(home: Path | None = None, env: dict[str, str] | None = None) -> Bundle | None:
    """The running bundle (`CLAUDE_CODE_EXECPATH`), else the newest one under `~/.vscode/extensions`."""
    environ = os.environ if env is None else env
    running = environ.get("CLAUDE_CODE_EXECPATH")
    if running and Path(running).is_file():
        return Bundle(Path(running), bundle_version(Path(running)))
    root = Path.home() if home is None else home
    found = [p for name in _BINARY_NAMES for p in root.glob(_BUNDLE_GLOB + name) if p.is_file()]
    if not found:
        return None
    newest = max(found, key=lambda p: bundle_version(p) or (-1,))
    return Bundle(newest, bundle_version(newest))


def missing_anchors(data: bytes | mmap.mmap, anchors: tuple[bytes, ...] = LITERAL_ANCHORS) -> list[str]:
    """The anchors not found anywhere in `data`."""
    return [a.decode() for a in anchors if data.find(a) == -1]


def method_body(source: str, signature: re.Pattern[str]) -> str | None:
    """The brace-balanced body of the first method matching `signature` (which must end at its
    opening `{`). Skips string and template literals; None when the method is not found or the body
    does not close within `_MAX_BODY_CHARS` (regex literals with braces can confuse it - callers
    treat None as 'structure changed')."""
    m = signature.search(source)
    if m is None:
        return None
    i, depth, end = m.end(), 1, min(len(source), m.end() + _MAX_BODY_CHARS)
    stack: list[str] = []  # open template literals / `${` expressions
    while i < end:
        c = source[i]
        if stack and stack[-1] == "`":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
            elif source.startswith("${", i):
                stack.append("${")
                i += 2
                continue
            i += 1
            continue
        if c in "\"'":
            j = i + 1
            while j < end and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            i = j + 1
            continue
        if c == "`":
            stack.append("`")
        elif c == "{":
            if stack:
                stack.append("{")
            else:
                depth += 1
        elif c == "}":
            if stack and stack[-1] in ("${", "{"):
                stack.pop()
            else:
                depth -= 1
                if depth == 0:
                    return source[m.end() : i]
        i += 1
    return None


_SPAWN_SIG = re.compile(r"async spawnClaude\([^)]*\)\{")
_PERMISSION_SIG = re.compile(r"async requestToolPermission\([^)]*\)\{")


def panel_drift(extension_js: str) -> list[str]:
    """Structural findings about the VS Code panel host that D24 depends on (claim 13 item 7).
    Each finding is a reason to revisit a decision, never a failure."""
    findings: list[str] = []
    spawn = method_body(extension_js, _SPAWN_SIG)
    if spawn is None or "canUseTool" not in spawn:
        findings.append(
            "spawnClaude(...) with its SDK options was not found in extension.js: the panel changed shape; "
            "revisit the claude-vscode elicitation skip (D24) by reading the new bundle"
        )
    elif "onElicitation" in spawn:
        findings.append(
            "spawnClaude now passes onElicitation: the panel may show MCP elicitation; revisit the "
            "claude-vscode elicitation skip (D24, ANALYSIS-TOOLING §6)"
        )
    permission = method_body(extension_js, _PERMISSION_SIG)
    head = None if permission is None else permission.split("sendRequest(", 1)[0]
    if head is None or "sendRequest(" not in (permission or ""):
        findings.append(
            "requestToolPermission(...) forwarding to the webview card was not found: revisit the panel gate (D24)"
        )
    else:
        allows = head.count('behavior:"allow"')
        if allows != 1 or PANEL_AUTO_ALLOW not in head:
            findings.append(
                f"requestToolPermission auto-allows something besides {PANEL_AUTO_ALLOW}* "
                f"({allows} allow branch(es) before the card): revisit the panel gate (D24)"
            )
    return findings


def warn_and_skip(findings: list[str]) -> None:
    for text in findings:
        warnings.warn(text, ClaudeCodeBundleDrift, stacklevel=2)
    if findings:
        pytest.skip("; ".join(findings))


# ---- the installed bundle (marker claude_code) -------------------------------------------------------


@pytest.fixture(scope="module")
def bundle() -> Bundle:
    found = find_bundle()
    if found is None:
        pytest.skip(
            "no Claude Code bundle (CLAUDE_CODE_EXECPATH or ~/.vscode/extensions/anthropic.claude-code-*)"
        )
    return found


@pytest.fixture(scope="module")
def binary(bundle: Bundle) -> Iterator[mmap.mmap]:
    with bundle.binary.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        yield mm


@pytest.mark.claude_code
@pytest.mark.parametrize("anchor", LITERAL_ANCHORS, ids=lambda a: a.decode()[:40])
def test_literal_anchor_present(bundle: Bundle, binary: mmap.mmap, anchor: bytes):
    """Claim 13 items 1, 2, 5, 6: the literal the D24 gate rests on is still in the bundle."""
    assert binary.find(anchor) != -1, (
        f"Claude Code {bundle.label} ({bundle.binary}) no longer contains {anchor.decode()!r}: the "
        "requiresUserInteraction gate changed; re-read the bundle and revisit docs/DECISIONS.md D24 / "
        "docs/VERIFICATION.md claim 13 before relying on kw_project_upgrade's permission card"
    )


@pytest.mark.claude_code
def test_panel_structure(bundle: Bundle):
    """Claim 13 item 7: the panel declines elicitation and auto-allows only claude-in-chrome.
    A change warns and skips (minified code moves); it never fails."""
    if not bundle.extension_js.is_file():
        pytest.skip(f"no extension.js next to {bundle.binary} (not a VS Code extension bundle)")
    text = bundle.extension_js.read_text(encoding="utf-8", errors="replace")
    warn_and_skip(panel_drift(text))


# ---- the detection logic on synthetic bundles (always run) -------------------------------------------

_GOOD_PANEL = (
    "class H{async spawnClaude($,J,Q){let q=1,M={cwd:Z,resume:J,canUseTool:Q,onUserDialog:U,model:X,"
    "stderr:(T)=>{log(`x ${T} {`)}};return run(M)}"
    'async requestToolPermission($,J,Q,X,Y,z){if(this.ok&&J.startsWith("mcp__claude-in-chrome__"))'
    'return{behavior:"allow",updatedInput:Q};let K=await this.sendRequest($,{type:"tool_permission_request"});'
    "return K.result}}"
)


def test_find_bundle_prefers_the_running_one(tmp_path: Path):
    ext = tmp_path / ".vscode/extensions"
    for v in ("2.1.280", "2.1.99", "2.1.281"):
        b = ext / f"anthropic.claude-code-{v}-win32-x64/resources/native-binary/claude.exe"
        b.parent.mkdir(parents=True)
        b.write_bytes(b"x")
    newest = find_bundle(home=tmp_path, env={})
    assert newest is not None and newest.version == (2, 1, 281)
    assert newest.extension_js.parent.name == "anthropic.claude-code-2.1.281-win32-x64"
    running = ext / "anthropic.claude-code-2.1.280-win32-x64/resources/native-binary/claude.exe"
    chosen = find_bundle(home=tmp_path, env={"CLAUDE_CODE_EXECPATH": str(running)})
    assert chosen is not None and chosen.binary == running and chosen.version == (2, 1, 280)
    # a stale CLAUDE_CODE_EXECPATH falls back to the newest; no bundle at all -> None
    stale = find_bundle(home=tmp_path, env={"CLAUDE_CODE_EXECPATH": str(tmp_path / "gone.exe")})
    assert stale is not None and stale.version == (2, 1, 281)
    assert find_bundle(home=tmp_path / "empty", env={}) is None


def test_missing_anchors_reads_through_mmap(tmp_path: Path):
    blob = tmp_path / "claude.exe"
    blob.write_bytes(b"\0" * 4096 + b" ".join(LITERAL_ANCHORS[:-1]) + b"\0")
    with blob.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        assert missing_anchors(mm) == ['name:"claude-code"']
    assert missing_anchors(b" ".join(LITERAL_ANCHORS)) == []


def test_panel_drift_detects_each_change():
    assert panel_drift(_GOOD_PANEL) == []
    with_elicitation = _GOOD_PANEL.replace("onUserDialog:U,", "onUserDialog:U,onElicitation:E,")
    assert any("onElicitation" in f for f in panel_drift(with_elicitation))
    extra_allow = _GOOD_PANEL.replace(
        "let K=await", 'if(J.startsWith("mcp__kiwrap__"))return{behavior:"allow",updatedInput:Q};let K=await'
    )
    assert any("auto-allows" in f for f in panel_drift(extra_allow))
    no_chrome = _GOOD_PANEL.replace("mcp__claude-in-chrome__", "mcp__other__")
    assert any("auto-allows" in f for f in panel_drift(no_chrome))
    renamed = _GOOD_PANEL.replace("spawnClaude", "startClaude").replace("requestToolPermission", "askTool")
    assert len(panel_drift(renamed)) == 2


def test_method_body_skips_strings_and_templates():
    src = 'x;async f(a){let s="}",t=`${a}}`,u=`a${`b${1}`}c`;{}return s}async g(){}'
    body = method_body(src, re.compile(r"async f\([^)]*\)\{"))
    assert body == 'let s="}",t=`${a}}`,u=`a${`b${1}`}c`;{}return s'
    assert method_body("async f(){ never closed", re.compile(r"async f\(\)\{")) is None
    assert method_body("nothing here", re.compile(r"async f\(\)\{")) is None


def test_structural_drift_warns_and_skips_never_fails():
    with pytest.warns(ClaudeCodeBundleDrift), pytest.raises(pytest.skip.Exception):
        warn_and_skip(["spawnClaude now passes onElicitation"])
    warn_and_skip([])  # nothing to report: returns normally


# ---- kiwrap's own docs teach the D24 route -----------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_AGENT_DOCS = (_REPO / "AGENTS.md", _REPO / ".claude/skills/kiwrap-flow/SKILL.md")


def test_help_pages_teach_the_permission_card_not_confirm():
    """D24: the only conversion is kw_project_upgrade, gated by the client; no help page teaches
    confirm=true, a dry-run upgrade or a KiCad-less shortcut."""
    from kiwrap.core.tools import help as help_tool

    pages = {
        "formats": help_tool.FORMATS_PAGE,
        "readonly": help_tool.READONLY_PAGE,
        **{k: v for k, v in help_tool.ERROR_PAGES.items()},
        **{k: v for k, v in help_tool.TOOL_PAGES.items()},
        "overview": help_tool.overview(),
    }
    for name, text in pages.items():
        assert "confirm=true" not in text, name
        assert "dry_run=true)`: it converts" not in text, name
        assert "action='upgrade', " not in text and 'action="upgrade", ' not in text, name
    upgrade = help_tool.TOOL_PAGES["kw_project_upgrade"]
    for needle in (
        LITERAL_ANCHORS[0].decode(),
        "from_kicad",
        "to_kicad",
        "files",
        "upgrade_args",
        "not_converted",
    ):
        assert needle in upgrade, needle
    for needle in (
        "client_permission_flow",
        "kiwrap_vscode_modal",
        "elicitation",
        "in_process",
        "unknown_client",
    ):
        assert needle in upgrade, needle
    assert "kw_project_upgrade(**data.upgrade_args)" in help_tool.ERROR_PAGES["E_FORMAT_UPGRADE_REQUIRED"]
    assert (
        "upgrade_preview" in help_tool.FORMATS_PAGE and LITERAL_ANCHORS[0].decode() in help_tool.FORMATS_PAGE
    )
    assert "upgrade_preview" in help_tool.READONLY_PAGE and "kw_project_upgrade" in help_tool.READONLY_PAGE


def test_help_upgrade_pages_use_placeholders_not_kicad_versions():
    """CLAUDE.md: no KiCad version numbers outside kiwrap.backends / kiwrap.data; the D24 pages
    speak in <from_kicad> / KiCad <x>."""
    from kiwrap.core.tools import help as help_tool

    version = re.compile(r"(?<![\w.])(?:[6-9]|1[0-9])\.[0-9]{1,2}(?:\.[0-9]+)?(?![\w.])")
    for text in (
        help_tool.FORMATS_PAGE,
        help_tool.READONLY_PAGE,
        help_tool.ERROR_PAGES["E_FORMAT_UPGRADE_REQUIRED"],
        help_tool.TOOL_PAGES["kw_project"],
        help_tool.TOOL_PAGES["kw_project_upgrade"],
    ):
        assert version.findall(text) == [], version.findall(text)


@pytest.mark.parametrize("doc", _AGENT_DOCS, ids=lambda p: p.name)
def test_agent_docs_teach_kw_project_upgrade(doc: Path):
    text = doc.read_text(encoding="utf-8")
    assert "confirm=true" not in text
    assert "kw_project_upgrade(**data.upgrade_args)" in text
    assert "upgrade_preview" in text
