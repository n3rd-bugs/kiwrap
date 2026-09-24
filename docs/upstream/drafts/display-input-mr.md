# MR draft: `mr/display-input` (NOT opened; branch pushed to the private GitHub fork only, 2026-09-20)

Branch `mr/display-input` on `n3rd-bugs/kicad-fork` (private) = `base/6dd893f7d1` (nightly
10.99.0-4083) + one commit `67ceff363d` "Add an input-from-events mode for message-driven editors
(MSW)", 25 files, +534 −48. Evidence and measurements:
[`../../research/display/win-input-patch.md`](../../research/display/win-input-patch.md).

## Recommendation

Open an issue first (below), framed as **assistive / automated input on Windows**, not as
"remote display for an IDE": the KiCad developers accept platform-integration fixes for
accessibility tooling and test automation, and the change is a pure opt-in (an environment
variable, nothing changes when unset). Keep the MR to the seam + filter + the mechanical sweep;
leave the `cloak` value out of the first MR (it is a display-helper feature and the reviewers will
ask why KiCad should hide its own windows) and propose it as a follow-up or keep it fork-only.
Before submitting: rebase on `upstream/master` (the sweep touches `tool_dispatcher.cpp`,
`wx_view_controls.cpp`, `pcb_selection_tool.cpp`, which move often), run the pcbnew/eeschema QA
binaries, `clang-format --dry-run` the changed ranges, and add the guard for hardware key input
noted under "Open points" so the mode is safe even when a user types into a window that is in this
mode. Do not batch with the G1/G6 branches.

---

## Issue text (draft)

**Title:** Windows: editors cannot be driven by posted window messages (assistive input, UI automation)

**Description**

On Windows, KiCad's canvas and dialogs consult the hardware instead of the events they receive:

- `WX_VIEW_CONTROLS` reads `wxGetMousePosition()` (`GetCursorPos`) for every motion event and for
  `GetMousePosition()`, `refreshMouse()` and the context-menu position, so a `WM_LBUTTONDOWN` posted
  to the canvas selects whatever is under the *physical* cursor, and the context menu opens there;
- `TOOL_DISPATCHER::BUTTON_STATE::GetState()` uses `wxGetMouseState()` (`GetAsyncKeyState`), so a
  posted drag is released as soon as the physical button is up;
- the modifier flags come from `GetKeyState()`, which posted `WM_KEYDOWN VK_CONTROL` messages never
  update, so `Ctrl+Z` cannot be sent as messages;
- `wxEVT_CHAR_HOOK` is produced by wxMSW's `WH_KEYBOARD` hook, which Windows runs for hardware input
  only, so a posted Escape reaches `wxEVT_KEY_DOWN`/`wxEVT_CHAR` but never `wxDialogBase::OnCharHook`
  or `DIALOG_SHIM::OnCharHook`: dialogs cannot be cancelled;
- with `center_on_zoom` (default on) every wheel step calls `WarpPointer()` and moves the physical
  cursor.

This matters for assistive input software that synthesises input per window rather than through
`SendInput` (switch access, eye-gaze and voice tools that target a background window), for UI test
automation that must not take over the desktop, and for remote/mirrored display helpers. Linux/X11
does not have the problem (`XTEST` on a private display) and GTK generates `wxEVT_CHAR_HOOK` from
the key press itself.

**Proposal**

An opt-in mode, `KICAD_INPUT_FROM_EVENTS=1` in the environment, in which KiCad believes its
events: `KIPLATFORM::UI::GetMousePosition()` returns the position of the last mouse event (or last
programmatic warp), a new `KIPLATFORM::UI::GetMouseState()` the buttons of the last mouse event, a
new `KIPLATFORM::UI::IsKeyDown()` the thread's synchronous key state; `WarpPointer()` moves the
reported position only; a process-wide `wxEventFilter` keeps that state, mirrors key transitions
into the thread key state (`SetKeyboardState()`, so wx and native controls agree), derives
`wxEVT_CHAR_HOOK` from `wxEVT_KEY_DOWN` the way the keyboard hook does, and ignores the spurious
`WM_MOUSELEAVE` the system raises for a window the real cursor is not in. Callers that polled
`wxGetMouseState()`/`wxGetKeyState()` directly go through the seam. No behaviour change when the
variable is unset; GTK/macOS keep their implementations.

---

## MR text (draft)

**Title:** Add an input-from-events mode for message-driven editors (MSW)

**Summary**

`KICAD_INPUT_FROM_EVENTS=1` makes the Windows build take pointer position, button state and key
state from the events it receives instead of from the hardware, so an editor can be driven by
window messages posted to it (assistive input tools, UI automation, display helpers) without
touching the user's cursor, focus or keyboard. Nothing changes while the variable is unset.

