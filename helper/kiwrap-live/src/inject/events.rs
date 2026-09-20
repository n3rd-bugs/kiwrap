//! `events`: `PostMessageW` only (port of winstream `inject_events.py`), for a KiCad built with
//! `mr/display-input` and started with `KICAD_INPUT_FROM_EVENTS=1|cloak`. No `SendInput`,
//! `SetForegroundWindow`, `SetFocus` or `AttachThreadInput` anywhere: the user's cursor, focus and
//! keyboard are never touched and the KiCad window may be cloaked or covered
//! (docs/research/display/win-input-patch.md 3, 5).
//!
//! Targeting (mirrors what the OS does for real input):
//! * mouse  -> the deepest visible child under the point among THIS surface's top-level windows
//!   in z-order (a dialog / menu above the frame wins), then any popup of the process; a disabled
//!   top-level (the frame behind a modal dialog) gets nothing; client coordinates in `lParam`,
//!   `MK_*` button + Ctrl/Shift bits in `wParam`; a `WM_MOUSEMOVE` precedes every button-down;
//!   `WM_MOUSELEAVE` to the previous window on hand-over (the OS only does that for the real
//!   cursor); `WM_xBUTTONDBLCLK` when a second down arrives within `GetDoubleClickTime()` and the
//!   double-click rectangle (the OS synthesises that only for real input);
//! * wheel  -> same HWND, `lParam` in screen coordinates (WM_MOUSEWHEEL convention);
//! * keys   -> the UI thread's focus window (`GetGUIThreadInfo`) when it belongs to the surface,
//!   else the surface's topmost enabled window; scan code + extended bit; modifiers as ordinary
//!   VK_SHIFT/VK_CONTROL/VK_MENU messages (the patched KiCad mirrors them into its thread key
//!   state); Alt as WM_SYSKEY* with the context bit; no WM_CHAR (wx's TranslateMessage makes it);
//! * Esc    -> a posted VK_ESCAPE works on the patched build (synthesised wxEVT_CHAR_HOOK), except
//!   in a native `#32770` dialog whose grid editor holds the focus (measured); `--esc-close auto`
//!   therefore turns Esc on an owned `#32770` into `WM_CLOSE` (what Cancel does).

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::{GetDoubleClickTime, MapVirtualKeyW, MAPVK_VK_TO_VSC};
use windows::Win32::UI::WindowsAndMessaging::{
    GetSystemMetrics, PostMessageW, SM_CXDOUBLECLK, SM_CYDOUBLECLK, WM_CLOSE, WM_KEYDOWN, WM_KEYUP,
    WM_LBUTTONDBLCLK, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDBLCLK, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEHWHEEL,
    WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_RBUTTONDBLCLK, WM_RBUTTONDOWN, WM_RBUTTONUP, WM_SYSKEYDOWN,
    WM_SYSKEYUP, WM_XBUTTONDBLCLK, WM_XBUTTONDOWN, WM_XBUTTONUP,
};

use super::{
    deepest_child, generic_modifier, is_modifier_vk, lparam_xy, missing_modifiers, resolve_vk, Injector, MouseButton,
    Target, VK_CONTROL, VK_ESCAPE, VK_MENU, VK_SHIFT,
};
use crate::hub::EscClose;

const WM_MOUSELEAVE: u32 = 0x02A3;
use crate::protocol::Modifiers;
use crate::win;

const MK_LBUTTON: u32 = 0x0001;
const MK_RBUTTON: u32 = 0x0002;
const MK_SHIFT: u32 = 0x0004;
const MK_CONTROL: u32 = 0x0008;
const MK_MBUTTON: u32 = 0x0010;
const MK_XBUTTON1: u32 = 0x0020;
const MK_XBUTTON2: u32 = 0x0040;

pub fn post(h: usize, msg: u32, wp: usize, lp: isize) -> bool {
    unsafe { PostMessageW(Some(HWND(h as *mut _)), msg, WPARAM(wp), LPARAM(lp)).is_ok() }
}

fn mk_bit(b: MouseButton) -> u32 {
    match b {
        MouseButton::Left => MK_LBUTTON,
        MouseButton::Right => MK_RBUTTON,
        MouseButton::Middle => MK_MBUTTON,
        MouseButton::X1 => MK_XBUTTON1,
        MouseButton::X2 => MK_XBUTTON2,
    }
}

fn msgs(b: MouseButton) -> (u32, u32, u32) {
    match b {
        MouseButton::Left => (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK),
        MouseButton::Right => (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK),
        MouseButton::Middle => (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK),
        MouseButton::X1 | MouseButton::X2 => (WM_XBUTTONDOWN, WM_XBUTTONUP, WM_XBUTTONDBLCLK),
    }
}

/// Is `h` an owned top-level window (a dialog / DIALOG_SHIM), never a popup menu?
pub fn is_owned_dialog(root: usize) -> bool {
    root != 0 && win::owner(root) != 0 && win::class_name(root) != "#32768"
}

