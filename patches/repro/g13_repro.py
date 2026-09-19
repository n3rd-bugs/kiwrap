"""Minimal, deterministic reproduction of the headless UpdateItems hang (kiwrap gap G13).

Sequence, against `kicad-cli api-server` with the ecc83 demo schematic open (U1 is the
multi-unit ECC83 tube, units 1-3):

    1. GetItems(SchematicSymbol)      -> all symbols
    2. UpdateItems(U1 unit 1)         -> AS_OK (the item is sent back unchanged)
    3. UpdateItems(U1 unit 2)         -> NEVER ANSWERED; the server process stays alive and no
                                         longer answers Ping from a fresh client either

Updating the same unit twice, or two different single-unit symbols, is fine. Only kicad-python
(kipy) is used; the environment for kicad-cli is passed through KiCad(env=...) when this kipy
supports it, else set in os.environ (KICAD_CONFIG_HOME etc.) so the user's settings are untouched.

Usage: python g13_repro.py [--kicad-cli PATH] [--schematic PATH] [--timeout-ms 5000] [--work DIR]
Exit code 0 = hang reproduced, 1 = second UpdateItems answered (bug absent), 2 = other failure.
"""

from __future__ import annotations

import argparse
import inspect
import os
import pathlib
import queue
import shutil
import sys
import tempfile
import time


def _drain(q: queue.Queue[str]) -> list[str]:
    lines: list[str] = []
    while True:
        try:
            lines.append(q.get_nowait())
        except queue.Empty:
            return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kicad-cli", default=os.environ.get("KICAD_CLI") or None)
    ap.add_argument(
        "--schematic",
        default=None,
        help="a .kicad_sch with a multi-unit symbol (default: the ecc83 demo next to kicad-cli)",
    )
    ap.add_argument("--timeout-ms", type=int, default=5000)
    ap.add_argument("--work", default=None, help="scratch dir (default: a temp dir)")
    ap.add_argument(
        "--keep-server",
        action="store_true",
        help="leave the wedged server running (to attach a debugger) and print its PID",
    )
    args = ap.parse_args()

    from google.protobuf.empty_pb2 import Empty
    from kipy import KiCad
    from kipy.client import KiCadClient
    from kipy.proto.common import commands
    from kipy.server import find_kicad_cli

    cli = find_kicad_cli(args.kicad_cli)
    work = pathlib.Path(args.work or tempfile.mkdtemp(prefix="g13-"))
    cfg, tmp, docs = work / "cfg", work / "tmp", work / "docs"
    for d in (cfg, tmp, docs):
        d.mkdir(parents=True, exist_ok=True)
    (cfg / "10.99").mkdir(exist_ok=True)
    (cfg / "10.99" / "kicad_advanced").write_text("EnableAPILogging=1\n", encoding="utf-8")
    # Private settings/documents/cache so the run never touches the user's KiCad configuration.
    overrides = {
        "KICAD_CONFIG_HOME": str(cfg),
        "KICAD_DOCUMENTS_HOME": str(docs),
        "KICAD_CACHE_HOME": str(work / "cache"),
    }
    if args.schematic:
        src = pathlib.Path(args.schematic)
        project = work / "project"
        project.mkdir(exist_ok=True)
        for p in src.parent.iterdir():
            if p.is_file():
                shutil.copy(p, project / p.name)
        sch_file = project / src.name
    else:
        demo = pathlib.Path(cli).parent.parent / "share" / "kicad" / "demos" / "ecc83"
        shutil.copytree(demo, work / "ecc83", dirs_exist_ok=True)
        sch_file = work / "ecc83" / "ecc83-pp.kicad_sch"

    kw = dict(headless=True, kicad_cli_path=cli, file_path=str(sch_file), timeout_ms=args.timeout_ms)
    if "env" in inspect.signature(KiCad.__init__).parameters:
        # A short TEMP for the server only; the client's socket path stays under the caller's TEMP.
        kw["env"] = {**os.environ, **overrides, "TEMP": str(tmp), "TMP": str(tmp)}
    else:
        os.environ.update(overrides)
    print(f"kicad-cli: {cli}\nschematic: {sch_file}\ntimeout: {args.timeout_ms} ms")
    k = KiCad(**kw)
    server = k._server
    print(f"server pid {server._process.pid}, version {k.get_version().full_version}")
    sch = k.get_schematic()
    symbols = sch.get_symbols()
    multi = [s for s in symbols if s.unit > 1]
    if not multi:
        print("no multi-unit symbol in this schematic")
        return 2
    ref = multi[0].reference_field.text.value
    units = sorted((s for s in symbols if s.reference_field.text.value == ref), key=lambda s: s.unit)
    print(f"multi-unit symbol {ref}: units {[s.unit for s in units]}")
    a, b = units[0], units[1]
    reproduced = False
    try:
        for label, item in (
            (f"{ref} unit {a.unit}", a),
            (f"{ref} unit {a.unit} again", a),
            (f"{ref} unit {b.unit}", b),
        ):
            t0 = time.monotonic()
            try:
                sch.update_items(item)
                print(f"UpdateItems({label}): ok in {time.monotonic() - t0:.2f} s")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"UpdateItems({label}): NO REPLY after {time.monotonic() - t0:.1f} s ({type(exc).__name__}: {str(exc)[:80]})"
                )
                reproduced = True
                break
        alive = server._process.poll() is None
        print(f"server process alive: {alive}")
        if reproduced and alive:
            probe = KiCadClient(server.socket_url, "g13.probe", "", 2000)
            t0 = time.monotonic()
            try:
                probe.send(commands.Ping(), Empty)
                print(f"Ping from a fresh client: answered in {time.monotonic() - t0:.2f} s")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"Ping from a fresh client: no reply after {time.monotonic() - t0:.1f} s ({type(exc).__name__})"
                )
            finally:
                probe.close()
    finally:
        for line in _drain(server._stderr_lines):
            print("server stderr:", line)
        api_log = docs / "KiCad" / "10.99" / "logs" / "api.log"
        for candidate in list(docs.rglob("api.log")) + list(cfg.rglob("api.log")):
            api_log = candidate
        if api_log.exists():
            print(f"api.log ({api_log}), last lines:")
            for line in api_log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
                print("  ", line)
        else:
            print("no api.log written")
        if args.keep_server and reproduced:
            print(f"leaving the server running for a debugger: pid {server._process.pid}")
            k._client.close()
            k._server = None
        else:
            k.close()
    return 0 if reproduced else 1


if __name__ == "__main__":
    sys.exit(main())
