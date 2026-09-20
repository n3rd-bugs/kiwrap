//! `mirror`: real OS input through `SendInput` (winstream `SendInputInjector`). Mouse goes to
//! whatever is under the (moved) cursor, so dialogs, menus and the chooser work for free; keys go
//! to the foreground window, so the surface is raised first when it is not. Measured limits
//! (winstream.md 2b): the KiCad window must be on-screen and unobscured at the click point; every
//! injected move moves the user's real cursor; KiCad takes the foreground.
//! Foreground handling: raise-if-obscured before a button/wheel (`WindowFromPoint` root not in the
//! surface), raise-if-not-foreground before a key, release every held key on blur.
//! Containment (reviewer finding 3, 2026-09-20): `SendInput` is real OS input, so a click or key
//! is only sent when, after the raise attempt, the window under the point (for keys: the
//! foreground root) belongs to the surface; otherwise the event is dropped and counted
//! (`dropped_obscured` / `dropped_foreground`) rather than delivered to whatever else is there.
//! The server already drops events whose coordinates fall outside the surface rect.

use std::collections::HashSet;

use windows::Win32::UI::Input::KeyboardAndMouse::{
    MapVirtualKeyW, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT, KEYBD_EVENT_FLAGS,
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, MAPVK_VK_TO_VSC, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL, MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP, MOUSEINPUT, MOUSE_EVENT_FLAGS, VIRTUAL_KEY,
};
use windows::Win32::Foundation::POINT;
use windows::Win32::UI::WindowsAndMessaging::WindowFromPoint;

use super::{is_modifier_vk, missing_modifiers, resolve_vk, Injector, MouseButton, Target};
use crate::protocol::Modifiers;
use crate::surfaces::Rect;
use crate::win;

pub struct MirrorInjector {
    vscreen: Rect,
    held: HashSet<u16>,
    held_ext: HashSet<u16>,
    pub foreground_fixes: u64,
    pub obscured_fixes: u64,
    pub dropped_obscured: u64,
    pub dropped_foreground: u64,
    pub sent: u64,
    pub unknown_keys: u64,
}

impl MirrorInjector {
    pub fn new() -> Self {
        MirrorInjector {
            vscreen: win::virtual_screen(),
            held: HashSet::new(),
            held_ext: HashSet::new(),
            foreground_fixes: 0,
            obscured_fixes: 0,
            dropped_obscured: 0,
            dropped_foreground: 0,
            sent: 0,
            unknown_keys: 0,
        }
    }

    /// MOUSEEVENTF_ABSOLUTE | VIRTUALDESK: 0..65535 across the virtual screen (pixel centre).
    /// Measured mapping error 0 px at four probe points (winstream.md 2d).
    fn abs(&self, sx: i32, sy: i32) -> (i32, i32) {
        let v = self.vscreen;
        let nx = ((sx - v.l) as f64 + 0.5) * 65535.0 / v.w().max(1) as f64;
        let ny = ((sy - v.t) as f64 + 0.5) * 65535.0 / v.h().max(1) as f64;
        (nx.clamp(0.0, 65535.0) as i32, ny.clamp(0.0, 65535.0) as i32)
    }

    fn mouse(&self, dx: i32, dy: i32, flags: MOUSE_EVENT_FLAGS, data: i32) -> INPUT {
        INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT { dx, dy, mouseData: data as u32, dwFlags: flags, time: 0, dwExtraInfo: 0 },
            },
        }
    }

    fn keybd(vk: u16, ext: bool, up: bool) -> INPUT {
        let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) } as u16;
        let mut flags = KEYBD_EVENT_FLAGS(0);
        if ext {
            flags |= KEYEVENTF_EXTENDEDKEY;
        }
        if up {
            flags |= KEYEVENTF_KEYUP;
        }
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(vk), wScan: scan, dwFlags: flags, time: 0, dwExtraInfo: 0 } },
        }
    }

    fn send(&mut self, inputs: &[INPUT]) {
        let n = unsafe { SendInput(inputs, std::mem::size_of::<INPUT>() as i32) };
        self.sent += n as u64;
    }

    /// True when the foreground root belongs to the surface (after one raise attempt when it
    /// did not): keys go to the foreground window, so they are sent only then.
    fn ensure_foreground(&mut self, t: &Target) -> bool {
        let owned = |fg: usize| fg != 0 && t.members.contains(&win::root_of(fg));
        if owned(win::foreground()) {
            return true;
        }
        self.foreground_fixes += 1;
        win::bring_to_front(t.focus_target.max(t.root));
        owned(win::foreground())
    }

    /// `SendInput` hits whatever HWND is under the point: if another window (another KiCad
    /// frame of the same process or a foreign app such as VS Code) overlaps this surface there,
    /// raise it first. True when the root under the point belongs to the surface afterwards;
    /// false (the raise was refused - foreground-lock rules - or the point is off the window)
    /// means the click must NOT be sent (it would land in the other app).
    fn ensure_unobscured(&mut self, t: &Target, sx: i32, sy: i32) -> bool {
        let under = |sx: i32, sy: i32| {
            let h = unsafe { WindowFromPoint(POINT { x: sx, y: sy }) }.0 as usize;
            if h != 0 {
                win::root_of(h)
            } else {
                0
            }
        };
        let top = under(sx, sy);
        if top != 0 && t.members.contains(&top) {
            return true;
        }
        self.obscured_fixes += 1;
        win::bring_to_front(t.focus_target.max(t.root));
        let top = under(sx, sy);
        top != 0 && t.members.contains(&top)
    }

    fn release_all(&mut self) {
        let held: Vec<u16> = self.held.drain().collect();
        for vk in held {
            let ext = self.held_ext.contains(&vk);
            let inp = Self::keybd(vk, ext, true);
            self.send(&[inp]);
        }
        self.held_ext.clear();
    }
}

