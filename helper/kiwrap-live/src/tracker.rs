//! Window tracker: `SetWinEventHook` on the target process (create / destroy / show / hide /
//! move / name change / cloak / minimise, plus the system-wide foreground event) drives a rescan
//! of the process's top-level windows, their grouping into surfaces (`surfaces::group`), the
//! per-window WGC sessions and the per-surface compositors. No polling: the hook callback arms a
//! 12 ms coalescing timer on this thread (a dialog opening fires create+show+locationchange bursts;
//! measured hook-event-to-list 11-15 ms in the prototype); a slow 2 s timer only checks that the
//! process is still alive and reconciles anything a missed event could have left behind.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::UI::Accessibility::{SetWinEventHook, UnhookWinEvent, HWINEVENTHOOK};
use windows::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, GetMessageW, KillTimer, PostThreadMessageW, SetTimer, TranslateMessage, MSG, WM_QUIT, WM_TIMER,
};
use windows::Win32::System::Threading::GetCurrentThreadId;

use crate::capture::{Session, Sink, Slot};
use crate::composite;
use crate::hub::{Hub, SurfaceChannel, TrackerState};
use crate::protocol::{ControlPush, SurfaceInfo};
use crate::surfaces::{self, IdMap, Surface};
use crate::win;

const EVENT_SYSTEM_FOREGROUND: u32 = 0x0003;
const EVENT_SYSTEM_MENUPOPUPSTART: u32 = 0x0006;
const EVENT_SYSTEM_MENUPOPUPEND: u32 = 0x0007;
const EVENT_SYSTEM_MOVESIZESTART: u32 = 0x000A;
const EVENT_SYSTEM_MOVESIZEEND: u32 = 0x000B;
const EVENT_SYSTEM_DIALOGSTART: u32 = 0x0010;
const EVENT_SYSTEM_DIALOGEND: u32 = 0x0011;
const EVENT_SYSTEM_MINIMIZESTART: u32 = 0x0016;
const EVENT_SYSTEM_MINIMIZEEND: u32 = 0x0017;
const EVENT_OBJECT_CREATE: u32 = 0x8000;
const EVENT_OBJECT_HIDE: u32 = 0x8003;
const EVENT_OBJECT_REORDER: u32 = 0x8004;
const EVENT_OBJECT_LOCATIONCHANGE: u32 = 0x800B;
const EVENT_OBJECT_NAMECHANGE: u32 = 0x800C;
const EVENT_OBJECT_CLOAKED: u32 = 0x8017;
const EVENT_OBJECT_UNCLOAKED: u32 = 0x8018;
const WINEVENT_OUTOFCONTEXT: u32 = 0x0000;
const WINEVENT_SKIPOWNPROCESS: u32 = 0x0002;

const TIMER_RESCAN: usize = 1;
const TIMER_SAFETY: usize = 2;
const COALESCE_MS: u32 = 12;
const SAFETY_MS: u32 = 2000;

static PENDING: AtomicBool = AtomicBool::new(false);
static FOREGROUND: AtomicUsize = AtomicUsize::new(0);
static HOOK_EVENTS: AtomicU32 = AtomicU32::new(0);
static FIRST_EVENT_AT: Mutex<Option<Instant>> = Mutex::new(None);
static TARGET_PID: AtomicU32 = AtomicU32::new(0);
// thread timers (hwnd = NULL) get an id ASSIGNED by SetTimer; the requested id is ignored
static RESCAN_TIMER: AtomicUsize = AtomicUsize::new(0);
static SAFETY_TIMER: AtomicUsize = AtomicUsize::new(0);
static TRACKER_TID: AtomicU32 = AtomicU32::new(0);
static HUB: OnceLock<Arc<Hub>> = OnceLock::new();

unsafe extern "system" fn hook_proc(
    _hook: HWINEVENTHOOK,
    event: u32,
    hwnd: HWND,
    id_object: i32,
    id_child: i32,
    _tid: u32,
    _t_ms: u32,
) {
    if id_object != 0 || id_child != 0 || hwnd.0.is_null() {
        return; // OBJID_WINDOW only
    }
    HOOK_EVENTS.fetch_add(1, Ordering::Relaxed);
    if event == EVENT_SYSTEM_FOREGROUND {
        FOREGROUND.store(hwnd.0 as usize, Ordering::Relaxed);
    }
    if event == EVENT_OBJECT_LOCATIONCHANGE {
        // only top-level windows of the target matter; child controls fire this constantly
        let (_, pid) = win::thread_and_pid(hwnd.0 as usize);
        if pid != TARGET_PID.load(Ordering::Relaxed) || win::root_of(hwnd.0 as usize) != hwnd.0 as usize {
            return;
        }
    }
    if !PENDING.swap(true, Ordering::AcqRel) {
        *FIRST_EVENT_AT.lock().unwrap() = Some(Instant::now());
        let id = unsafe { SetTimer(None, TIMER_RESCAN, COALESCE_MS, None) };
        RESCAN_TIMER.store(id, Ordering::Relaxed);
    }
}