pub struct EventInjector {
    esc_close: EscClose,
    buttons: u32,
    mods: u32,
    alt: bool,
    held: HashSet<u16>,
    held_ext: HashSet<u16>,
    last_down: HashMap<u32, (Instant, i32, i32)>,
    last_pos: (i32, i32),
    hover: usize,
    dbl_ms: u128,
    dbl_w: i32,
    dbl_h: i32,
    pub posted: u64,
    pub misses: u64,
    pub unknown_keys: u64,
    pub esc_closes: u64,
}

impl EventInjector {
    pub fn new(esc_close: EscClose) -> Self {
        unsafe {
            EventInjector {
                esc_close,
                buttons: 0,
                mods: 0,
                alt: false,
                held: HashSet::new(),
                held_ext: HashSet::new(),
                last_down: HashMap::new(),
                last_pos: (0, 0),
                hover: 0,
                dbl_ms: GetDoubleClickTime() as u128,
                dbl_w: GetSystemMetrics(SM_CXDOUBLECLK) / 2,
                dbl_h: GetSystemMetrics(SM_CYDOUBLECLK) / 2,
                posted: 0,
                misses: 0,
                unknown_keys: 0,
                esc_closes: 0,
            }
        }
    }

    /// Deepest visible child under the screen point: this surface's windows first (z-order),
    /// then any popup of the process. (0, 0, 0) = nothing (or a disabled top-level).
    pub fn hit_test(t: &Target, sx: i32, sy: i32) -> (usize, i32, i32) {
        let members: HashSet<usize> = t.members.iter().copied().collect();
        for pass in 0..2 {
            for w in &t.windows {
                let mine = members.contains(&w.hwnd);
                if (pass == 0 && !mine) || (pass == 1 && (mine || !w.is_fragile())) {
                    continue;
                }
                let r = win::window_rect(w.hwnd);
                if !r.contains(sx, sy) {
                    continue;
                }
                if !win::is_enabled(w.hwnd) {
                    return (0, 0, 0); // a disabled top-level would not get real input either
                }
                return deepest_child(w.hwnd, sx, sy);
            }
        }
        (0, 0, 0)
    }

    fn key_target(&self, t: &Target) -> usize {
        let (focus, _, _) = win::thread_focus(t.thread_id);
        if focus != 0 && win::is_window(focus) {
            let root = win::root_of(focus);
            if t.members.contains(&root) || t.members.is_empty() {
                return focus;
            }
            // focus is in another editor of the same thread: a popup menu of ours still wins
            if win::class_name(root) == "#32768" {
                return focus;
            }
        }
        if t.focus_target != 0 {
            return t.focus_target;
        }
        t.root
    }

    pub fn buttons(&self) -> u32 {
        self.buttons
    }
    pub fn mods(&self) -> u32 {
        self.mods
    }
    pub fn set_buttons(&mut self, b: u32) {
        self.buttons = b;
    }
    pub fn key_target_pub(&self, t: &Target) -> usize {
        self.key_target(t)
    }

    fn hand_over(&mut self, h: usize) {
        if self.hover != 0 && self.hover != h && win::is_window(self.hover) {
            post(self.hover, WM_MOUSELEAVE, 0, 0);
        }
        self.hover = h;
    }

    fn post_key(&mut self, h: usize, vk: u16, ext: bool, down: bool) {
        let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) };
        let mut lp: u32 = 1 | (scan << 16) | if ext { 1 << 24 } else { 0 };
        let generic = generic_modifier(vk);
        let msg;
        if generic == VK_MENU {
            self.alt = down;
            msg = if down { WM_SYSKEYDOWN } else { WM_KEYUP };
            if down {
                lp |= 1 << 29;
            }
        } else if self.alt {
            msg = if down { WM_SYSKEYDOWN } else { WM_SYSKEYUP };
            lp |= 1 << 29;
        } else {
            msg = if down { WM_KEYDOWN } else { WM_KEYUP };
        }
        if !down {
            lp |= (1 << 30) | (1 << 31);
        }
        match generic {
            VK_SHIFT => self.mods = if down { self.mods | MK_SHIFT } else { self.mods & !MK_SHIFT },
            VK_CONTROL => self.mods = if down { self.mods | MK_CONTROL } else { self.mods & !MK_CONTROL },
            _ => {}
        }
        // post the generic VK (VK_SHIFT, not VK_LSHIFT): wx and the patched KiCad key on it
        post(h, msg, generic as usize, lp as i32 as isize);
        self.posted += 1;
        if down {
            self.held.insert(vk);
            if ext {
                self.held_ext.insert(vk);
            }
        } else {
            self.held.remove(&vk);
            self.held_ext.remove(&vk);
        }
    }
}

