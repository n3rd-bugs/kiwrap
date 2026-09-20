//! Input injectors. Three modes of one launched session (docs/DISPLAY.md 12.6):
//! * `mirror` — real OS input through `SendInput` (KiCad on the desktop; the helper raises the
//!   window when the click point is obscured and makes it foreground for keys; held keys are
//!   released when the view loses focus);
//! * `events` — `PostMessage` only, for a KiCad built with `mr/display-input`
//!   (`KICAD_INPUT_FROM_EVENTS=1|cloak`): nothing touches the user's cursor, focus or keyboard;
//! * `hybrid` — posted input with a *borrowed* cursor for stock KiCad (win-multi.md 4), the opt-in
//!   mirror the plan calls "B".
//! Coordinates arrive in surface px and are turned into screen px by the server.

pub mod events;
pub mod hybrid;
pub mod mirror;

use crate::protocol::Modifiers;
use crate::surfaces::WinInfo;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MouseButton {
    Left,
    Middle,
    Right,
    X1,
    X2,
}

impl MouseButton {
    /// DOM `MouseEvent.button` numbering.
    pub fn from_dom(b: u32) -> Option<MouseButton> {
        match b {
            0 => Some(MouseButton::Left),
            1 => Some(MouseButton::Middle),
            2 => Some(MouseButton::Right),
            3 => Some(MouseButton::X1),
            4 => Some(MouseButton::X2),
            _ => None,
        }
    }
    /// DOM `MouseEvent.buttons` bit.
    pub fn dom_bit(&self) -> u32 {
        match self {
            MouseButton::Left => 1,
            MouseButton::Right => 2,
            MouseButton::Middle => 4,
            MouseButton::X1 => 8,
            MouseButton::X2 => 16,
        }
    }
    pub fn all() -> [MouseButton; 5] {
        [MouseButton::Left, MouseButton::Right, MouseButton::Middle, MouseButton::X1, MouseButton::X2]
    }
}

/// Where an event goes: the surface it was sent on, resolved by the server at dispatch time.
#[derive(Clone, Debug, Default)]
#[allow(dead_code)] // surface_id/pid carried for injector diagnostics
pub struct Target {
    pub surface_id: String,
    pub root: usize,
    /// Every top-level HWND of this surface (frame + owned dialogs + popups), z-order.
    pub members: Vec<usize>,
    /// Topmost enabled member (a modal dialog before its disabled frame).
    pub focus_target: usize,
    pub thread_id: u32,
    pub pid: u32,
    /// All visible top-level windows of the process (z-order), for hit-testing.
    pub windows: Vec<WinInfo>,
}

pub trait Injector: Send {
    #[allow(dead_code)]
    fn name(&self) -> &'static str;
    fn mouse_move(&mut self, t: &Target, sx: i32, sy: i32, mods: Modifiers);
    fn button(&mut self, t: &Target, sx: i32, sy: i32, button: MouseButton, down: bool, mods: Modifiers);
    fn wheel(&mut self, t: &Target, sx: i32, sy: i32, dx: i32, dy: i32, mods: Modifiers);
    /// Returns false when the key code is unknown.
    fn key(&mut self, t: &Target, code: &str, key: Option<&str>, down: bool, mods: Modifiers) -> bool;
    /// The view gained (true) or lost (false) keyboard focus.
    fn focus(&mut self, t: &Target, gained: bool);
    fn stats(&self) -> serde_json::Value;
}

/// Input is ignored (capture-only view).
pub struct NoInjector {
    pub dropped: u64,
}

