//! Shared state between the tracker thread (windows -> surfaces, capture sessions), the
//! per-surface compositor threads (frames), the injector thread (input) and the WebSocket server.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::Instant;

use tokio::sync::{broadcast, watch};

use crate::capture::Slot;
use crate::codec::Bgra;
use crate::protocol::SurfaceInfo;
use crate::surfaces::{Rect, Surface, WinInfo};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InjectMode {
    None,
    Mirror,
    Events,
    Hybrid,
}

impl InjectMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            InjectMode::None => "none",
            InjectMode::Mirror => "mirror",
            InjectMode::Events => "events",
            InjectMode::Hybrid => "hybrid",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EscClose {
    Auto,
    Always,
    Never,
}

#[derive(Clone, Debug)]
#[allow(dead_code)] // esc_close consumed when constructing the events injector
pub struct Config {
    pub pid: u32,
    pub quality: u32,
    pub fps: u32,
    pub gather_dialogs: bool,
    pub inject: InjectMode,
    pub cloak_expected: bool,
    pub tile: u32,
    pub esc_close: EscClose,
    pub menus: bool,
    /// Resize a surface's frame to the client's viewport on its `resize` input event so the
    /// stream is 1:1 in the tab (`--fit-to-view`; the extension passes it when the tab is the
    /// primary UI, i.e. any injecting mode).
    pub fit_to_view: bool,
}

/// One encoded frame ready to be sent to every client of a surface.
#[allow(dead_code)] // t_capture/t_encoded kept for latency accounting
pub struct Encoded {
    pub id: u32,
    pub w: u32,
    pub h: u32,
    pub jpeg: Vec<u8>,
    pub dirty: Vec<[i32; 4]>,
    pub rect: Rect,
    pub dpi: u32,
    pub probe: Option<u32>,
    pub t_capture: Instant,
    pub t_encoded: Instant,
    pub encode_us: u64,
}

/// Per-surface plumbing that outlives individual clients.
#[allow(dead_code)] // `root` mirrors the surface root hwnd for diagnostics
pub struct SurfaceChannel {
    pub id: String,
    pub root: usize,
    pub frame_tx: watch::Sender<Option<Arc<Encoded>>>,
    pub wake: (Mutex<bool>, Condvar),
    pub stop: AtomicBool,
    /// Latest composite (for screenshots and for new clients).
    pub last: Mutex<Option<Bgra>>,
    pub last_rect: Mutex<Rect>,
    pub frame_id: AtomicU32,
    pub frames: AtomicU64,
    pub bytes: AtomicU64,
    pub skipped_unchanged: AtomicU64,
    pub encode_us_total: AtomicU64,
    pub probe: AtomicU32,
    /// A client asked for a fresh full frame (re-encode the last composite even if unchanged).
    pub keyframe: AtomicBool,
    pub clients: AtomicU32,
    pub last_frame_at: Mutex<Option<Instant>>,
}

impl SurfaceChannel {
    pub fn new(id: &str, root: usize) -> Arc<Self> {
        let (frame_tx, _rx) = watch::channel(None);
        Arc::new(SurfaceChannel {
            id: id.to_string(),
            root,
            frame_tx,
            wake: (Mutex::new(false), Condvar::new()),
            stop: AtomicBool::new(false),
            last: Mutex::new(None),
            last_rect: Mutex::new(Rect::default()),
            frame_id: AtomicU32::new(0),
            frames: AtomicU64::new(0),
            bytes: AtomicU64::new(0),
            skipped_unchanged: AtomicU64::new(0),
            encode_us_total: AtomicU64::new(0),
            probe: AtomicU32::new(0),
            keyframe: AtomicBool::new(false),
            clients: AtomicU32::new(0),
            last_frame_at: Mutex::new(None),
        })
    }
    pub fn wake(&self) {
        let (m, cv) = &self.wake;
        *m.lock().unwrap() = true;
        cv.notify_one();
    }
    pub fn request_keyframe(&self) {
        self.keyframe.store(true, Ordering::Relaxed);
        self.wake();
    }
}

/// The tracker's view of the process: surfaces with ids, plus everything the injectors need.
#[derive(Clone, Default)]
pub struct TrackerState {
    pub surfaces: Vec<(String, Surface)>,
    pub windows: Vec<WinInfo>,
    pub foreground: usize,
    pub dpi: HashMap<usize, u32>,
    pub rescans: u64,
    pub hook_events: u64,
    pub hook_to_list_us: Vec<u64>,
    pub process_alive: bool,
    /// Monitor (hmonitor -> rect) whose capture backs popup menus.
    pub monitors: HashMap<usize, Rect>,
}

