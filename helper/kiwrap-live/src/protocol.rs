//! Wire types of the helper protocol (contract 1 of the Phase-2 plan).
//!
//! * stdout, once: `{"port": <u16>, "token": "<t>", "version": "<semver>"}` (+ `pid`).
//! * `ws://127.0.0.1:<port>/control?token=<t>` — JSON text frames both ways:
//!   client `{type:"surfaces"}` -> `{type:"surfaces", surfaces:[SurfaceInfo]}`;
//!   client `{type:"screenshot", surface, max_px}` -> `{type:"screenshot", surface, w, h, png}`;
//!   client `{type:"raise", surface}` -> `{type:"raise", surface, ok}`;
//!   server pushes `{type:"event", event:"create"|"destroy"|"title"|"rect"|"process_exit", ...}`.
//!   Every request may carry an `id`, echoed on the reply.
//! * `ws://127.0.0.1:<port>/stream?surface=<id>&token=<t>` — binary frames
//!   `[u32 frame_id][u16 w][u16 h]` (little endian) + JPEG; a JSON text frame
//!   `{type:"meta", w, h, dpi_scale, dirty:[[x,y,w,h]...]}` precedes every size change; the client
//!   sends JSON input events `{type:"mouse"|"wheel"|"key"|"focus", ...}`.

use serde::{Deserialize, Serialize};

use crate::surfaces::{DialogInfo, Kind, Surface};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const FRAME_HEADER_LEN: usize = 8;

#[derive(Serialize)]
pub struct Hello<'a> {
    pub port: u16,
    pub token: &'a str,
    pub version: &'static str,
    pub pid: u32,
    pub helper_pid: u32,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct SurfaceInfo {
    pub id: String,
    pub kind: Kind,
    pub title: String,
    pub hwnd: usize,
    /// Screen rect of the streamed image [l, t, r, b], physical px.
    pub rect_px: [i32; 4],
    /// The editor frame's own rect (== rect_px unless a dialog sticks out).
    pub frame_px: [i32; 4],
    pub dpi: u32,
    pub cloaked: bool,
    pub cloak_flags: u32,
    pub enabled: bool,
    pub foreground: bool,
    pub dialogs: Vec<DialogInfo>,
}

impl SurfaceInfo {
    pub fn from_surface(id: &str, s: &Surface, dpi: u32, foreground_hwnd: usize) -> Self {
        SurfaceInfo {
            id: id.to_string(),
            kind: s.kind,
            title: s.title.clone(),
            hwnd: s.root,
            rect_px: s.rect.as_array(),
            frame_px: s.root_rect.as_array(),
            dpi,
            cloaked: s.cloaked != 0,
            cloak_flags: s.cloaked,
            enabled: s.enabled,
            foreground: foreground_hwnd != 0 && s.members.iter().any(|m| m.hwnd == foreground_hwnd),
            dialogs: s.dialogs.clone(),
        }
    }
}

/// Pushed on the control channel.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlPush {
    Event {
        event: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        surface: Option<SurfaceInfo>,
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
}

#[derive(Deserialize, Debug)]
pub struct ControlRequest {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub id: Option<serde_json::Value>,
    #[serde(default)]
    pub surface: Option<String>,
    #[serde(default)]
    pub max_px: Option<u32>,
    /// screenshot only: "png" (default) or "jpeg".
    #[serde(default)]
    pub format: Option<String>,
    #[serde(default)]
    pub quality: Option<u32>,
}

/// Stream meta text frame, sent before a frame whose size differs from the previous one.
#[derive(Serialize, Debug)]
pub struct Meta {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub w: u32,
    pub h: u32,
    pub dpi_scale: f32,
    pub dirty: Vec<[i32; 4]>,
    pub frame_id: u32,
    pub surface: String,
    pub rect_px: [i32; 4],
}