**What changes**

- `libs/kiplatform/include/kiplatform/ui.h`: `InputFromEvents()`, `StartInputFromEvents()`,
  `GetMouseState()`, `IsKeyDown()`; `GetMousePosition()` and `WarpPointer()` documented for the mode.
- `libs/kiplatform/port/wxmsw/ui.cpp`: the mode (state on the wx app object because kiplatform is
  linked statically into several modules; the event filter; the CHAR_HOOK synthesis mirrors
  `wxKeyboardHook`'s rules: focus window else active window, capture suppresses it, a consumed hook
  consumes the key-down so wx suppresses the char).
- `libs/kiplatform/port/wxgtk/ui.cpp`, `port/wxosx/ui.mm`: pass-through stubs.
- `common/pgm_base.cpp`: install the filter for GUI programs.
- `common/view/wx_view_controls.cpp`: key state through the seam; the auto-pan timer does not poll
  the real cursor in this mode; the drag-zoom edge warp honours a refused warp (`m_infinitePanWorks`
  was never set on that path — on Wayland, where `WarpPointer()` can fail, the start point jumped).
- `common/tool/tool_dispatcher.cpp`, `common_tools.cpp`, `action_menu.cpp`, the three selection
  tools, the footprint/symbol/design-block choosers, the Tab view switchers, the 3D preview,
  `sch_move_tool.cpp`: `wxGetMouseState()`/`wxGetKeyState()` → `KIPLATFORM::UI::GetMouseState()`/
  `IsKeyDown()`; the four direct `wxGetMousePosition()` users in common widgets →
  `KIPLATFORM::UI::GetMousePosition()`.

**Testing**

Windows 11, MSVC 14.44, wx 3.3.2, RelWithDebInfo. With the variable unset: pcbnew starts, mouse and
keyboard behave as before (the mode's code is not reached; the seam functions fall through to the
wx calls). With the variable set and the window covered by another application (or fully hidden):
an external driver posting `WM_MOUSEMOVE/WM_*BUTTON*/WM_MOUSEWHEEL/WM_KEYDOWN/WM_KEYUP` selects
items by click, rotates (`R`), deletes, undoes (`Ctrl+Z`), drag-moves a footprint, zooms (wheel,
centred on the reported pointer without moving the physical cursor), pans (middle drag), opens the
properties dialog (double-click and `E`), types into a field and presses OK, opens the footprint
chooser, filters it and closes it with Esc, opens the context menu at the reported pointer and
clicks an item; `GetCursorPos` and `GetForegroundWindow` are unchanged throughout. Latency from
posted message to WGC frame 37 ms median; 27 fps under continuous motion.

**Known limits (documented in the header)**

Tooltips are a comctl32 control that polls the real cursor and do not appear; popup menus and
combobox drop-downs are OS windows and are placed by the system; the frame still activates itself
when first shown.

---

## Open points before submitting

1. **Hardware input in event mode**: the WH_KEYBOARD hook still runs for real keys, so a key the
   hook handler *skips* is offered as `wxEVT_CHAR_HOOK` a second time by the filter. Add a guard
   (e.g. skip synthesis when `GetMessageExtraInfo()` / the last hook call marks the key as hardware,
   or track the VK the hook just saw) so a user typing into a window in this mode sees no
   difference.
2. **`center_on_zoom`** semantics in this mode: the view centres on the reported pointer and the
   reported pointer jumps to the canvas centre (the physical cursor does not move); an automation
   client is expected to send absolute positions and is unaffected; document it.
3. **`cloak`** (DWMWA_CLOAK on show): keep fork-only for now, or propose separately as
   "KICAD_HIDE_FRAMES" for headless-with-GUI automation; the reviewers will want a use case that is
   not "an IDE mirrors KiCad".
4. **Advanced-config alternative**: the reviewers may prefer an `kicad_advanced` key over an
   environment variable; the env var is process-scoped (a launcher can set it per process) and
   matches `KICAD_SOFTWARE_RENDERING`, which is the argument to make.
5. **Eeschema / symbol / footprint editors** share the paths but were not run under the mode;
   run the eeschema matrix (select, `R`, drag, `E`, `A` symbol chooser, Esc) before submitting.
6. Alt combinations (`WM_SYSKEYDOWN` with `KF_ALTDOWN`) are handled by wx from the message itself
   and should work; untested.
7. Check `qa_pcbnew`/`qa_eeschema` still pass (the mode is off there; only the seam changed) and
   that the GTK stub compiles (it was not built here).
