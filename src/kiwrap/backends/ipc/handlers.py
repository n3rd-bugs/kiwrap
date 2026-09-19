"""T5: enumerate every IPC command message the client knows and ask a headless server about each.

`command_messages()` lists the command messages of the installed kicad-python protos by rule, not
by hand: a message defined in a `*_commands.proto` / `*_jobs.proto` file (the files KiCad's
`api/proto` tree uses for requests), whose name does not end in `Response`, and which no other
message in the whole descriptor pool uses as a field type (sub-messages such as `SaveOptions`,
`ItemHeader`, `BoardPlotSettings` are referenced; commands are only ever packed into the
`ApiRequest.message` Any). 129 messages on kicad-python f79cebd; the one known false positive is
`BoardLayers` (a response-shaped message nothing references), which the snapshot simply records as
unhandled.

`probe()` sends a minimal instance of each command to a server that has the ecc83 board or
schematic open. "Minimal" = an empty message plus, when the message has one, its document field
(`document` / `board` / `header.document` / `job_settings.document` set to the open document) so
the answer distinguishes "handled" from "no document" rejections (`_minimal_instance` explains
why the nested job document matters). Replies are classified:
    handled      AS_OK
    bad_request  AS_BAD_REQUEST / AS_UNIMPLEMENTED / AS_BUSY (a handler ran and refused the input)
    unhandled    AS_UNHANDLED ("no handler available for request of type ...")
    crash        the server process died while answering
    timeout      no reply within the deadline (server alive but wedged)
    error        anything else (connection loss without exit, unpack failures)
Read-only commands share one session per document type; commands that may mutate or close state
(names starting with a verb in `_STATEFUL_PREFIXES`) each get a fresh server so a crash or wedge
is attributed exactly and cannot poison later answers. The result is written by
conformance/tests/t5_ipc_handlers.py to conformance/snapshots/ipc-handlers/<version>.json and
compared on later runs (drift canary, ARCHITECTURE §6 T5/T9).

10.x is NOT probed here: it has no headless server; its handlers need a GUI session (Phase 3).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import pkgutil
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.backends.ipc import ensure_kipy_importable
from kiwrap.backends.ipc.sessions import HeadlessSession, SessionStartError
from kiwrap.platform import current

__all__ = ["CommandInfo", "ProbeResult", "Snapshot", "command_messages", "compare", "probe"]

_COMMAND_FILE_RE = re.compile(r"(_commands|_jobs)\.proto$")
_STATEFUL_PREFIXES = (
    "Set",
    "Update",
    "Add",
    "Delete",
    "Create",
    "Save",
    "Revert",
    "Close",
    "Open",
    "Run",
    "Refill",
    "Import",
    "Place",
    "Flip",
    "Interactive",
    "Inject",
    "Reload",
    "Load",
    "Rename",
    "Copy",
    "Parse",
    "Clear",
    "Remove",
    "Begin",
    "End",
    "Refresh",
    "Sync",
    "Highlight",
    "Focus",
    "CrossProbe",
)


@dataclass(frozen=True)
class CommandInfo:
    name: str  # message name, e.g. GetNets
    full_name: str  # kiapi.board.commands.GetNets
    proto_file: str  # board/board_commands.proto
    module: str  # kipy.proto.board.board_commands_pb2
    stateful: bool

    def to_json(self) -> dict[str, Any]:
        return {"full_name": self.full_name, "proto_file": self.proto_file, "stateful": self.stateful}


@dataclass(frozen=True)
class ProbeResult:
    command: str
    document: str  # "pcb" | "schematic"
    outcome: str
    status: str = ""  # ApiStatusCode name
    message: str = ""
    elapsed_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "status": self.status,
            "message": self.message[:300],
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class Snapshot:
    version: str
    client: str
    commands: dict[str, CommandInfo]
    results: dict[str, dict[str, ProbeResult]] = field(default_factory=dict)  # command -> doc -> result
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "kicad": self.version,
            "client": self.client,
            "notes": self.notes,
            "summary": self.summary(),
            "commands": {
                name: {
                    **info.to_json(),
                    "results": {d: r.to_json() for d, r in self.results.get(name, {}).items()},
                }
                for name, info in sorted(self.commands.items())
            },
        }

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for per_doc in self.results.values():
            for doc, r in per_doc.items():
                out.setdefault(doc, {})
                out[doc][r.outcome] = out[doc].get(r.outcome, 0) + 1
        return out

    def outcome(self, command: str, document: str) -> str | None:
        r = self.results.get(command, {}).get(document)
        return r.outcome if r else None

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return path


def _proto_modules() -> list[Any]:
    ensure_kipy_importable()
    import kipy.proto

    modules = []
    for info in pkgutil.walk_packages(kipy.proto.__path__, "kipy.proto."):
        if info.name.endswith("_pb2"):
            modules.append(importlib.import_module(info.name))
    return modules


def command_messages() -> dict[str, CommandInfo]:
    """The command inventory (see the module docstring for the rule)."""
    modules = _proto_modules()
    referenced: set[str] = set()
    all_messages: list[tuple[Any, Any]] = []  # (module, descriptor)

    def visit(desc: Any) -> None:
        for f in desc.fields:
            if f.message_type is not None:
                referenced.add(f.message_type.full_name)
        for nested in desc.nested_types:
            visit(nested)

    for mod in modules:
        for desc in mod.DESCRIPTOR.message_types_by_name.values():
            visit(desc)
            all_messages.append((mod, desc))
    commands: dict[str, CommandInfo] = {}
    for mod, desc in all_messages:
        if not _COMMAND_FILE_RE.search(desc.file.name) or desc.name.endswith("Response"):
            continue
        if desc.full_name in referenced:
            continue
        commands[desc.name] = CommandInfo(
            name=desc.name,
            full_name=desc.full_name,
            proto_file=desc.file.name,
            module=mod.__name__,
            stateful=desc.name.startswith(_STATEFUL_PREFIXES),
        )
    return commands


def _nested_document_field(desc: Any) -> str | None:
    """The name of a `DocumentSpecifier` field one level down (`ItemHeader.document`,
    `RunJobSettings.document`), or None."""
    for f in desc.fields:
        if f.message_type is not None and f.message_type.name == "DocumentSpecifier":
            return str(f.name)
    return None


def _minimal_instance(info: CommandInfo, document: Any) -> Any:
    """An empty message with its document field(s) pointed at the open document.

    "Document field" means a top-level `DocumentSpecifier` (`document` / `board` / `schematic`)
    or one nested one level down in a wrapper message: `ItemHeader.document` of the item commands
    and `RunJobSettings.document` of the 22 `RunBoardJobExport*` / `RunSchematicJobExport*`
    commands. The nested job document was not filled by the first snapshot (2026-09-19), so every
    job command read `unhandled`: `API_HANDLER_EDITOR::validateDocument` answers AS_UNHANDLED to an
    empty specifier (type DOCTYPE_UNKNOWN) and the server reports "no handler available" -
    the handlers themselves are registered on the headless server (docs/UPSTREAM.md G6 was a
    probe artifact; verified on 10.99.0-4083: RunBoardJobExportStats with the nested document
    set answers AS_OK / JS_SUCCESS in 0.02 s). Nothing else is set: an empty output path makes
    a job write its default file name into the server's working directory (the sandbox root's
    temp dir), and unknown enum values are the handler's problem to validate.
    """
    mod = importlib.import_module(info.module)
    msg = getattr(mod, info.name)()
    for f in msg.DESCRIPTOR.fields:
        if f.message_type is None:
            continue
        if f.message_type.name == "DocumentSpecifier":
            getattr(msg, f.name).CopyFrom(document)
        elif (nested := _nested_document_field(f.message_type)) is not None:
            getattr(getattr(msg, f.name), nested).CopyFrom(document)
    return msg


def _send(session: HeadlessSession, info: CommandInfo, document: Any, doc_kind: str) -> ProbeResult:
    from google.protobuf.empty_pb2 import Empty
    from kipy.errors import ApiError, ConnectionError
    from kipy.proto.common import ApiStatusCode

    msg = _minimal_instance(info, document)
    t0 = time.monotonic()
    try:
        session.client().send(msg, Empty)
        return ProbeResult(info.name, doc_kind, "handled", "AS_OK", "", int((time.monotonic() - t0) * 1000))
    except ApiError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        status = ApiStatusCode.Name(e.code) if e.code in ApiStatusCode.values() else str(e.code)
        text = e.raw_message or str(e)
        # kipy raises ApiError for an AS_OK reply whose payload is not Empty: still a handled reply.
        if e.code == ApiStatusCode.AS_OK or text.startswith("Failed to unpack"):
            return ProbeResult(info.name, doc_kind, "handled", "AS_OK", text, elapsed)
        if e.code == ApiStatusCode.AS_UNHANDLED:
            return ProbeResult(info.name, doc_kind, "unhandled", status, text, elapsed)
        return ProbeResult(info.name, doc_kind, "bad_request", status, text, elapsed)
    except ConnectionError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        time.sleep(0.2)
        if not session.running:
            return ProbeResult(info.name, doc_kind, "crash", "", session.transcript()[-500:], elapsed)
        outcome = "timeout" if "imed out" in str(e) else "error"
        return ProbeResult(info.name, doc_kind, outcome, "", str(e), elapsed)
    except Exception as e:  # pragma: no cover - unexpected client failure
        return ProbeResult(info.name, doc_kind, "error", "", repr(e), int((time.monotonic() - t0) * 1000))


def _document_of(session: HeadlessSession, doc_kind: str) -> Any:
    return session.board().document if doc_kind == "pcb" else session.schematic().document


def _job_outputs(config_root: Any) -> tuple[Path, set[str]]:
    """The api-server's working directory (the sandbox root's temp dir, `HeadlessSession.start`)
    and the names already in it. A job command probed with an empty output path writes its
    default file name there (`ecc83-pp.svg`, `ecc83-pp-odb.zip`, ...); `probe()` removes what
    it created so the drift canary leaves no exports behind."""
    cwd = config_root.temp_dir(current())
    try:
        return cwd, {entry.name for entry in cwd.iterdir()}
    except OSError:
        return cwd, set()


def _remove_new_files(cwd: Path, before: set[str]) -> None:
    try:
        entries = list(cwd.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name not in before and entry.is_file():
            with contextlib.suppress(OSError):
                entry.unlink()


def probe(
    install: Any,
    board_file: Path,
    schematic_file: Path,
    *,
    config_root: Any = None,
    request_timeout_ms: int = 8000,
    commands: dict[str, CommandInfo] | None = None,
    stateful_fresh_session: bool = True,
) -> Snapshot:
    """Run the handler probe against scratch copies of a board and a schematic (never originals)."""
    from kiwrap.backends.ipc import client_version
    from kiwrap.sandbox import ensure

    config_root = config_root if config_root is not None else ensure(install)
    inventory = commands if commands is not None else command_messages()
    snap = Snapshot(version=install.version, client=client_version(), commands=inventory)
    snap.notes.append(
        "minimal instance = empty message + document fields (top-level, ItemHeader.document, "
        "RunJobSettings.document) set to the open document; stateful commands run in a fresh server each"
    )
    for doc_kind, file in (("pcb", board_file), ("schematic", schematic_file)):
        shared: HeadlessSession | None = None

        def open_shared(path: Path = file) -> HeadlessSession:
            s = HeadlessSession(install, path, config_root=config_root, request_timeout_ms=request_timeout_ms)
            return s.start()

        cwd, before = _job_outputs(config_root)
        try:
            shared = open_shared()
            for name, info in sorted(inventory.items()):
                if info.stateful and stateful_fresh_session:
                    try:
                        with HeadlessSession(
                            install, file, config_root=config_root, request_timeout_ms=request_timeout_ms
                        ) as fresh:
                            result = _send(fresh, info, _document_of(fresh, doc_kind), doc_kind)
                    except SessionStartError as e:
                        result = ProbeResult(name, doc_kind, "error", "", f"server did not start: {e}")
                else:
                    if shared is None or not shared.running:
                        with contextlib.suppress(Exception):
                            if shared is not None:
                                shared.close()
                        shared = open_shared()
                    result = _send(shared, info, _document_of(shared, doc_kind), doc_kind)
                    if result.outcome in ("crash", "timeout", "error"):
                        with contextlib.suppress(Exception):
                            shared.close()
                        shared = None
                # Keep the snapshot machine-independent: scrub the scratch project path.
                if str(file.parent) in result.message:
                    result = ProbeResult(
                        result.command,
                        result.document,
                        result.outcome,
                        result.status,
                        result.message.replace(str(file.parent), "<project>"),
                        result.elapsed_ms,
                    )
                snap.results.setdefault(name, {})[doc_kind] = result
        finally:
            if shared is not None:
                shared.close()
            _remove_new_files(cwd, before)
    return snap


def compare(previous: dict[str, Any], current: Snapshot) -> list[str]:
    """Human-readable drift lines between a stored snapshot (JSON) and a fresh one; empty = same."""
    lines: list[str] = []
    old_cmds = previous.get("commands", {})
    for name in sorted(set(old_cmds) | set(current.commands)):
        if name not in old_cmds:
            lines.append(f"new command: {name}")
            continue
        if name not in current.commands:
            lines.append(f"command gone: {name}")
            continue
        for doc in ("pcb", "schematic"):
            before = old_cmds[name].get("results", {}).get(doc, {}).get("outcome")
            after = current.outcome(name, doc)
            if before != after:
                lines.append(f"{name} [{doc}]: {before} -> {after}")
    return lines