/// Ask the tracker thread to stop (from any thread).
pub fn request_stop() {
    let tid = TRACKER_TID.load(Ordering::Relaxed);
    if tid != 0 {
        unsafe {
            let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

struct Tracker {
    hub: Arc<Hub>,
    ids: IdMap,
    sessions: HashMap<usize, Session>,
    monitor_sessions: HashMap<usize, Session>,
    failed: HashMap<usize, Instant>,
    gathered: HashSet<usize>,
    prev: Vec<(String, Surface)>,
    prev_foreground_id: Option<String>,
    warned_cloak: bool,
}

impl Tracker {
    fn rescan(&mut self) {
        let t_event = FIRST_EVENT_AT.lock().unwrap().take();
        let hub = self.hub.clone();
        let pid = hub.cfg.pid;
        let mut wins = win::windows_of_pid(pid);
        let alive = !wins.is_empty() || win::process_alive(pid);
        let mut surfaces = surfaces::group(&wins);
        if hub.cfg.gather_dialogs {
            let mut moved = false;
            for s in &surfaces {
                for (h, x, y) in surfaces::gather_moves(s, &self.gathered) {
                    self.gathered.insert(h);
                    if win::move_window_frame(h, x, y) {
                        moved = true;
                        log::info!("gathered {h:#x} onto {:#x} at ({x},{y})", s.root);
                    }
                }
            }
            if moved {
                wins = win::windows_of_pid(pid);
                surfaces = surfaces::group(&wins);
            }
        }
        let ids = self.ids.assign(&surfaces);
        let named: Vec<(String, Surface)> =
            ids.into_iter().zip(surfaces).map(|((id, _root), s)| (id, s)).collect();
        let mut dpi = HashMap::new();
        for (_, s) in &named {
            dpi.insert(s.root, win::dpi_for_window(s.root));
        }
        let foreground = FOREGROUND.load(Ordering::Relaxed);

        // ---- capture sessions per non-fragile member window
        let wanted: HashSet<usize> =
            named.iter().flat_map(|(_, s)| s.members.iter().filter(|m| !m.is_fragile()).map(|m| m.hwnd)).collect();
        for h in wanted.iter().copied() {
            if let Some(s) = self.sessions.get(&h) {
                if !s.is_closed() {
                    continue;
                }
                self.sessions.remove(&h);
            }
            if let Some(t) = self.failed.get(&h) {
                if t.elapsed().as_millis() < 1500 {
                    continue;
                }
            }
            let slot = Slot::new();
            let hub2 = hub.clone();
            let sink = Arc::new(Sink { slot: slot.clone(), wake: Box::new(move || hub2.window_updated(h)), closed: AtomicBool::new(false) });
            match Session::window(h, sink, hub.cfg.fps) {
                Ok(s) => {
                    hub.slots.lock().unwrap().insert(h, slot);
                    self.sessions.insert(h, s);
                    self.failed.remove(&h);
                }
                Err(e) => {
                    log::warn!("{e}");
                    self.failed.insert(h, Instant::now());
                }
            }
        }
        for h in self.sessions.keys().copied().collect::<Vec<_>>() {
            if !wanted.contains(&h) {
                if let Some(s) = self.sessions.remove(&h) {
                    s.stop();
                }
                hub.slots.lock().unwrap().remove(&h);
            }
        }
        self.failed.retain(|h, _| wanted.contains(h));

        // ---- monitor sessions for popup menus / drop-downs (never captured per window)
        let mut monitors: HashMap<usize, surfaces::Rect> = HashMap::new();
        if hub.cfg.menus {
            let fragile: Vec<usize> =
                named.iter().flat_map(|(_, s)| s.members.iter().filter(|m| m.is_fragile()).map(|m| m.hwnd)).collect();
            for h in fragile {
                let (hm, r) = win::monitor_of(h);
                monitors.insert(hm, r);
            }
            for (hm, _) in monitors.iter() {
                if self.monitor_sessions.get(hm).map(|s| !s.is_closed()).unwrap_or(false) {
                    continue;
                }
                let slot = Slot::new();
                let hub2 = hub.clone();
                let hm2 = *hm;
                let sink = Arc::new(Sink { slot: slot.clone(), wake: Box::new(move || hub2.monitor_updated(hm2)), closed: AtomicBool::new(false) });
                match Session::monitor(*hm, sink, hub.cfg.fps) {
                    Ok(s) => {
                        hub.monitor_slots.lock().unwrap().insert(*hm, slot);
                        self.monitor_sessions.insert(*hm, s);
                        log::info!("monitor capture {hm:#x} started for a popup");
                    }
                    Err(e) => log::warn!("{e}"),
                }
            }
            for hm in self.monitor_sessions.keys().copied().collect::<Vec<_>>() {
                if !monitors.contains_key(&hm) {
                    if let Some(s) = self.monitor_sessions.remove(&hm) {
                        s.stop();
                    }
                    hub.monitor_slots.lock().unwrap().remove(&hm);
                    log::info!("monitor capture {hm:#x} stopped");
                }
            }
        }

        // ---- publish the state, then diff for events and channel lifecycle
        let new_state = TrackerState {
            surfaces: named.clone(),
            windows: wins,
            foreground,
            dpi: dpi.clone(),
            rescans: 0,
            hook_events: HOOK_EVENTS.load(Ordering::Relaxed) as u64,
            hook_to_list_us: Vec::new(),
            process_alive: alive,
            monitors,
        };
        let infos: Vec<SurfaceInfo> = new_state.infos();
        {
            let mut st = hub.state.write().unwrap();
            let rescans = st.rescans + 1;
            let mut lat = std::mem::take(&mut st.hook_to_list_us);
            if let Some(t) = t_event {
                lat.push(t.elapsed().as_micros() as u64);
                if lat.len() > 200 {
                    lat.drain(0..100);
                }
            }
            *st = new_state;
            st.rescans = rescans;
            st.hook_to_list_us = lat;
        }

        // channels: create for new ids, stop for gone ids
        {
            let mut chans = hub.channels.lock().unwrap();
            for (id, s) in &named {
                if !chans.contains_key(id) {
                    let ch = SurfaceChannel::new(id, s.root);
                    chans.insert(id.clone(), ch.clone());
                    composite::spawn(hub.clone(), ch);
                }
            }
            let live: HashSet<&String> = named.iter().map(|(i, _)| i).collect();
            for id in chans.keys().cloned().collect::<Vec<_>>() {
                if !live.contains(&id) {
                    if let Some(ch) = chans.remove(&id) {
                        ch.stop.store(true, Ordering::Relaxed);
                        ch.wake();
                        let _ = ch.frame_tx.send(None);
                    }
                }
            }
        }

        // events
        let prev: HashMap<&String, &Surface> = self.prev.iter().map(|(i, s)| (i, s)).collect();
        for info in &infos {
            let s = named.iter().find(|(i, _)| i == &info.id).map(|(_, s)| s).unwrap();
            match prev.get(&info.id) {
                None => {
                    log::info!("surface create {} {:?} {:#x} {:?} cloaked={}", info.id, info.kind, s.root, s.rect, s.cloaked);
                    self.push("create", Some(info.clone()));
                }
                Some(p) => {
                    if p.title != s.title {
                        self.push("title", Some(info.clone()));
                    }
                    if p.rect != s.rect || p.root_rect != s.root_rect {
                        self.push("rect", Some(info.clone()));
                        if let Some(ch) = hub.channel(&info.id) {
                            ch.request_keyframe();
                        }
                    } else if p.dialogs != s.dialogs || p.enabled != s.enabled {
                        self.push("dialogs", Some(info.clone()));
                        if let Some(ch) = hub.channel(&info.id) {
                            ch.request_keyframe();
                        }
                    }
                    if p.cloaked != s.cloaked {
                        self.push("cloak", Some(info.clone()));
                    }
                }
            }
        }
        for (id, _) in &self.prev {
            if !named.iter().any(|(i, _)| i == id) {
                log::info!("surface destroy {id}");
                hub.push_control(&ControlPush::Event { event: "destroy", surface: None, id: Some(id.clone()) });
            }
        }
        let fg_id = named.iter().find(|(_, s)| s.members.iter().any(|m| m.hwnd == foreground)).map(|(i, _)| i.clone());
        if fg_id != self.prev_foreground_id {
            let info = fg_id.as_ref().and_then(|i| infos.iter().find(|x| &x.id == i).cloned());
            hub.push_control(&ControlPush::Event { event: "foreground", surface: info, id: fg_id.clone() });
            self.prev_foreground_id = fg_id;
        }
        if hub.cfg.cloak_expected && !self.warned_cloak && !named.is_empty() && named.iter().all(|(_, s)| s.cloaked == 0) {
            log::warn!("--cloak-expected but no surface is cloaked: is KICAD_INPUT_FROM_EVENTS=cloak set on a build with mr/display-input?");
            self.warned_cloak = true;
        }
        self.prev = named;

        if !alive {
            log::info!("process {pid} exited; helper stopping");
            hub.push_control(&ControlPush::Event { event: "process_exit", surface: None, id: None });
            hub.exiting.store(true, Ordering::Relaxed);
            request_stop();
        }
    }

    fn push(&self, event: &'static str, info: Option<SurfaceInfo>) {
        let id = info.as_ref().map(|i| i.id.clone());
        self.hub.push_control(&ControlPush::Event { event, surface: info, id });
    }

    fn shutdown(&mut self) {
        for (_, s) in self.sessions.drain() {
            s.stop();
        }
        for (_, s) in self.monitor_sessions.drain() {
            s.stop();
        }
        let chans: Vec<_> = self.hub.channels.lock().unwrap().drain().map(|(_, c)| c).collect();
        for ch in chans {
            ch.stop.store(true, Ordering::Relaxed);
            ch.wake();
        }
    }
}

/// Run the tracker on the current thread until `request_stop()` or the process exits.
pub fn run(hub: Arc<Hub>) {
    let _ = HUB.set(hub.clone());
    TARGET_PID.store(hub.cfg.pid, Ordering::Relaxed);
    TRACKER_TID.store(unsafe { GetCurrentThreadId() }, Ordering::Relaxed);
    FOREGROUND.store(win::foreground(), Ordering::Relaxed);
    let mut tracker = Tracker {
        hub: hub.clone(),
        ids: IdMap::default(),
        sessions: HashMap::new(),
        monitor_sessions: HashMap::new(),
        failed: HashMap::new(),
        gathered: HashSet::new(),
        prev: Vec::new(),
        prev_foreground_id: None,
        warned_cloak: false,
    };
    let ranges: &[(u32, u32, u32)] = &[
        (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0), // every process: someone taking the foreground away
        (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND, hub.cfg.pid),
        (EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND, hub.cfg.pid),
        (EVENT_SYSTEM_DIALOGSTART, EVENT_SYSTEM_DIALOGEND, hub.cfg.pid),
        (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND, hub.cfg.pid),
        (EVENT_OBJECT_CREATE, EVENT_OBJECT_HIDE, hub.cfg.pid),
        (EVENT_OBJECT_REORDER, EVENT_OBJECT_REORDER, hub.cfg.pid),
        (EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_NAMECHANGE, hub.cfg.pid),
        (EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED, hub.cfg.pid),
    ];
    let mut hooks = Vec::new();
    for (lo, hi, pid) in ranges {
        let h = unsafe { SetWinEventHook(*lo, *hi, None, Some(hook_proc), *pid, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS) };
        if h.0.is_null() {
            log::warn!("SetWinEventHook({lo:#x}-{hi:#x}) failed");
        } else {
            hooks.push(h);
        }
    }
    let safety = unsafe { SetTimer(None, TIMER_SAFETY, SAFETY_MS, None) };
    SAFETY_TIMER.store(safety, Ordering::Relaxed);
    tracker.rescan();
    let mut msg = MSG::default();
    unsafe {
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            if msg.message == WM_TIMER {
                let id = msg.wParam.0;
                if id != 0 && id == RESCAN_TIMER.load(Ordering::Relaxed) {
                    let _ = KillTimer(None, id);
                    RESCAN_TIMER.store(0, Ordering::Relaxed);
                    PENDING.store(false, Ordering::Release);
                    tracker.rescan();
                } else if id == SAFETY_TIMER.load(Ordering::Relaxed) && !PENDING.load(Ordering::Relaxed) {
                    tracker.rescan();
                }
                continue;
            }
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        for h in hooks {
            let _ = UnhookWinEvent(h);
        }
    }
    tracker.shutdown();
}
