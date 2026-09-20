//! `hybrid`: posted input with a *borrowed* cursor for STOCK KiCad (winstream `HybridInjector`,
//! measured in win-multi.md 4). KiCad's canvas takes the position of a click, a wheel zoom or a
//! context menu from `wxGetMousePosition()` (the OS cursor), which is why plain posted clicks
//! select nothing. Here every button / wheel event teleports the real cursor to the target point,
//! delivers the message synchronously (`SendMessageTimeout`, so KiCad reads the cursor while it is
//! there) and puts the cursor back: the user's pointer stays over VS Code, no window is activated.
//! Pointer moves are only posted (enough for middle-drag pan). Keys are posted to the thread's
//! focus window; modifiers must be genuinely down (wx reads `GetKeyState`), so they go through
//! `SendInput` and also reach the real foreground app (known limit). A posted Esc on an owned
//! dialog becomes `WM_CLOSE` (stock wxDialog ignores a posted VK_ESCAPE). Context menus are the
//! one input that needs a real foreground flip (`TrackPopupMenu` closes in a background window).
//! Left over (DISPLAY.md 12.4): KiCad's own cursor warps (`Home`, `center_on_zoom`) move the
//! user's mouse; the window must exist on the desktop.

use windows::Win32::Foundation::{HWND, LPARAM, POINT, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    MapVirtualKeyW, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP, MAPVK_VK_TO_VSC, VIRTUAL_KEY,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, SendMessageTimeoutW, SetCursorPos, SMTO_ABORTIFHUNG, SMTO_BLOCK,
};

use super::events::{is_owned_dialog, post, EventInjector};
use super::{is_modifier_vk, lparam_xy, resolve_vk, Injector, MouseButton, Target, VK_ESCAPE};
use crate::hub::EscClose;
use crate::protocol::Modifiers;
use crate::win;

const SEND_TIMEOUT_MS: u32 = 250;

pub struct HybridInjector {
    inner: EventInjector,
    pub foreground_fixes: u64,
    pub timeouts: u64,
    real_mods: std::collections::HashSet<u16>,
}

impl HybridInjector {
    pub fn new() -> Self {
        HybridInjector {
            inner: EventInjector::new(EscClose::Always),
            foreground_fixes: 0,
            timeouts: 0,
            real_mods: std::collections::HashSet::new(),
        }
    }

    /// Teleport the cursor, deliver synchronously, restore.
    fn send_with_cursor(&mut self, h: usize, msg: u32, wp: usize, lp: isize, sx: i32, sy: i32) {
        unsafe {
            let mut o = POINT::default();
            let _ = GetCursorPos(&mut o);
            let _ = SetCursorPos(sx, sy);
            let mut res: usize = 0;
            let r = SendMessageTimeoutW(
                HWND(h as *mut _),
                msg,
                WPARAM(wp),
                LPARAM(lp),
                SMTO_ABORTIFHUNG | SMTO_BLOCK,
                SEND_TIMEOUT_MS,
                Some(&mut res),
            );
            if r.0 == 0 {
                self.timeouts += 1;
            }
            let _ = SetCursorPos(o.x, o.y);
        }
    }

    fn real_key(vk: u16, ext: bool, up: bool) {
        let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) } as u16;
        let mut flags = KEYBD_EVENT_FLAGS(0);
        if ext {
            flags |= KEYEVENTF_EXTENDEDKEY;
        }
        if up {
            flags |= KEYEVENTF_KEYUP;
        }
        let inp = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(vk), wScan: scan, dwFlags: flags, time: 0, dwExtraInfo: 0 } },
        };
        unsafe {
            SendInput(&[inp], std::mem::size_of::<INPUT>() as i32);
        }
    }
}

impl Default for HybridInjector {
    fn default() -> Self {
        Self::new()
    }
}