/// Input event from a stream client (surface px).
#[derive(Deserialize, Debug, Default, Clone)]
pub struct InputEvent {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub x: Option<f64>,
    #[serde(default)]
    pub y: Option<f64>,
    /// DOM `MouseEvent.buttons` bitmask after the event (1 left, 2 right, 4 middle).
    #[serde(default)]
    pub buttons: Option<u32>,
    /// DOM `MouseEvent.button` (0 left, 1 middle, 2 right) of the button that changed.
    #[serde(default)]
    pub button: Option<u32>,
    #[serde(default)]
    pub dx: Option<f64>,
    #[serde(default)]
    pub dy: Option<f64>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub code: Option<String>,
    #[serde(default)]
    pub modifiers: Option<serde_json::Value>,
    #[serde(default)]
    pub down: Option<bool>,
    /// Latency probe id echoed in the next frame's meta (benchmarks).
    #[serde(default)]
    pub probe: Option<u32>,
    #[serde(default)]
    pub id: Option<serde_json::Value>,
    /// `resize`: the client's viewport in physical px (the frame is sized to it with --fit-to-view).
    #[serde(default)]
    pub w: Option<f64>,
    #[serde(default)]
    pub h: Option<f64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Modifiers {
    pub ctrl: bool,
    pub shift: bool,
    pub alt: bool,
    pub meta: bool,
}

impl Modifiers {
    /// Accepts `{ctrl, shift, alt, meta}` booleans (also `ctrlKey`... DOM names), a list of
    /// names (`["Control","Shift"]`), or a bitmask (1 ctrl, 2 shift, 4 alt, 8 meta).
    pub fn parse(v: &Option<serde_json::Value>) -> Modifiers {
        let mut m = Modifiers::default();
        let Some(v) = v else { return m };
        match v {
            serde_json::Value::Object(o) => {
                let g = |k: &str, k2: &str| {
                    o.get(k).or_else(|| o.get(k2)).and_then(|x| x.as_bool()).unwrap_or(false)
                };
                m.ctrl = g("ctrl", "ctrlKey");
                m.shift = g("shift", "shiftKey");
                m.alt = g("alt", "altKey");
                m.meta = g("meta", "metaKey");
            }
            serde_json::Value::Array(a) => {
                for x in a {
                    match x.as_str().map(|s| s.to_ascii_lowercase()).as_deref() {
                        Some("ctrl") | Some("control") => m.ctrl = true,
                        Some("shift") => m.shift = true,
                        Some("alt") => m.alt = true,
                        Some("meta") => m.meta = true,
                        _ => {}
                    }
                }
            }
            serde_json::Value::Number(n) => {
                let b = n.as_u64().unwrap_or(0);
                m.ctrl = b & 1 != 0;
                m.shift = b & 2 != 0;
                m.alt = b & 4 != 0;
                m.meta = b & 8 != 0;
            }
            _ => {}
        }
        m
    }
}

pub fn frame_header(frame_id: u32, w: u32, h: u32) -> [u8; FRAME_HEADER_LEN] {
    let mut b = [0u8; FRAME_HEADER_LEN];
    b[0..4].copy_from_slice(&frame_id.to_le_bytes());
    b[4..6].copy_from_slice(&(w.min(65535) as u16).to_le_bytes());
    b[6..8].copy_from_slice(&(h.min(65535) as u16).to_le_bytes());
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_is_little_endian_u32_u16_u16() {
        let h = frame_header(0x01020304, 1920, 1080);
        assert_eq!(h, [0x04, 0x03, 0x02, 0x01, 0x80, 0x07, 0x38, 0x04]);
    }

    #[test]
    fn modifiers_parse_every_shape() {
        let o: serde_json::Value = serde_json::from_str(r#"{"ctrl":true,"shiftKey":true}"#).unwrap();
        assert_eq!(Modifiers::parse(&Some(o)), Modifiers { ctrl: true, shift: true, alt: false, meta: false });
        let a: serde_json::Value = serde_json::from_str(r#"["Control","Alt"]"#).unwrap();
        assert_eq!(Modifiers::parse(&Some(a)), Modifiers { ctrl: true, shift: false, alt: true, meta: false });
        let n: serde_json::Value = serde_json::from_str("6").unwrap();
        assert_eq!(Modifiers::parse(&Some(n)), Modifiers { ctrl: false, shift: true, alt: true, meta: false });
        assert_eq!(Modifiers::parse(&None), Modifiers::default());
    }

    #[test]
    fn input_event_deserialises_contract_fields() {
        let e: InputEvent = serde_json::from_str(
            r#"{"type":"mouse","x":10.5,"y":20,"buttons":1,"button":0,"down":true,"modifiers":{"ctrl":false}}"#,
        )
        .unwrap();
        assert_eq!(e.kind, "mouse");
        assert_eq!(e.buttons, Some(1));
        assert_eq!(e.down, Some(true));
        let k: InputEvent = serde_json::from_str(r#"{"type":"key","code":"KeyR","key":"r","down":true}"#).unwrap();
        assert_eq!(k.code.as_deref(), Some("KeyR"));
    }

    #[test]
    fn control_request_parses() {
        let r: ControlRequest = serde_json::from_str(r#"{"type":"screenshot","surface":"pcb","max_px":1024,"id":7}"#).unwrap();
        assert_eq!(r.kind, "screenshot");
        assert_eq!(r.max_px, Some(1024));
        assert_eq!(r.id, Some(serde_json::json!(7)));
    }

    #[test]
    fn surface_info_serialises_kind_names() {
        let s = crate::surfaces::Surface {
            root: 5,
            kind: Kind::ThreeD,
            title: "x — 3D Viewer".into(),
            root_rect: crate::surfaces::Rect::new(0, 0, 10, 10),
            rect: crate::surfaces::Rect::new(0, 0, 10, 10),
            cloaked: 1,
            enabled: true,
            thread_id: 1,
            members: vec![],
            dialogs: vec![],
        };
        let info = SurfaceInfo::from_surface("3d", &s, 144, 0);
        let j = serde_json::to_value(&info).unwrap();
        assert_eq!(j["kind"], "3d");
        assert_eq!(j["cloaked"], true);
        assert_eq!(j["rect_px"], serde_json::json!([0, 0, 10, 10]));
    }
}