impl Injector for NoInjector {
    fn name(&self) -> &'static str {
        "none"
    }
    fn mouse_move(&mut self, _: &Target, _: i32, _: i32, _: Modifiers) {
        self.dropped += 1;
    }
    fn button(&mut self, _: &Target, _: i32, _: i32, _: MouseButton, _: bool, _: Modifiers) {
        self.dropped += 1;
    }
    fn wheel(&mut self, _: &Target, _: i32, _: i32, _: i32, _: i32, _: Modifiers) {
        self.dropped += 1;
    }
    fn key(&mut self, _: &Target, _: &str, _: Option<&str>, _: bool, _: Modifiers) -> bool {
        self.dropped += 1;
        true
    }
    fn focus(&mut self, _: &Target, _: bool) {}
    fn stats(&self) -> serde_json::Value {
        serde_json::json!({"mode": "none", "dropped": self.dropped})
    }
}

// ------------------------------------------------------------------ virtual keys
pub const VK_SHIFT: u16 = 0x10;
pub const VK_CONTROL: u16 = 0x11;
pub const VK_MENU: u16 = 0x12;
pub const VK_ESCAPE: u16 = 0x1B;
pub const VK_LSHIFT: u16 = 0xA0;
pub const VK_RSHIFT: u16 = 0xA1;
pub const VK_LCONTROL: u16 = 0xA2;
pub const VK_RCONTROL: u16 = 0xA3;
pub const VK_LMENU: u16 = 0xA4;
pub const VK_RMENU: u16 = 0xA5;
pub const VK_LWIN: u16 = 0x5B;
pub const VK_RWIN: u16 = 0x5C;

/// `KeyboardEvent.code` -> (VK, extended-key flag). Letters/digits map 1:1 to ASCII VKs.
/// Table from the winstream prototype (`inject.py`), verified for R/Delete/Ctrl+Z/Esc/Home/A/E,
/// text entry and Ctrl+C.
pub fn vk_for_code(code: &str) -> Option<(u16, bool)> {
    let v = match code {
        "Escape" => (0x1B, false),
        "Tab" => (0x09, false),
        "Enter" => (0x0D, false),
        "NumpadEnter" => (0x0D, true),
        "Backspace" => (0x08, false),
        "Space" => (0x20, false),
        "Delete" => (0x2E, true),
        "Insert" => (0x2D, true),
        "Home" => (0x24, true),
        "End" => (0x23, true),
        "PageUp" => (0x21, true),
        "PageDown" => (0x22, true),
        "ArrowLeft" => (0x25, true),
        "ArrowUp" => (0x26, true),
        "ArrowRight" => (0x27, true),
        "ArrowDown" => (0x28, true),
        "ShiftLeft" => (VK_LSHIFT, false),
        "ShiftRight" => (VK_RSHIFT, true),
        "ControlLeft" => (VK_LCONTROL, false),
        "ControlRight" => (VK_RCONTROL, true),
        "AltLeft" => (VK_LMENU, false),
        "AltRight" => (VK_RMENU, true),
        "MetaLeft" | "OSLeft" => (VK_LWIN, true),
        "MetaRight" | "OSRight" => (VK_RWIN, true),
        "CapsLock" => (0x14, false),
        "NumLock" => (0x90, true),
        "ScrollLock" => (0x91, false),
        "Pause" => (0x13, false),
        "Minus" => (0xBD, false),
        "Equal" => (0xBB, false),
        "BracketLeft" => (0xDB, false),
        "BracketRight" => (0xDD, false),
        "Backslash" => (0xDC, false),
        "IntlBackslash" => (0xE2, false),
        "Semicolon" => (0xBA, false),
        "Quote" => (0xDE, false),
        "Comma" => (0xBC, false),
        "Period" => (0xBE, false),
        "Slash" => (0xBF, false),
        "Backquote" => (0xC0, false),
        "NumpadAdd" => (0x6B, false),
        "NumpadSubtract" => (0x6D, false),
        "NumpadMultiply" => (0x6A, false),
        "NumpadDivide" => (0x6F, true),
        "NumpadDecimal" => (0x6E, false),
        "PrintScreen" => (0x2C, true),
        "ContextMenu" => (0x5D, true),
        _ => {
            if let Some(n) = code.strip_prefix('F').and_then(|s| s.parse::<u16>().ok()) {
                if (1..=24).contains(&n) {
                    return Some((0x70 + n - 1, false));
                }
                return None;
            }
            if let Some(c) = code.strip_prefix("Key") {
                let b = c.as_bytes();
                if b.len() == 1 && b[0].is_ascii_uppercase() {
                    return Some((b[0] as u16, false));
                }
                return None;
            }
            if let Some(d) = code.strip_prefix("Digit") {
                let b = d.as_bytes();
                if b.len() == 1 && b[0].is_ascii_digit() {
                    return Some((b[0] as u16, false));
                }
                return None;
            }
            if let Some(d) = code.strip_prefix("Numpad") {
                let b = d.as_bytes();
                if b.len() == 1 && b[0].is_ascii_digit() {
                    return Some((0x60 + (b[0] - b'0') as u16, false));
                }
                return None;
            }
            return None;
        }
    };
    Some(v)
}