impl Injector for HybridInjector {
    fn name(&self) -> &'static str {
        "hybrid"
    }

    fn mouse_move(&mut self, t: &Target, sx: i32, sy: i32, mods: Modifiers) {
        self.inner.mouse_move(t, sx, sy, mods);
    }

    fn button(&mut self, t: &Target, sx: i32, sy: i32, button: MouseButton, down: bool, _mods: Modifiers) {
        let (h, cx, cy) = EventInjector::hit_test(t, sx, sy);
        if h == 0 {
            self.inner.misses += 1;
            return;
        }
        if button == MouseButton::Right && down {
            let fg = win::foreground();
            if fg == 0 || !t.members.contains(&win::root_of(fg)) {
                self.foreground_fixes += 1;
                win::bring_to_front(t.focus_target.max(t.root));
            }
        }
        let (m_down, m_up, _dbl) = match button {
            MouseButton::Left => (0x0201u32, 0x0202u32, 0x0203u32),
            MouseButton::Right => (0x0204, 0x0205, 0x0206),
            MouseButton::Middle => (0x0207, 0x0208, 0x0209),
            MouseButton::X1 | MouseButton::X2 => (0x020B, 0x020C, 0x020D),
        };
        let mk = match button {
            MouseButton::Left => 0x0001,
            MouseButton::Right => 0x0002,
            MouseButton::Middle => 0x0010,
            MouseButton::X1 => 0x0020,
            MouseButton::X2 => 0x0040,
        };
        // keep the inner injector's button/modifier state in sync (it owns `buttons`/`mods`)
        let wp = if down { self.inner_buttons() | mk } else { self.inner_buttons() & !mk } | self.inner_mods();
        self.set_inner_buttons(wp & 0x0073);
        if down {
            post(h, 0x0200, wp as usize, lparam_xy(cx, cy)); // WM_MOUSEMOVE first
        }
        self.send_with_cursor(h, if down { m_down } else { m_up }, wp as usize, lparam_xy(cx, cy), sx, sy);
        self.inner.posted += 1;
    }

    fn wheel(&mut self, t: &Target, sx: i32, sy: i32, dx: i32, dy: i32, _mods: Modifiers) {
        let (h, _cx, _cy) = EventInjector::hit_test(t, sx, sy);
        if h == 0 {
            self.inner.misses += 1;
            return;
        }
        let base = (self.inner_buttons() | self.inner_mods()) as usize;
        if dy != 0 {
            self.send_with_cursor(h, 0x020A, (((dy as i16 as u16 as u32) << 16) as usize) | base, lparam_xy(sx, sy), sx, sy);
        }
        if dx != 0 {
            self.send_with_cursor(h, 0x020E, (((dx as i16 as u16 as u32) << 16) as usize) | base, lparam_xy(sx, sy), sx, sy);
        }
        self.inner.posted += 1;
    }

    fn key(&mut self, t: &Target, code: &str, key: Option<&str>, down: bool, mods: Modifiers) -> bool {
        let Some((vk, ext)) = resolve_vk(code, key) else {
            self.inner.unknown_keys += 1;
            return false;
        };
        if is_modifier_vk(vk) {
            // stock wx reads GetKeyState: the modifier has to be genuinely down
            Self::real_key(vk, ext, !down);
            if down {
                self.real_mods.insert(vk);
            } else {
                self.real_mods.remove(&vk);
            }
            // also track it for the MK_* bits of posted mouse messages
            return self.inner.key(t, code, key, down, Modifiers::default());
        }
        if vk == VK_ESCAPE && down {
            let h = self.inner_key_target(t);
            let root = win::root_of(h);
            if is_owned_dialog(root) {
                post(root, 0x0010, 0, 0); // WM_CLOSE
                self.inner.esc_closes += 1;
                return true;
            }
        }
        // modifiers requested but not held by the client: press them for real around the key
        let extra = super::missing_modifiers(mods, &self.real_mods);
        for m in &extra {
            Self::real_key(*m, false, false);
        }
        let r = self.inner.key(t, code, key, down, Modifiers::default());
        for m in extra.iter().rev() {
            Self::real_key(*m, false, true);
        }
        r
    }

    fn focus(&mut self, t: &Target, gained: bool) {
        if !gained {
            let held: Vec<u16> = self.real_mods.drain().collect();
            for vk in held {
                Self::real_key(vk, false, true);
            }
        }
        self.inner.focus(t, gained);
    }

    fn stats(&self) -> serde_json::Value {
        let mut v = self.inner.stats();
        v["mode"] = serde_json::json!("hybrid");
        v["foreground_fixes"] = serde_json::json!(self.foreground_fixes);
        v["timeouts"] = serde_json::json!(self.timeouts);
        v
    }
}

// small accessors so the hybrid injector can share the event injector's state without exposing
// its fields publicly
impl HybridInjector {
    fn inner_buttons(&self) -> u32 {
        self.inner.buttons()
    }
    fn inner_mods(&self) -> u32 {
        self.inner.mods()
    }
    fn set_inner_buttons(&mut self, b: u32) {
        self.inner.set_buttons(b);
    }
    fn inner_key_target(&self, t: &Target) -> usize {
        self.inner.key_target_pub(t)
    }
}
