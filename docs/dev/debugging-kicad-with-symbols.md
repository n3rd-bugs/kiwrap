# Debugging KiCad crashes without a source build (cdb + symbols.kicad.org)

Status: **verified on this machine** ([VERIFICATION.md claim 12](../VERIFICATION.md#claim-12--survived),
spike `spikes/verify-symbols-pdb/`, `spikes/verify-symbols/rsds.py`). This is the first tool to
reach for when KiCad itself misbehaves under kiwrap; the source build
([kicad-source-build.md](kicad-source-build.md)) is gated behind it.

What is verified:

- `https://symbols.kicad.org/kicad-stable` serves **full private PDBs with line information** for
  the installed KiCad 10.0.0 binaries (and 9.0.1), plus the bundled wxWidgets and vcpkg DLLs,
  keyed by the exact GUID+age from each binary's RSDS record. Only the CAB-compressed `.pd_` form
  is stored (the `.pdb` URL is 404); `symsrv`/`cdb` expand it transparently.
- `cdb` (Windows 10 SDK Debuggers, `C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe`,
  10.0.22621.3233 here) symbolicates both synthetic mini dumps and **real WER dumps** of KiCad to
  `file @ line` with no checkout and no build.
- `EnableAPILogging=1` in `kicad_advanced` makes KiCad write every IPC request/response to
  `logs/api.log` under the documents home.
- Worked example at the end: the eeschema 10.0.0 IPC crash, upstream **#24966**.

## 1. Prerequisites

| Item | Where | Notes |
|---|---|---|
| `cdb.exe` / `windbg.exe` | Windows SDK → "Debugging Tools for Windows" (installed here) | `winget install Microsoft.WinDbg` also works (WinDbg Preview understands the same commands) |
| Symbol cache dir | e.g. `C:\symcache` | **Use backslashes.** A forward-slash cache path made `symsrv` fail with `SYMSRV RESULT 0x57` in the spike |
| KiCad symbol server | `https://symbols.kicad.org/kicad-stable` (also `kicad-testing`, `kicad-nightly` — those are pruned over time; stable is retained) | serves KiCad + wx + vcpkg PDBs only |
| Microsoft symbol server | `https://msdl.microsoft.com/download/symbols` | needed for `ntdll`/`KERNELBASE` frames; KiCad frames symbolicate without it |
| Source checkout (optional) | `git clone` of the matching tag, e.g. `C:\src\kicad` at `10.0.0` | PDBs are **not** source-indexed; file@line paths point at `C:\jenkins\workspace\build-win-10.0-stable\.build\kicad\...`. Add `.srcpath+ C:\src\kicad` to see source text |

Set the symbol path once for every session (environment variable honoured by cdb/WinDbg/VS):

```powershell
$env:_NT_SYMBOL_PATH = 'srv*C:\symcache*https://symbols.kicad.org/kicad-stable;srv*C:\symcache*https://msdl.microsoft.com/download/symbols'
```

### Which PDB does my binary want? (`pdbid.py`)

`spikes/verify-symbols-pdb/pdbid.py` parses the CodeView RSDS record and prints `GUID` + age;
the symstore path is `https://symbols.kicad.org/kicad-stable/<name>.pdb/<GUID><age>/<name>.pd_`.
Installed 10.0.0 identities (for cross-checking a fetch):

| Binary | GUID+age | Served `.pd_` |
|---|---|---|
| `kicad.exe` | `8D4D877A814B46ACA4B697D7D250BC9C5` | yes |
| `pcbnew.exe` | `98E5AA937AF942869CC1829F63866FD35` | yes (1.4 MB) |
| `_pcbnew.dll` | `64D0808226A44C06812F71472CD8A7B55` | yes (54.5 MB) |
| `eeschema.exe` / `_eeschema.dll` | `ECE61F73F98F4B21AE5494337CE31B285` / `BCDE735CB4DA4973B0158155E57740C06` | yes (41.2 MB for `_eeschema`) |
| `kicad-cli.exe` | `CBC90706EE2B4F5EBAF23E4DDE4328B45` | yes (3.4 MB) |
| `kicommon.dll` | `9AEDF265041941DDB93E9EF45FE5F2386` | yes (15.0 MB) |

`symchk.exe /pf <binary> /s srv*C:\symcache*https://symbols.kicad.org/kicad-stable` confirms
"private symbols & lines" for each (all PASSED in the spike). All 10.0.0 binaries were built with
MSVC 14.44.35207 on Jenkins (`build-win-10.0-stable`).

## 2. Getting a dump

### 2a. WER LocalDumps (preferred: catches crashes you did not expect)

Windows Error Reporting only writes local dumps when the `LocalDumps` key **exists** — it is off
on a clean machine (Microsoft: "This feature is not enabled by default"). On this machine the key
was present (created by other installers) with no values, and WER wrote `eeschema.exe.*.dmp`
files to `%LOCALAPPDATA%\CrashDumps` — that is where the worked example came from.

Enable it (admin PowerShell), scoped to the KiCad processes so the rest of the machine is untouched:

```powershell
$base = 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps'
foreach ($exe in 'kicad.exe','pcbnew.exe','eeschema.exe','kicad-cli.exe') {
    $k = "$base\$exe"
    New-Item -Path $k -Force | Out-Null
    New-ItemProperty -Path $k -Name DumpFolder -PropertyType ExpandString -Value 'C:\dumps\kicad' -Force | Out-Null
    New-ItemProperty -Path $k -Name DumpCount  -PropertyType DWord -Value 10 -Force | Out-Null
    New-ItemProperty -Path $k -Name DumpType   -PropertyType DWord -Value 2  -Force | Out-Null   # 2 = full, 1 = mini
}
```

Same thing with `reg.exe` (one process shown):

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\eeschema.exe" /v DumpFolder /t REG_EXPAND_SZ /d C:\dumps\kicad /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\eeschema.exe" /v DumpType /t REG_DWORD /d 2 /f
```

Defaults when values are missing: `DumpFolder = %LOCALAPPDATA%\CrashDumps`, `DumpCount = 10`,
`DumpType = 1` (mini dump: registers, stacks, some memory — enough for a stack with line numbers,
as the worked example shows; `2` gives heap contents for `dv`/`dt` inspection).

**Caveat — Sentry/Crashpad opt-in.** KiCad 10 bundles `crashpad_handler.exe`; Microsoft's docs
say applications with their own crash reporting are not supported by LocalDumps. KiCad only
starts Crashpad when the marker file `%LOCALAPPDATA%\kicad\<major.minor>\sentry-opt-in` exists
(`common/app_monitor.cpp`, branch 10.0). On this machine 9.0 is opted in and 10.0 is not, so 10.0
crashes fall through to WER. For an opted-in version, take the minidump from
`%LOCALAPPDATA%\kicad\<major.minor>\sentry\` instead — same format, same cdb workflow. kiwrap's
sandboxed roots never carry the marker, so sandboxed GUIs always go to WER.

### 2b. procdump fallback (no registry change, or a hang rather than a crash)

```powershell
winget install Microsoft.Sysinternals.ProcDump            # or the Sysinternals zip
# Launch KiCad under procdump and dump on an unhandled exception (-e) or on exit (-t):
procdump -accepteula -ma -e -t -x C:\dumps\kicad "C:\Program Files\KiCad\10.0\bin\eeschema.exe" C:\proj\board.kicad_sch
# Attach to a running (hung) instance by PID and take a full dump now:
procdump -accepteula -ma <pid> C:\dumps\kicad\hang.dmp
```

Run the launched process with the sandbox environment (`KICAD_CONFIG_HOME` etc.) — the hard
rule that nothing touches `%APPDATA%\kicad` applies to debugging sessions too.

### 2c. Under cdb directly (deterministic repros, e.g. kicad-cli)

```powershell
$env:KICAD_CONFIG_HOME = 'C:	mp\kicad-sandbox\config'      # never the real %APPDATA%\kicad, even under a debugger
$env:KICAD_DOCUMENTS_HOME = 'C:	mp\kicad-sandbox\documents'; $env:KICAD_CACHE_HOME = 'C:	mp\kicad-sandbox\cache'
cdb -G -lines -c ".sympath srv*C:\symcache*https://symbols.kicad.org/kicad-stable; !sym noisy; bu _pcbnew!DRC_ENGINE::RunTests; g; k 5; .dump /ma C:\dumps\kicad\cli.dmp; q" `
    "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc -o C:\tmp\drc.rpt C:\tmp\ecc83-pp.kicad_pcb
```

This is `spikes/verify-symbols-pdb/cdb_gen.txt`: symsrv fetched `_pcbnew.pd_`, `kicommon.pd_`
and `kicad-cli.pd_`, the deferred breakpoint resolved and hit, and `.dump /m` wrote a mini dump
that a *fresh* cdb with an empty cache symbolicated to `drc_engine.cpp @ 882`,
`pcbnew_jobs_handler.cpp @ 2425`, `job_dispatcher.cpp @ 58`, `command_pcb_drc.cpp @ 179`.
(`sxe -c ".dump /ma ..." eh` catches first-chance C++ exceptions instead of a breakpoint.)

## 3. Symbolicating a dump

```powershell
cdb -z C:\dumps\kicad\eeschema.exe.89232.dmp -lines `
    -y "srv*C:\symcache*https://symbols.kicad.org/kicad-stable;srv*C:\symcache*https://msdl.microsoft.com/download/symbols" `
    -c ".ecxr; kn 30; lmvm _eeschema; q"
```

Command cheat-sheet (all used in the spike logs `cdb_wer.log`, `cdb_analyze2.log`):

| Command | Purpose |
|---|---|
| `.sympath srv*C:\symcache*https://symbols.kicad.org/kicad-stable` | set/append the symbol path inside a session (`.sympath+` appends) |
| `.reload` / `!sym noisy` | force symbol load / show every symsrv fetch and why one failed |
| `.ecxr` | switch to the exception context of a crash dump (registers + faulting instruction) |
| `kn 30` / `k 12` | stack with frame numbers; `-lines` adds `[file @ line]` |
| `.frame N` then `dv` / `dt` | locals and types in frame N (release PDBs: expect many optimised away) |
| `lm m _eeschema` / `lmvm kicommon` | confirm "private pdb symbols" were loaded, and from where |
| `.srcpath+ C:\src\kicad` + `lsa .` | map Jenkins paths to a local checkout and print source around the PC |
| `.dump /m` (mini) / `.dump /ma` (full) | write a dump from a live cdb session |

Reading the output: `*** WARNING: Unable to verify checksum for _eeschema.dll` is harmless
(images are not checksummed); `Symbol loading may be unreliable without a symbol search path`
means `-y` was forgotten; a frame like `kicommon+0x1234` (no `!`) means the PDB was not found —
run `!sym noisy; .reload /f kicommon.dll` and check the GUID against `pdbid.py`.

## 4. `EnableAPILogging=1` — the IPC transcript (`logs/api.log`)

Create `kicad_advanced` (no extension) inside the **versioned** sandbox config dir and restart
KiCad:

```text
<KICAD_CONFIG_HOME>\10.0\kicad_advanced
    EnableAPILogging=1
```

KiCad then writes every request/response (message type, sequence, status, full payload) to
`<KICAD_DOCUMENTS_HOME>\KiCad\10.0\logs\api.log` (verified live: the file appeared at
`<sandbox>/documents/KiCad/10.0/logs/api.log`; `GetLogsPath()` in `common/paths.cpp` appends
`logs` to the documents home). Read the tail before reaching for a debugger — the spike diagnosed
`RunAction → RAS_OK ×3 then AS_BUSY ×2` (interactive tool left running) purely from this file.
kiwrap's `sandbox.ensure(install, enable_api_server=True)` roots are the right place for this
flag; never put it in the user's real `%APPDATA%\kicad\10.0\kicad_advanced`.

## 5. Worked example — eeschema 10.0.0 crashes on any editor RPC (#24966)

**Symptom.** With the Schematic Editor open (standalone or inside `kicad.exe`), the first
`BeginCommit`, `CreateItems`, `UpdateItems`, `DeleteItems` or `HitTest` request over the IPC API
kills the process; only `GetOpenDocuments` works ([ARCHITECTURE.md §2](../ARCHITECTURE.md),
[VERIFICATION.md claim 4](../VERIFICATION.md#claim-4--corrected)).

**Evidence collected without a build.**

1. The `api.log` of the session that produced this dump was not preserved in `spikes/`; with
   `EnableAPILogging=1` the transcript stops at the incoming `kiapi.common.commands.HitTest`
   request with no response line, which is the cheap way to spot the same class of failure.
2. WER wrote `%LOCALAPPDATA%\CrashDumps\eeschema.exe.89232.dmp` (19 MB mini dump, 2026-09-18
   22:08). Symbolicated with the command in §3 (`spikes/verify-symbols-pdb/cdb_wer.txt/.log`):

```text
0:000> .ecxr
rcx=0000000000000000 ...
_eeschema!API_HANDLER_EDITOR::checkForBusy+0xd:
00007ffb`ce65d19d 488b01   mov  rax,qword ptr [rcx] ds:00000000`00000000=????????????????
0:000> k 12
 # Call Site
00 _eeschema!API_HANDLER_EDITOR::checkForBusy+0xd   [C:\jenkins\...\kicad\common\api\api_handler_editor.cpp @ 207]
01 _eeschema!API_HANDLER_EDITOR::handleHitTest+0x3a [...\common\api\api_handler_editor.cpp @ 325]
02 _eeschema!`API_HANDLER::registerHandler<kiapi::common::commands::HitTest,...>'::`2'::<lambda_1>::operator()+0x13a [...\include\api\api_handler.h @ 112]
04 kicommon!API_HANDLER::Handle+0x30a               [...\common\api\api_handler.cpp @ 55]
05 kicommon!KICAD_API_SERVER::handleApiEvent+0x594  [...\common\api\api_server.cpp @ 246]
06 wxbase332u_vc_x64_custom!wxAppConsoleBase::CallEventHandler+0x58 [...\src\common\appbase.cpp @ 693]
...
1c eeschema!APP_SINGLE_TOP::OnRun                   [...\common\single_top.cpp @ 241]
```

   Access violation `c0000005` reading address 0: `checkForBusy` dereferences `m_frame`, which is
   null because standalone eeschema registers the generic `API_HANDLER_EDITOR` without a frame
   (the same `BeginCommit` path crashed in an earlier run: `handleBeginCommit @ 46`).
3. `lmvm _eeschema` → `_eeschema.pdb BCDE735CB4DA4973B0158155E57740C06`, "private pdb symbols",
   fetched from `symbols.kicad.org/kicad-stable` on the spot.
4. Cross-check against source: `git show 10.0.0:common/api/api_handler_editor.cpp` line 207 in a
   plain checkout (no build) shows the `m_frame->...` access; upstream issue #24966; the fix
   (commit `f3105844`, 10.0 branch) also adds the `document` field to Begin/EndCommit and lands in
   **10.0.7** (unreleased as of 2026-09-18).

**Consequence for kiwrap** (decision D2): live sessions use a sandboxed standalone `pcbnew.exe`
only; every mutating RPC is refused while `GetOpenDocuments(DOCTYPE_SCHEMATIC)` is non-empty on
KiCad < 10.0.7; the capability record carries `ipc.schematic_write_crashes` and
`ipc.commit_needs_document` so no tool ever branches on the version number.

## 6. Checklist when KiCad misbehaves under kiwrap

1. Reproduce with the sandbox root + `EnableAPILogging=1`; read `logs/api.log` tail.
2. Crash? Take the WER/procdump dump; run the §3 command; paste the `.ecxr` + `kn` output into
   the issue. Hang? `procdump -ma <pid>`, then `~*kn` in cdb to see every thread.
3. Map frames to source with a checkout of the exact tag (`.srcpath+`).
4. Search GitLab issues for the function name; note the fixing commit and the first release that
   carries it → capability key, not a version check.
5. Only if locals are needed and steps 1–4 did not explain it: trigger **T-BLOCKER** in
   [kicad-source-build.md](kicad-source-build.md).
