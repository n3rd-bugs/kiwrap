//! Thin Win32 wrappers. Everything here is in physical pixels: `main` opts the process into
//! per-monitor-v2 DPI awareness before any rect is read (docs/research/display/winstream.md 2d:
//! the context handle must be passed as a pointer-sized value, otherwise every rect comes back
//! DPI-virtualised).

use windows::Win32::Foundation::{HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Dwm::{
    DwmGetWindowAttribute, DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS,
};
use windows::Win32::Graphics::Gdi::{
    EnumDisplayMonitors, GetMonitorInfoW, MonitorFromWindow, HDC, HMONITOR, MONITORINFO,
    MONITOR_DEFAULTTONEAREST,
};
use windows::Win32::UI::HiDpi::{
    GetDpiForWindow, SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};
use windows::Win32::UI::Input::KeyboardAndMouse::{GetFocus, IsWindowEnabled};
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, EnumWindows, GetAncestor, GetClassNameW, GetForegroundWindow,
    GetGUIThreadInfo, GetWindow, GetWindowLongPtrW, GetWindowRect, GetWindowTextW,
    GetWindowThreadProcessId, IsIconic, IsWindow, IsWindowVisible,
    SetForegroundWindow, SetWindowPos, GA_ROOT, GUITHREADINFO, GWL_EXSTYLE, GWL_STYLE, GW_OWNER,
    SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER,
};
use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};

use crate::surfaces::{Rect, WinInfo};

pub fn hwnd(h: usize) -> HWND {
    HWND(h as *mut core::ffi::c_void)
}

/// Per-monitor v2 so every rect we read is in physical pixels (150 % scaling on the bench box).
pub fn set_dpi_aware() {
    unsafe {
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).is_err() {
            log::warn!("SetProcessDpiAwarenessContext(PMv2) failed; rects may be virtualised");
        }
    }
}

/// `DWMWA_EXTENDED_FRAME_BOUNDS`: the visible frame (no invisible resize borders), physical px.
/// Windows.Graphics.Capture delivers exactly this rect for a window (winstream.md 2a), so it is
/// the coordinate origin of every frame we stream. Falls back to `GetWindowRect`.
pub fn frame_bounds(h: usize) -> Rect {
    let mut r = RECT::default();
    unsafe {
        if DwmGetWindowAttribute(
            hwnd(h),
            DWMWA_EXTENDED_FRAME_BOUNDS,
            &mut r as *mut RECT as *mut _,
            std::mem::size_of::<RECT>() as u32,
        )
        .is_err()
        {
            let _ = GetWindowRect(hwnd(h), &mut r);
        }
    }
    Rect { l: r.left, t: r.top, r: r.right, b: r.bottom }
}

pub fn window_rect(h: usize) -> Rect {
    let mut r = RECT::default();
    unsafe {
        let _ = GetWindowRect(hwnd(h), &mut r);
    }
    Rect { l: r.left, t: r.top, r: r.right, b: r.bottom }
}

/// `DWMWA_CLOAKED` flags (0 = not cloaked; 1 app, 2 shell/other virtual desktop, 4 inherited).
pub fn cloaked_flags(h: usize) -> u32 {
    let mut v: u32 = 0;
    unsafe {
        if DwmGetWindowAttribute(
            hwnd(h),
            DWMWA_CLOAKED,
            &mut v as *mut u32 as *mut _,
            std::mem::size_of::<u32>() as u32,
        )
        .is_err()
        {
            return 0;
        }
    }
    v
}