impl TrackerState {
    pub fn surface(&self, id: &str) -> Option<&Surface> {
        self.surfaces.iter().find(|(i, _)| i == id).map(|(_, s)| s)
    }
    pub fn surface_id_of_hwnd(&self, hwnd: usize) -> Option<&String> {
        self.surfaces.iter().find(|(_, s)| s.members.iter().any(|m| m.hwnd == hwnd)).map(|(i, _)| i)
    }
    pub fn info(&self, id: &str, s: &Surface) -> SurfaceInfo {
        SurfaceInfo::from_surface(id, s, *self.dpi.get(&s.root).unwrap_or(&96), self.foreground)
    }
    pub fn infos(&self) -> Vec<SurfaceInfo> {
        self.surfaces.iter().map(|(id, s)| self.info(id, s)).collect()
    }
}

pub struct Hub {
    pub cfg: Config,
    pub state: RwLock<TrackerState>,
    pub slots: Mutex<HashMap<usize, Arc<Slot>>>,
    pub monitor_slots: Mutex<HashMap<usize, Arc<Slot>>>,
    pub channels: Mutex<HashMap<String, Arc<SurfaceChannel>>>,
    /// JSON text pushed to every control client.
    pub control_tx: broadcast::Sender<String>,
    pub input_events: AtomicU64,
    pub started: Instant,
    pub exiting: AtomicBool,
}

impl Hub {
    pub fn new(cfg: Config) -> Arc<Hub> {
        let (control_tx, _) = broadcast::channel(256);
        Arc::new(Hub {
            cfg,
            state: RwLock::new(TrackerState::default()),
            slots: Mutex::new(HashMap::new()),
            monitor_slots: Mutex::new(HashMap::new()),
            channels: Mutex::new(HashMap::new()),
            control_tx,
            input_events: AtomicU64::new(0),
            started: Instant::now(),
            exiting: AtomicBool::new(false),
        })
    }

    pub fn channel(&self, id: &str) -> Option<Arc<SurfaceChannel>> {
        self.channels.lock().unwrap().get(id).cloned()
    }

    pub fn push_control(&self, v: &impl serde::Serialize) {
        if let Ok(s) = serde_json::to_string(v) {
            let _ = self.control_tx.send(s);
        }
    }

    /// A window's slot got a new frame: wake the compositor of the surface it belongs to.
    pub fn window_updated(&self, hwnd: usize) {
        let id = { self.state.read().unwrap().surface_id_of_hwnd(hwnd).cloned() };
        if let Some(id) = id {
            if let Some(ch) = self.channel(&id) {
                ch.wake();
            }
        }
    }

    /// A monitor slot got a new frame: wake every surface that shows a popup from that monitor.
    pub fn monitor_updated(&self, hmonitor: usize) {
        let ids: Vec<String> = {
            let st = self.state.read().unwrap();
            let Some(mrect) = st.monitors.get(&hmonitor).copied() else { return };
            st.surfaces
                .iter()
                .filter(|(_, s)| s.members.iter().any(|m| m.is_fragile() && mrect.intersects(&m.rect)))
                .map(|(id, _)| id.clone())
                .collect()
        };
        for id in ids {
            if let Some(ch) = self.channel(&id) {
                ch.wake();
            }
        }
    }

    pub fn stats(&self) -> serde_json::Value {
        let st = self.state.read().unwrap();
        let chans = self.channels.lock().unwrap();
        let mut surfaces = serde_json::Map::new();
        for (id, ch) in chans.iter() {
            let frames = ch.frames.load(Ordering::Relaxed);
            surfaces.insert(
                id.clone(),
                serde_json::json!({
                    "frames": frames,
                    "bytes": ch.bytes.load(Ordering::Relaxed),
                    "skipped_unchanged": ch.skipped_unchanged.load(Ordering::Relaxed),
                    "encode_ms_avg": if frames > 0 { ch.encode_us_total.load(Ordering::Relaxed) as f64 / frames as f64 / 1000.0 } else { 0.0 },
                    "clients": ch.clients.load(Ordering::Relaxed),
                    "size": ch.last.lock().unwrap().as_ref().map(|b| [b.w, b.h]).unwrap_or([0, 0]),
                }),
            );
        }
        let slots = self.slots.lock().unwrap();
        let mut windows = serde_json::Map::new();
        for (h, s) in slots.iter() {
            windows.insert(format!("{h}"), serde_json::json!({"frames": s.frames.load(Ordering::Relaxed)}));
        }
        let hook_to_list: Vec<f64> = st.hook_to_list_us.iter().rev().take(20).map(|u| *u as f64 / 1000.0).collect();
        serde_json::json!({
            "pid": self.cfg.pid,
            "uptime_s": self.started.elapsed().as_secs_f64(),
            "inject": self.cfg.inject.as_str(),
            "quality": self.cfg.quality,
            "fps_cap": self.cfg.fps,
            "rescans": st.rescans,
            "hook_events": st.hook_events,
            "hook_to_list_ms": hook_to_list,
            "foreground": st.foreground,
            "process_alive": st.process_alive,
            "input_events": self.input_events.load(Ordering::Relaxed),
            "surfaces": surfaces,
            "windows": windows,
            "monitor_sessions": self.monitor_slots.lock().unwrap().len(),
        })
    }
}