impl Default for MirrorInjector {
    fn default() -> Self {
        Self::new()
    }
}

fn button_flags(b: MouseButton, down: bool) -> (MOUSE_EVENT_FLAGS, i32) {
    match (b, down) {
        (MouseButton::Left, true) => (MOUSEEVENTF_LEFTDOWN, 0),
        (MouseButton::Left, false) => (MOUSEEVENTF_LEFTUP, 0),
        (MouseButton::Middle, true) => (MOUSEEVENTF_MIDDLEDOWN, 0),
        (MouseButton::Middle, false) => (MOUSEEVENTF_MIDDLEUP, 0),
        (MouseButton::Right, true) => (MOUSEEVENTF_RIGHTDOWN, 0),
        (MouseButton::Right, false) => (MOUSEEVENTF_RIGHTUP, 0),
        (MouseButton::X1, true) => (MOUSEEVENTF_XDOWN, 1),
        (MouseButton::X1, false) => (MOUSEEVENTF_XUP, 1),
        (MouseButton::X2, true) => (MOUSEEVENTF_XDOWN, 2),
        (MouseButton::X2, false) => (MOUSEEVENTF_XUP, 2),
    }
}

impl Injector for MirrorInjector {
    fn name(&self) -> &'static str {
        "mirror"
    }

    fn mouse_move(&mut self, _t: &Target, sx: i32, sy: i32, _mods: Modifiers) {
        let (nx, ny) = self.abs(sx, sy);
        let inp = self.mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0);
        self.send(&[inp]);
    }

    fn button(&mut self, t: &Target, sx: i32, sy: i32, button: MouseButton, down: bool, _mods: Modifiers) {
        // a release is always sent (a stuck button is worse than a release landing elsewhere);
        // a press only into the surface's own windows
        if down && !self.ensure_unobscured(t, sx, sy) {
            self.dropped_obscured += 1;
            log::debug!("[mirror] {button:?} down at ({sx},{sy}) dropped: the surface is not under the point");
            return;
        }
        let (nx, ny) = self.abs(sx, sy);
        let (flag, data) = button_flags(button, down);
        let a = self.mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0);
        let b = self.mouse(nx, ny, flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, data);
        self.send(&[a, b]);
    }

    fn wheel(&mut self, t: &Target, sx: i32, sy: i32, dx: i32, dy: i32, _mods: Modifiers) {
        if !self.ensure_unobscured(t, sx, sy) {
            self.dropped_obscured += 1;
            return;
        }
        let (nx, ny) = self.abs(sx, sy);
        let mut v = vec![self.mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0)];
        if dy != 0 {
            v.push(self.mouse(0, 0, MOUSEEVENTF_WHEEL, dy));
        }
        if dx != 0 {
            v.push(self.mouse(0, 0, MOUSEEVENTF_HWHEEL, dx));
        }
        self.send(&v);
    }

    fn key(&mut self, t: &Target, code: &str, key: Option<&str>, down: bool, mods: Modifiers) -> bool {
        let Some((vk, ext)) = resolve_vk(code, key) else {
            self.unknown_keys += 1;
            return false;
        };
        if !self.ensure_foreground(t) {
            // a key up for something we hold must still be released, but nothing new is typed
            // into whatever window has the foreground
            if down || !self.held.contains(&vk) {
                self.dropped_foreground += 1;
                return true;
            }
        }
        let wrap = if down && !is_modifier_vk(vk) { missing_modifiers(mods, &self.held) } else { Vec::new() };
        let mut v = Vec::new();
        for m in &wrap {
            v.push(Self::keybd(*m, false, false));
        }
        v.push(Self::keybd(vk, ext, !down));
        for m in wrap.iter().rev() {
            v.push(Self::keybd(*m, false, true));
        }
        self.send(&v);
        if down {
            self.held.insert(vk);
            if ext {
                self.held_ext.insert(vk);
            }
        } else {
            self.held.remove(&vk);
            self.held_ext.remove(&vk);
        }
        true
    }

    fn focus(&mut self, _t: &Target, gained: bool) {
        if !gained {
            self.release_all();
        }
    }

    fn stats(&self) -> serde_json::Value {
        serde_json::json!({
            "mode": "mirror",
            "sent": self.sent,
            "foreground_fixes": self.foreground_fixes,
            "obscured_fixes": self.obscured_fixes,
            "dropped_obscured": self.dropped_obscured,
            "dropped_foreground": self.dropped_foreground,
            "held": self.held.len(),
            "unknown_keys": self.unknown_keys,
        })
    }
}