/// Fallback from `KeyboardEvent.key` when `code` is unknown or missing (single printable chars,
/// a few names).
pub fn vk_for_key(key: &str) -> Option<(u16, bool)> {
    let mut chars = key.chars();
    let (c, rest) = (chars.next()?, chars.next());
    if rest.is_none() {
        let u = c.to_ascii_uppercase();
        if u.is_ascii_alphanumeric() {
            return Some((u as u16, false));
        }
        return match c {
            ' ' => Some((0x20, false)),
            '-' => Some((0xBD, false)),
            '=' | '+' => Some((0xBB, false)),
            '[' | '{' => Some((0xDB, false)),
            ']' | '}' => Some((0xDD, false)),
            '\\' | '|' => Some((0xDC, false)),
            ';' | ':' => Some((0xBA, false)),
            '\'' | '"' => Some((0xDE, false)),
            ',' | '<' => Some((0xBC, false)),
            '.' | '>' => Some((0xBE, false)),
            '/' | '?' => Some((0xBF, false)),
            '`' | '~' => Some((0xC0, false)),
            _ => None,
        };
    }
    match key {
        "Escape" | "Esc" => Some((0x1B, false)),
        "Enter" => Some((0x0D, false)),
        "Tab" => Some((0x09, false)),
        "Backspace" => Some((0x08, false)),
        "Delete" => Some((0x2E, true)),
        "Home" => Some((0x24, true)),
        "End" => Some((0x23, true)),
        "Shift" => Some((VK_LSHIFT, false)),
        "Control" => Some((VK_LCONTROL, false)),
        "Alt" => Some((VK_LMENU, false)),
        _ => vk_for_code(key),
    }
}

pub fn resolve_vk(code: &str, key: Option<&str>) -> Option<(u16, bool)> {
    vk_for_code(code).or_else(|| key.and_then(vk_for_key))
}

pub fn is_modifier_vk(vk: u16) -> bool {
    matches!(vk, VK_SHIFT | VK_CONTROL | VK_MENU | VK_LSHIFT | VK_RSHIFT | VK_LCONTROL | VK_RCONTROL | VK_LMENU | VK_RMENU)
}

/// L/R modifier -> generic VK (wx and the patched KiCad key on VK_SHIFT/VK_CONTROL/VK_MENU).
pub fn generic_modifier(vk: u16) -> u16 {
    match vk {
        VK_LSHIFT | VK_RSHIFT => VK_SHIFT,
        VK_LCONTROL | VK_RCONTROL => VK_CONTROL,
        VK_LMENU | VK_RMENU => VK_MENU,
        v => v,
    }
}

/// Modifier VKs the `modifiers` field asks for that the client did not press itself (`held`):
/// the injector wraps the key with a press/release of those so `Ctrl+Z` works for clients that
/// only send the letter with `modifiers:{ctrl:true}`.
pub fn missing_modifiers(mods: Modifiers, held: &std::collections::HashSet<u16>) -> Vec<u16> {
    let mut v = Vec::new();
    let has = |g: u16| held.iter().any(|h| generic_modifier(*h) == g);
    if mods.ctrl && !has(VK_CONTROL) {
        v.push(VK_LCONTROL);
    }
    if mods.shift && !has(VK_SHIFT) {
        v.push(VK_LSHIFT);
    }
    if mods.alt && !has(VK_MENU) {
        v.push(VK_LMENU);
    }
    v
}