pub fn title(h: usize) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(hwnd(h), &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn class_name(h: usize) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetClassNameW(hwnd(h), &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn owner(h: usize) -> usize {
    unsafe { GetWindow(hwnd(h), GW_OWNER).map(|w| w.0 as usize).unwrap_or(0) }
}

pub fn thread_and_pid(h: usize) -> (u32, u32) {
    let mut pid = 0u32;
    let tid = unsafe { GetWindowThreadProcessId(hwnd(h), Some(&mut pid)) };
    (tid, pid)
}

pub fn is_enabled(h: usize) -> bool {
    unsafe { IsWindowEnabled(hwnd(h)).as_bool() }
}

pub fn is_window(h: usize) -> bool {
    unsafe { IsWindow(Some(hwnd(h))).as_bool() }
}

pub fn dpi_for_window(h: usize) -> u32 {
    let d = unsafe { GetDpiForWindow(hwnd(h)) };
    if d == 0 {
        96
    } else {
        d
    }
}

pub fn foreground() -> usize {
    unsafe { GetForegroundWindow().0 as usize }
}

pub fn root_of(h: usize) -> usize {
    unsafe { GetAncestor(hwnd(h), GA_ROOT).0 as usize }
}

/// All top-level windows in z-order (top first).
pub fn all_top_level() -> Vec<usize> {
    unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> windows::core::BOOL {
        let v = unsafe { &mut *(lp.0 as *mut Vec<usize>) };
        v.push(h.0 as usize);
        true.into()
    }
    let mut out: Vec<usize> = Vec::with_capacity(256);
    unsafe {
        let _ = EnumWindows(Some(cb), LPARAM(&mut out as *mut _ as isize));
    }
    out
}

/// Visible, non-minimised, non-empty top-level windows of `pid` in z-order (top first).
/// Cloaked windows are INCLUDED (the patched KiCad in `KICAD_INPUT_FROM_EVENTS=cloak` mode
/// cloaks every top-level; WGC still captures them, win-input-patch.md 4) and flagged.
pub fn windows_of_pid(pid: u32) -> Vec<WinInfo> {
    let mut out = Vec::new();
    for h in all_top_level() {
        let (tid, p) = thread_and_pid(h);
        if p != pid {
            continue;
        }
        unsafe {
            if !IsWindowVisible(hwnd(h)).as_bool() || IsIconic(hwnd(h)).as_bool() {
                continue;
            }
        }
        let rect = frame_bounds(h);
        if rect.w() <= 0 || rect.h() <= 0 {
            continue;
        }
        let (style, exstyle) = unsafe {
            (
                GetWindowLongPtrW(hwnd(h), GWL_STYLE) as u32,
                GetWindowLongPtrW(hwnd(h), GWL_EXSTYLE) as u32,
            )
        };
        let z = out.len();
        out.push(WinInfo {
            hwnd: h,
            title: title(h),
            class: class_name(h),
            rect,
            owner: owner(h),
            z,
            enabled: is_enabled(h),
            cloaked: cloaked_flags(h),
            thread_id: tid,
            style,
            exstyle,
        });
    }
    out
}

pub fn screen_to_client(h: usize, sx: i32, sy: i32) -> (i32, i32) {
    let mut p = POINT { x: sx, y: sy };
    unsafe {
        let _ = windows::Win32::Graphics::Gdi::ScreenToClient(hwnd(h), &mut p);
    }
    (p.x, p.y)
}

/// Focus / active / capture windows of a UI thread, without attaching to it (`GetGUIThreadInfo`).
pub fn thread_focus(tid: u32) -> (usize, usize, usize) {
    let mut gti = GUITHREADINFO {
        cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
        ..Default::default()
    };
    unsafe {
        if GetGUIThreadInfo(tid, &mut gti).is_err() {
            return (0, 0, 0);
        }
    }
    (gti.hwndFocus.0 as usize, gti.hwndActive.0 as usize, gti.hwndCapture.0 as usize)
}

/// Focus window of the thread owning `h` for a *background* KiCad: `GetGUIThreadInfo` first,
/// then the classic AttachThreadInput + GetFocus (winstream `focused_hwnd_of_thread`).
#[allow(dead_code)] // classic AttachThreadInput fallback, kept for background-focus edge cases
pub fn focused_hwnd_of_thread(h: usize) -> usize {
    let (tid, _) = thread_and_pid(h);
    let (focus, _, _) = thread_focus(tid);
    if focus != 0 && is_window(focus) {
        return focus;
    }
    unsafe {
        let me = GetCurrentThreadId();
        if tid != me && AttachThreadInput(me, tid, true).as_bool() {
            let f = GetFocus().0 as usize;
            let _ = AttachThreadInput(me, tid, false);
            if f != 0 {
                return f;
            }
        }
    }
    0
}

/// `SetForegroundWindow` is refused for background processes. Order of attempts (winstream
/// `bring_to_front`): plain call; then AttachThreadInput to the foreground thread and retry.
/// The synthetic ALT tap of the prototype is NOT used here: it puts a wx frame's menu bar into
/// keyboard mode and swallows the next click (winstream.md 3.2).
pub fn bring_to_front(h: usize) -> bool {
    unsafe {
        if foreground() == h {
            return true;
        }
        let _ = SetForegroundWindow(hwnd(h));
        if foreground() != h {
            let fg = foreground();
            let fg_tid = if fg != 0 { thread_and_pid(fg).0 } else { 0 };
            let me = GetCurrentThreadId();
            let attached = fg_tid != 0 && fg_tid != me && AttachThreadInput(me, fg_tid, true).as_bool();
            let _ = BringWindowToTop(hwnd(h));
            let _ = SetForegroundWindow(hwnd(h));
            if attached {
                let _ = AttachThreadInput(me, fg_tid, false);
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
        foreground() == h
    }
}

/// Move a window (no size change, no activation, no z-order change). `x, y` are the frame-bounds
/// origin wanted; the delta between frame bounds and the window rect is applied.
pub fn move_window_frame(h: usize, x: i32, y: i32) -> bool {
    let fb = frame_bounds(h);
    let wr = window_rect(h);
    let (dx, dy) = (fb.l - wr.l, fb.t - wr.t);
    unsafe {
        SetWindowPos(hwnd(h), None, x - dx, y - dy, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            .is_ok()
    }
}

/// Restore (if maximised) and place a window so that its DWM frame bounds become (x, y, w, h);
/// omitted values keep the current ones. Two passes: SetWindowPos wants the full window rect
/// (with invisible borders), so place, measure the delta, correct (winstream `resize_window`).
pub fn place_window_frame(h: usize, x: Option<i32>, y: Option<i32>, w: Option<i32>, hh: Option<i32>) -> bool {
    use windows::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_RESTORE};
    unsafe {
        if IsIconic(hwnd(h)).as_bool() || (GetWindowLongPtrW(hwnd(h), GWL_STYLE) as u32) & 0x0100_0000 != 0 {
            let _ = ShowWindow(hwnd(h), SW_RESTORE);
            std::thread::sleep(std::time::Duration::from_millis(150));
        }
    }
    let cur = frame_bounds(h);
    let (x, y) = (x.unwrap_or(cur.l), y.unwrap_or(cur.t));
    let (w, hh) = (w.unwrap_or(cur.w()), hh.unwrap_or(cur.h()));
    for _ in 0..3 {
        let fb = frame_bounds(h);
        let wr = window_rect(h);
        let (dx, dy) = (fb.l - wr.l, fb.t - wr.t);
        let (dw, dh) = (wr.w() - fb.w(), wr.h() - fb.h());
        let ok = unsafe {
            SetWindowPos(hwnd(h), None, x - dx, y - dy, w + dw, hh + dh, SWP_NOZORDER | SWP_NOACTIVATE).is_ok()
        };
        if !ok {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(60));
        let now = frame_bounds(h);
        if now.l == x && now.t == y && now.w() == w && now.h() == hh {
            return true;
        }
    }
    let now = frame_bounds(h);
    (now.l - x).abs() <= 2 && (now.t - y).abs() <= 2 && (now.w() - w).abs() <= 2 && (now.h() - hh).abs() <= 2
}

/// (HMONITOR as usize, monitor rect) of the monitor nearest to `h`.
pub fn monitor_of(h: usize) -> (usize, Rect) {
    unsafe {
        let m = MonitorFromWindow(hwnd(h), MONITOR_DEFAULTTONEAREST);
        let mut info = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        let _ = GetMonitorInfoW(m, &mut info);
        let r = info.rcMonitor;
        (m.0 as usize, Rect { l: r.left, t: r.top, r: r.right, b: r.bottom })
    }
}

/// All monitors in `EnumDisplayMonitors` order (what windows-capture uses for `Monitor::from_index`).
#[allow(dead_code)] // monitor enumeration, kept for multi-monitor menu placement
pub fn monitors() -> Vec<(usize, Rect)> {
    unsafe extern "system" fn cb(m: HMONITOR, _dc: HDC, r: *mut RECT, lp: LPARAM) -> windows::core::BOOL {
        let v = unsafe { &mut *(lp.0 as *mut Vec<(usize, Rect)>) };
        let r = unsafe { *r };
        v.push((m.0 as usize, Rect { l: r.left, t: r.top, r: r.right, b: r.bottom }));
        true.into()
    }
    let mut out: Vec<(usize, Rect)> = Vec::new();
    unsafe {
        let _ = EnumDisplayMonitors(None, None, Some(cb), LPARAM(&mut out as *mut _ as isize));
    }
    out
}

pub fn virtual_screen() -> Rect {
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    };
    unsafe {
        let x = GetSystemMetrics(SM_XVIRTUALSCREEN);
        let y = GetSystemMetrics(SM_YVIRTUALSCREEN);
        let w = GetSystemMetrics(SM_CXVIRTUALSCREEN);
        let h = GetSystemMetrics(SM_CYVIRTUALSCREEN);
        Rect { l: x, t: y, r: x + w, b: y + h }
    }
}

/// Full image path of a process (`QueryFullProcessImageNameW`), None when it cannot be opened.
pub fn process_image(pid: u32) -> Option<String> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_FORMAT, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_FORMAT(0), PWSTR(buf.as_mut_ptr()), &mut len).is_ok();
        let _ = CloseHandle(h);
        if !ok {
            return None;
        }
        Some(String::from_utf16_lossy(&buf[..len as usize]))
    }
}

pub fn process_alive(pid: u32) -> bool {
    use windows::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    unsafe {
        match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => {
                let mut code = 0u32;
                let ok = GetExitCodeProcess(h, &mut code).is_ok();
                let _ = CloseHandle(h);
                ok && code == STILL_ACTIVE.0 as u32
            }
            Err(_) => false,
        }
    }
}