impl Injector for EventInjector {
    fn name(&self) -> &'static str {
        "events"
    }

    fn mouse_move(&mut self, t: &Target, sx: i32, sy: i32, _mods: Modifiers) {
        self.last_pos = (sx, sy);
        let (h, cx, cy) = Self::hit_test(t, sx, sy);
        if h == 0 {
            self.misses += 1;
            return;
        }
        self.hand_over(h);
        post(h, WM_MOUSEMOVE, (self.buttons | self.mods) as usize, lparam_xy(cx, cy));
        self.posted += 1;
    }

    fn button(&mut self, t: &Target, sx: i32, sy: i32, button: MouseButton, down: bool, _mods: Modifiers) {
        self.last_pos = (sx, sy);
        let (h, cx, cy) = Self::hit_test(t, sx, sy);
        if h == 0 {
            self.misses += 1;
            return;
        }
        let (m_down, m_up, m_dbl) = msgs(button);
        let mk = mk_bit(button);
        let now = Instant::now();
        self.hand_over(h);
        let msg;
        if down {
            let key = mk;
            let is_dbl = self
                .last_down
                .get(&key)
                .map(|(t0, x, y)| now.duration_since(*t0).as_millis() <= self.dbl_ms && (x - sx).abs() <= self.dbl_w && (y - sy).abs() <= self.dbl_h)
                .unwrap_or(false);
            if is_dbl {
                msg = m_dbl;
                self.last_down.remove(&key);
            } else {
                msg = m_down;
                self.last_down.insert(key, (now, sx, sy));
            }
            self.buttons |= mk;
            // a real click is preceded by a move to the same spot; make the canvas see it too
            post(h, WM_MOUSEMOVE, (self.buttons | self.mods) as usize, lparam_xy(cx, cy));
        } else {
            msg = m_up;
            self.buttons &= !mk;
        }
        let mut wp = (self.buttons | self.mods) as usize;
        if matches!(button, MouseButton::X1 | MouseButton::X2) {
            wp |= (if button == MouseButton::X1 { 1usize } else { 2usize }) << 16;
        }
        post(h, msg, wp, lparam_xy(cx, cy));
        self.posted += 1;
    }

    fn wheel(&mut self, t: &Target, sx: i32, sy: i32, dx: i32, dy: i32, _mods: Modifiers) {
        self.last_pos = (sx, sy);
        let (h, _cx, _cy) = Self::hit_test(t, sx, sy);
        if h == 0 {
            self.misses += 1;
            return;
        }
        self.hand_over(h);
        let base = (self.buttons | self.mods) as usize;
        if dy != 0 {
            post(h, WM_MOUSEWHEEL, (((dy as i16 as u16 as u32) << 16) as usize) | base, lparam_xy(sx, sy));
        }
        if dx != 0 {
            post(h, WM_MOUSEHWHEEL, (((dx as i16 as u16 as u32) << 16) as usize) | base, lparam_xy(sx, sy));
        }
        self.posted += 1;
    }

    fn key(&mut self, t: &Target, code: &str, key: Option<&str>, down: bool, mods: Modifiers) -> bool {
        let Some((vk, ext)) = resolve_vk(code, key) else {
            self.unknown_keys += 1;
            return false;
        };
        let h = self.key_target(t);
        if vk == VK_ESCAPE && down && self.esc_close != EscClose::Never {
            let root = win::root_of(h);
            let close = match self.esc_close {
                EscClose::Always => is_owned_dialog(root),
                EscClose::Auto => is_owned_dialog(root) && win::class_name(root) == "#32770",
                EscClose::Never => false,
            };
            if close {
                post(root, WM_CLOSE, 0, 0);
                self.esc_closes += 1;
                self.posted += 1;
                return true;
            }
        }
        let wrap = if down && !is_modifier_vk(vk) { missing_modifiers(mods, &self.held) } else { Vec::new() };
        for m in &wrap {
            self.post_key(h, *m, false, true);
        }
        self.post_key(h, vk, ext, down);
        for m in wrap.iter().rev() {
            self.post_key(h, *m, false, false);
        }
        true
    }

    fn focus(&mut self, t: &Target, gained: bool) {
        if gained {
            return;
        }
        // release held keys and buttons so nothing sticks in the background window
        let h = self.key_target(t);
        let held: Vec<u16> = self.held.iter().copied().collect();
        for vk in held {
            let ext = self.held_ext.contains(&vk);
            self.post_key(h, vk, ext, false);
        }
        if self.buttons != 0 {
            let (sx, sy) = self.last_pos;
            for b in MouseButton::all() {
                if self.buttons & mk_bit(b) != 0 {
                    self.button(t, sx, sy, b, false, Modifiers::default());
                }
            }
        }
        self.alt = false;
    }

    fn stats(&self) -> serde_json::Value {
        serde_json::json!({
            "mode": "events",
            "posted": self.posted,
            "misses": self.misses,
            "buttons": self.buttons,
            "mods": self.mods,
            "held": self.held.len(),
            "esc_closes": self.esc_closes,
            "unknown_keys": self.unknown_keys,
        })
    }
}