pub fn lparam_xy(x: i32, y: i32) -> isize {
    (((y as u16 as u32) << 16) | (x as u16 as u32)) as i32 as isize
}

/// Deepest visible, enabled child of `top` under the screen point (`ChildWindowFromPointEx`
/// walk, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED), with client coordinates.
pub fn deepest_child(top: usize, sx: i32, sy: i32) -> (usize, i32, i32) {
    use windows::Win32::Foundation::POINT;
    use windows::Win32::UI::WindowsAndMessaging::{ChildWindowFromPointEx, CWP_SKIPDISABLED, CWP_SKIPINVISIBLE};
    let mut h = top;
    for _ in 0..32 {
        let (cx, cy) = crate::win::screen_to_client(h, sx, sy);
        let c = unsafe { ChildWindowFromPointEx(crate::win::hwnd(h), POINT { x: cx, y: cy }, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED) };
        let c = c.0 as usize;
        if c == 0 || c == h {
            break;
        }
        h = c;
    }
    let (cx, cy) = crate::win::screen_to_client(h, sx, sy);
    (h, cx, cy)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_table_matches_the_prototype() {
        assert_eq!(vk_for_code("KeyR"), Some((0x52, false)));
        assert_eq!(vk_for_code("Digit5"), Some((0x35, false)));
        assert_eq!(vk_for_code("Numpad5"), Some((0x65, false)));
        assert_eq!(vk_for_code("Delete"), Some((0x2E, true)));
        assert_eq!(vk_for_code("Home"), Some((0x24, true)));
        assert_eq!(vk_for_code("F12"), Some((0x7B, false)));
        assert_eq!(vk_for_code("F25"), None);
        assert_eq!(vk_for_code("ControlRight"), Some((0xA3, true)));
        assert_eq!(vk_for_code("Nope"), None);
        assert_eq!(vk_for_code("Keyr"), None);
    }

    #[test]
    fn key_fallback() {
        assert_eq!(resolve_vk("", Some("r")), Some((0x52, false)));
        assert_eq!(resolve_vk("Unknown", Some("Escape")), Some((0x1B, false)));
        assert_eq!(resolve_vk("KeyZ", Some("z")), Some((0x5A, false)));
        assert_eq!(resolve_vk("", Some("?")), Some((0xBF, false)));
        assert_eq!(resolve_vk("", None), None);
    }

    #[test]
    fn modifier_helpers() {
        assert!(is_modifier_vk(VK_LCONTROL));
        assert!(!is_modifier_vk(0x52));
        assert_eq!(generic_modifier(VK_RSHIFT), VK_SHIFT);
        let mut held = std::collections::HashSet::new();
        assert_eq!(missing_modifiers(Modifiers { ctrl: true, shift: true, alt: false, meta: false }, &held), vec![VK_LCONTROL, VK_LSHIFT]);
        held.insert(VK_RCONTROL);
        assert_eq!(missing_modifiers(Modifiers { ctrl: true, shift: false, alt: false, meta: false }, &held), Vec::<u16>::new());
    }

    #[test]
    fn lparam_packs_negative_coordinates() {
        assert_eq!(lparam_xy(10, 20) as u32, 0x0014_000A);
        assert_eq!(lparam_xy(-1, 5) as u32, 0x0005_FFFF);
    }

    #[test]
    fn dom_buttons() {
        assert_eq!(MouseButton::from_dom(2), Some(MouseButton::Right));
        assert_eq!(MouseButton::Middle.dom_bit(), 4);
        assert_eq!(MouseButton::from_dom(9), None);
    }
}
