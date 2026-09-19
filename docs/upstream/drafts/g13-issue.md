# Issue draft: headless api-server stops answering after UpdateItems on two units of one symbol (G13; NOT posted, 2026-09-19)

Target: `gitlab.com/kicad/code/kicad` issues, labels `api`, `eeschema`, `connectivity`. Repro
script: [`patches/repro/g13_repro.py`](../../../patches/repro/g13_repro.py) (kicad-python only,
no kiwrap). Verified on the stock nightly and on both kiwrap bench builds of the same commit
(RelWithDebInfo and Debug); the Debug build prints the exception, the stack below is from cdb
attached to the Debug server. No MR from us unless assigned: the fix sits in the schematic API
serialisation / new connectivity engine (Jon / Seth territory).

---

## Issue text

Title: `kicad-cli api-server: UpdateItems on a second unit of a multi-unit symbol is never answered and the server stops responding`

### Description

With the headless server (`kicad-cli api-server`) and a schematic that contains a multi-unit
symbol, sending `UpdateItems` for one unit of the symbol and then `UpdateItems` for another
unit of the same symbol makes the second request time out. The process stays alive but no later
request (not even `Ping` from a new client) is answered; the only way out is to kill it.

Updating the same unit twice, or two different single-unit symbols, works. The item does not
need to change: sending back exactly what `GetItems` returned is enough.

### Steps to reproduce

With kicad-python `main` (f79cebd1) and the `ecc83` demo (U1 is the ECC83 dual triode, units
1-3):

```python
from kipy import KiCad
k = KiCad(headless=True, kicad_cli_path=r"C:\Program Files\KiCad\10.99\bin\kicad-cli.exe",
          file_path=r"...\ecc83\ecc83-pp.kicad_sch", timeout_ms=5000)
sch = k.get_schematic()
u1 = sorted((s for s in sch.get_symbols() if s.reference_field.text.value == "U1"), key=lambda s: s.unit)
sch.update_items(u1[0])   # unit 1: OK
sch.update_items(u1[0])   # unit 1 again: OK
sch.update_items(u1[1])   # unit 2: ConnectionError: Error receiving reply from KiCad: Timed out
```

The attached `g13_repro.py` does this with a private `KICAD_CONFIG_HOME`, then shows that a
fresh client's `Ping` is not answered either and prints the server's stderr and the tail of
`api.log` (`EnableAPILogging=1`). Output on the nightly:

```
server pid 84668, version 10.99.0-4083-g6dd893f7d1
multi-unit symbol U1: units [1, 2, 3]
UpdateItems(U1 unit 1): ok in 0.01 s
UpdateItems(U1 unit 1 again): ok in 0.01 s
UpdateItems(U1 unit 2): NO REPLY after 5.0 s (ConnectionError: Error receiving reply from KiCad: Timed out)
server process alive: True
Ping from a fresh client: no reply after 2.0 s (ConnectionError)
```

`api.log` shows the third `Request: ... UpdateItems` with no `Response:` after it, and nothing
for the later `Ping`.

### What happens

On a Debug build of the same commit the server prints

```
Error: Unhandled exception class: class std::runtime_error  what: Duplicate connectivity source identity
```

The throw is `SCH_CONNECTIVITY::ExtractScreenFacts` (`eeschema/connectivity/conn_facts.cpp:495`,
reached from line 593, the per-pin `checkIdentity( pin->m_Uuid )` of the symbol being updated),
called from the commit the handler pushes:

```
_eeschema!`SCH_CONNECTIVITY::ExtractScreenFacts'::`2'::<lambda_3>::operator()   conn_facts.cpp @ 495
_eeschema!SCH_CONNECTIVITY::ExtractScreenFacts                                  conn_facts.cpp @ 593
_eeschema!SCH_CONNECTIVITY::INPUT_STORE::Screen
_eeschema!SCH_CONNECTIVITY::INPUT_STORE::Instance
_eeschema!SCH_CONNECTIVITY::INPUT_STORE::Capture
_eeschema!SCH_CONNECTIVITY::ENGINE::Update
_eeschema!SCH_CONNECTIVITY::FACADE::Update
_eeschema!SCH_CONNECTIVITY::FACADE::updateModel
_eeschema!SCH_CONNECTIVITY::FACADE::Recalculate
_eeschema!SCHEMATIC::RecalculateConnections
_eeschema!SCH_COMMIT::pushSchEdit
_eeschema!SCH_COMMIT::Push
_eeschema!API_HANDLER_EDITOR::pushCurrentCommit
_eeschema!API_HANDLER_SCH::handleCreateUpdateItemsInternal
_eeschema!API_HANDLER_EDITOR::handleUpdateItems
kicommon!API_HANDLER::Handle
kicommon!KICAD_API_SERVER::handleApiRequestString
kicommon!KICAD_API_SERVER::handleApiEvent
wxbase332ud!wxEvtHandler::ProcessPendingEvents
kicad_cli!CLI::API_SERVER_COMMAND::doPerform
```

Two things combine:

1. **Duplicate pin identities after a round trip.** `PackSymbol` (`eeschema/api/api_sch_utils.cpp`,
   "Also include the pins from non-placed units") fills `definition.items` with the instance's
   own pins for the placed unit and, for the other units, with the *library symbol's* pins
   (`lib->GetPins()`, serialised with their own `m_Uuid`). Those library pin ids are the same for
   every instance of the symbol (each instance's `LIB_SYMBOL` is a copy, and copies keep the pin
   KIIDs). `GetItems` on ecc83 already shows it: the three U1 instances report 27 pins with only
   18 distinct ids; every pair of instances shares the 3 pins of the unit neither of them places.
   On `UpdateItems`, `SCH_SYMBOL::Deserialize` (`sch_symbol.cpp`, "make sure that the UUIDs of
   the instance pins round-trip") assigns each definition pin's id to the instance's `SCH_PIN`
   of that number, so after updating unit 1 its pins for units 2 and 3 carry the library ids;
   after updating unit 2 the same ids exist on a second `SCH_PIN` in the screen. The debugger
   confirms it: the id that throws (`b1b9818b-c489-4dd0-9b82-4d4138cecbe8` in one run) is pin
   "4" (unit 3) and appears in the `GetItems` response of both U1 unit 1 and U1 unit 2.
2. **An exception in a handler is a permanent hang.** `KICAD_API_SERVER::handleApiRequestString`
   does not catch what `handler->Handle()` throws. wx swallows the exception on the way out of
   `ProcessPendingEvents` (release build: silently; Debug: the line above), so `Reply()` is
   never called, and `KINNG_REQUEST_SERVER::listenThread` sits in `m_replyReady.wait(...)`
   forever (`libs/kinng/src/kinng.cpp`); it never calls `nng_recv` again, so every later
   request of every client is dropped. Thread dump of the wedged server: main thread idle in
   `doPerform`'s `wxMilliSleep` loop, the kinng thread in `condition_variable::wait`, nothing
   inside a handler.

So the headless server is lost for any handler that throws, and the schematic pin serialisation
gives the connectivity engine a reason to throw as soon as two instances of one multi-unit
symbol have been sent back. The same handler runs in the GUI; I have not tried what a running
eeschema does with the exception.

### Expected

`UpdateItems` answers for every unit (the item is unchanged, so `IRS_OK`), and a handler that
fails answers with an error status instead of taking the server down. Probably: `PackSymbol`
should not emit library pin ids for the non-placed units (the instance's `GetRawPins()` already
holds one `SCH_PIN` per library pin with instance-unique ids), or `Deserialize` should not adopt
ids for pins the instance does not place; and `handleApiRequestString` could wrap `Handle()` in
a try/catch that replies `AS_BAD_REQUEST` (or a new status) with `e.what()`.

### Version

```
Application: kicad-cli x64 on x64
Version: 10.99.0-4083-g6dd893f7d1, release build
Libraries: wxWidgets 3.3.2, FreeType 2.14.3, HarfBuzz 14.3.1, FontConfig 2.17.1, libcurl/8.21.0 Schannel zlib/1.3.2
Platform: Windows 11 (build 26200), 64-bit edition, 64 bit, Little endian, wxBase
Build Info: Date: Sep 18 2026 08:52:50, wxWidgets 3.3.2 (wchar_t,STL containers), Boost 1.92.0, OCC 7.9.2, Curl 8.21.0, ngspice 46, Compiler: Visual C++ 1944 without C++ ABI, KICAD_USE_PCH=OFF
```

Also reproduced with a local RelWithDebInfo and a Debug build of `6dd893f7d1` (MSVC 14.44).
kicad-python: `main` at f79cebd1 (0.9.0.dev0).

---

## Evidence kept locally (not for the issue)

- Deterministic across 12 runs (stock nightly x 8, RelWithDebInfo x 2, Debug x 2); every order
  of two *different* units of U1 hangs (`1,2`, `2,1`, `1,3`, `3,2`, also with an unrelated
  `R1` update in between); `1,1`, `R1,R2`, `C1,C2`, `P1,P2`, `#PWR01,#PWR02` do not; a
  value-only edit and a no-op update behave the same as a mirror toggle.
- Thread dump of the wedged RelWithDebInfo server (cdb `-pv`): scratchpad `g14-g13/g13_stack_rel.txt`;
  throw stack + locals on the Debug server: `g14-g13/g13-work-cdb/cdb_out.txt`
  (`aId` bytes `b1 b9 81 8b c4 89 4d d0 9b 82 4d 41 38 ce cb e8`, `item->m_unit == 2`,
  `item->m_Uuid == ...48b4f263`).
- The 27 pin uuids in `ecc83-pp.kicad_sch` are all distinct; the shared ids are created in memory.
- kiwrap: capability `ipc.schematic_update_shared_lib_symbol_hangs` (matrix `10.99.0`, verified);
  `tests/test_kipy_packaging.py::test_hang_guard_matches_the_live_repro` runs the repro through
  `HeadlessSession` and asserts the matrix value predicts it; `HeadlessSession.close()` still
  terminates the wedged process (`_release_documents` uses a throw-away client with a short
  timeout). No update helper exists in `backends.ipc.writes`; symbol edits go through the file
  engine (UPSTREAM.md 1.1).
- Watch for a fix in `eeschema/api/api_sch_utils.cpp` (`PackSymbol`), `eeschema/sch_symbol.cpp`
  (`Deserialize`) or `common/api/api_server.cpp` (`handleApiRequestString`) on the next nightly
  bump; the live test flips the matrix value.
