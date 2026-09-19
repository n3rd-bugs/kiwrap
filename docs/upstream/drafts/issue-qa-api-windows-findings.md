# Draft issue texts from the bench acceptance run (NOT posted; 2026-09-19)

Three findings from running `qa_api` (qa/tests/api) of commit 6dd893f7d1 on Windows, once as a
Debug build and once as RelWithDebInfo. Each is a separate issue candidate; the per-issue text
below is written for the GitLab tracker (version block from `kicad-cli version --format about`
of the bench build). None of them is a kiwrap blocker; they are filed so the QA baseline is
understood before any of our MRs touch `qa/tests/api`.

---

## A. `ApiE2E/CreateDocument` compares a path with separator against one without

**Where:** `qa/tests/api/test_api_e2e.cpp`, `CreateDocument` case, line 614 (at 6dd893f7d1):

    BOOST_CHECK( document.project().path() == projectWithExt.GetPath( true ) );

**What happens:** `wxFileName::GetPath( int flags = wxPATH_GET_VOLUME, ... )` takes an int, so
`GetPath( true )` is `GetPath( wxPATH_GET_VOLUME )` (`0x0001`), which returns the directory
WITHOUT a trailing separator. The server fills `project().path()` from
`PROJECT::GetProjectPath()`, which is `GetPathWithSep()`. The two assertions (schematic and
board context) fail on every platform; the rest of the case (create, modified state, save,
files exist) passes.

Observed on Windows, Debug and RelWithDebInfo builds of 6dd893f7d1:

    test_api_e2e.cpp(614): error: in "ApiE2E/CreateDocument": check document.project().path() == projectWithExt.GetPath( true ) has failed

Sending `CreateDocument` for the same paths through kicad-python returns
`project.path == 'C:\...\kicC129.tmp\'` (with separator), i.e. the server side is consistent
with `OpenDocument`.

**Proposed fix:** `GetPath( wxPATH_GET_VOLUME | wxPATH_GET_SEPARATOR )` (one line). I can send
the MR if wanted.

---

## B. `SetPageSettings` with an unset page size asserts in Debug builds and wedges the headless server

**Where:** `common/api/api_handler_editor.cpp` `handleSetPageSettings` ->
`common/api/api_enums.cpp:822` `FromProtoEnum<PAGE_SIZE_TYPE, PageSize>`: the `default:` branch
is `wxASSERT_MSG( false, "Unhandled case in FromProtoEnum<types::PageSize>" )` and the proto
default `PS_UNKNOWN = 0` reaches it whenever a client sets only some of the page settings.

**What happens:** in a Debug build the assert fires inside `kicad-cli api-server`; the process
stays alive but never answers (the request and every later one time out). The QA case
`ApiE2E/CreateDocumentRejectsUnsavedModifications` does exactly this (it sets only
`orientation`), so on a Debug build it fails after 10 s, the shared api-server is wedged, and
the six `ApiJobs` cases that follow fail on `OpenDocument ... nng_recv failed: Timed out`.
Run alone, `ApiJobs` passes 6/6. RelWithDebInfo builds are unaffected (the assert compiles
out and the handler proceeds).

Stack (Debug, 6dd893f7d1):

    api_enums.cpp(823): assert "false" failed in FromProtoEnum(): Unhandled case in FromProtoEnum<types::PageSize>
    [01] FromProtoEnum<enum PAGE_SIZE_TYPE,enum kiapi::common::types::PageSize>  api_enums.cpp:822
    [02] API_HANDLER_EDITOR::handleSetPageSettings                               api_handler_editor.cpp:562

**Proposed fix:** treat `PS_UNKNOWN` as "leave the current size" in `handleSetPageSettings`
(or reject it with `AS_BAD_REQUEST`), so the assert is never reachable from client input; a
handler should not be able to assert on any request. The same pattern exists for
`SetBoardDesignRules`: its `BOARD_DESIGN_SETTINGS` copy in `handleSetBoardDesignRules`
(`api_handler_pcb.cpp:892`) reaches `JSON_SETTINGS::Migrate` with
`assert "m_migrators.count( filever ) > 0"` (`json_settings.cpp:778`) on a Debug build and
wedges the server the same way (seen with an empty `rules` message).

---

## C. `ApiWizards/QueryWizards` cannot run on Windows: unexpected `MOCK_PGM_BASE::IsKicadEnvVariableDefined()`

**Where:** `qa/tests/api/test_api_wizards.cpp`, `API_PLUGIN_MANAGER::ReloadPlugins` ->
`common/api/api_plugin_manager.cpp:422` (`#ifdef _WIN32` branch) `FindKicadFile( "pythonw.exe" )`
-> `common/gestfich.cpp:110` `Pgm().IsKicadEnvVariableDefined()`.

**What happens:** the QA binary's `MOCK_PGM_BASE` has no expectation for
`IsKicadEnvVariableDefined`, and the call is only reached on the `_WIN32` path (the binary is
not next to `pythonw.exe`, and with `KICAD_RUN_FROM_BUILD_DIR` the build-tree lookup
`qa/tests/pythonw/pythonw.exe` does not exist either), so the case aborts on Windows before
it can look for wizards:

    unknown location(0): error: in "ApiWizards/QueryWizards": unexpected call: ?.MOCK_PGM_BASE::IsKicadEnvVariableDefined()

Related: with `KICAD_RUN_FROM_BUILD_DIR=1`, `PATHS::GetStockDataPath( true )` resolves to the
executable's parent directory, so `qa_api.exe` (in `qa/tests/api/`) looks for
`qa/tests/schemas/api.v1.schema.json` and logs `schema file ... not found` (the mac fixture in
`test_remote_provider_client.cpp` works around the same thing with `KICAD_STOCK_DATA_HOME`).

**Proposed fix:** `MOCK_EXPECT( IsKicadEnvVariableDefined ).returns( false )` in the API
mocks (or skip the case when no wizard-capable Python is configured), and a `schemas`
junction/copy next to the QA binaries in `qa/CMakeLists.txt` for Windows.

---

### Version block (bench build; the stock nightly 10.99.0-4083 shows the same API behaviour)

    Application: kicad-cli x64 on x64
    Version: 10.99.0-unknown (6dd893f7d1), debug build / RelWithDebInfo build
    Libraries: wxWidgets 3.3.2, FreeType 2.14.3, HarfBuzz 14.3.1, FontConfig 2.17.1, libcurl/8.21.0
    Platform: Windows 11 (build 26200), 64-bit edition, 64 bit, Little endian, wxBase
    Build: MSVC 14.44.35207 (VS 2022 17.14), vcpkg 66c0373d, KICAD_IPC_API=ON, KICAD_BUILD_QA_TESTS=ON
