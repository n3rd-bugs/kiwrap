//! Windows.Graphics.Capture sessions (crate `windows-capture`, MIT) per HWND and per monitor.
//!
//! WGC facts measured on the bench machine (docs/research/display/winstream.md 2a):
//! - a per-window capture contains ONLY that HWND's surface: owned dialogs and menus are
//!   separate HWNDs and get their own session (dialogs) or a monitor crop (menus: a per-window
//!   session of a popup menu dismisses it 50-100 ms after it opens);
//! - frames are delivered only when the window's content changes (idle = 0 frames);
//! - the GL (GAL) canvas is present because WGC reads the DWM-composed surface;
//! - a cloaked window is still composed and captured (win-input-patch.md 4); a minimised or fully
//!   off-screen one is not (no frames after the first).
//! Every frame is copied (BGRA, tightly packed) into the window's `Slot`; the surface compositor
//! (`composite.rs`) is then woken through the hub.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows_capture::capture::{CaptureControl, Context, GraphicsCaptureApiHandler};
use windows_capture::frame::Frame;
use windows_capture::graphics_capture_api::InternalCaptureControl;
use windows_capture::monitor::Monitor;
use windows_capture::settings::{
    ColorFormat, CursorCaptureSettings, DirtyRegionSettings, DrawBorderSettings, MinimumUpdateIntervalSettings,
    SecondaryWindowSettings, Settings,
};
use windows_capture::window::Window;

use crate::codec::Bgra;

/// Latest frame of one capture item (window or monitor).
pub struct Slot {
    pub img: Mutex<Bgra>,
    pub seq: AtomicU64,
    pub frames: AtomicU64,
    pub last_at: Mutex<Option<Instant>>,
}

impl Slot {
    pub fn new() -> Arc<Self> {
        Arc::new(Slot {
            img: Mutex::new(Bgra::default()),
            seq: AtomicU64::new(0),
            frames: AtomicU64::new(0),
            last_at: Mutex::new(None),
        })
    }
    pub fn has_frame(&self) -> bool {
        self.seq.load(Ordering::Relaxed) > 0
    }
}

/// What a capture thread needs: where to store frames and whom to wake.
pub struct Sink {
    pub slot: Arc<Slot>,
    pub wake: Box<dyn Fn() + Send + Sync>,
    pub closed: AtomicBool,
}

struct Handler {
    sink: Arc<Sink>,
    scratch: Vec<u8>,
}

impl GraphicsCaptureApiHandler for Handler {
    type Flags = Arc<Sink>;
    type Error = Box<dyn std::error::Error + Send + Sync>;

    fn new(ctx: Context<Self::Flags>) -> Result<Self, Self::Error> {
        Ok(Handler { sink: ctx.flags, scratch: Vec::new() })
    }

    fn on_frame_arrived(&mut self, frame: &mut Frame, control: InternalCaptureControl) -> Result<(), Self::Error> {
        if self.sink.closed.load(Ordering::Relaxed) {
            control.stop();
            return Ok(());
        }
        let w = frame.width();
        let h = frame.height();
        if w == 0 || h == 0 {
            return Ok(());
        }
        let buf = frame.buffer()?;
        let data = buf.as_nopadding_buffer(&mut self.scratch);
        {
            let mut img = self.sink.slot.img.lock().unwrap();
            if img.w != w || img.h != h {
                *img = Bgra::new(w, h);
            }
            let n = (w as usize) * (h as usize) * 4;
            img.data[..n].copy_from_slice(&data[..n]);
        }
        self.sink.slot.seq.fetch_add(1, Ordering::Release);
        self.sink.slot.frames.fetch_add(1, Ordering::Relaxed);
        *self.sink.slot.last_at.lock().unwrap() = Some(Instant::now());
        (self.sink.wake)();
        Ok(())
    }

    fn on_closed(&mut self) -> Result<(), Self::Error> {
        self.sink.closed.store(true, Ordering::Relaxed);
        (self.sink.wake)();
        Ok(())
    }
}

#[allow(dead_code)] // `started` kept for per-session age telemetry
pub struct Session {
    control: Option<CaptureControl<Handler, Box<dyn std::error::Error + Send + Sync>>>,
    pub sink: Arc<Sink>,
    pub started: Instant,
}

impl Session {
    fn settings<T: TryInto<windows_capture::settings::GraphicsCaptureItemType>>(
        item: T,
        sink: Arc<Sink>,
        fps_cap: u32,
    ) -> Settings<Arc<Sink>, T> {
        let interval = if fps_cap > 0 {
            MinimumUpdateIntervalSettings::Custom(Duration::from_micros(1_000_000 / fps_cap as u64))
        } else {
            MinimumUpdateIntervalSettings::Default
        };
        Settings::new(
            item,
            CursorCaptureSettings::WithoutCursor,
            DrawBorderSettings::WithoutBorder,
            SecondaryWindowSettings::Default,
            interval,
            DirtyRegionSettings::Default,
            ColorFormat::Bgra8,
            sink,
        )
    }

    /// Per-window session. Fails (Err) for windows WGC refuses (e.g. destroyed meanwhile).
    pub fn window(hwnd: usize, sink: Arc<Sink>, fps_cap: u32) -> Result<Session, String> {
        let item = Window::from_raw_hwnd(hwnd as *mut core::ffi::c_void);
        let control = Handler::start_free_threaded(Self::settings(item, sink.clone(), fps_cap))
            .map_err(|e| format!("window capture {hwnd:#x}: {e}"))?;
        Ok(Session { control: Some(control), sink, started: Instant::now() })
    }

    /// Whole-monitor session (for menus / drop-downs, cropped by the compositor).
    pub fn monitor(hmonitor: usize, sink: Arc<Sink>, fps_cap: u32) -> Result<Session, String> {
        let item = Monitor::from_raw_hmonitor(hmonitor as *mut core::ffi::c_void);
        let control = Handler::start_free_threaded(Self::settings(item, sink.clone(), fps_cap))
            .map_err(|e| format!("monitor capture {hmonitor:#x}: {e}"))?;
        Ok(Session { control: Some(control), sink, started: Instant::now() })
    }

    pub fn is_closed(&self) -> bool {
        self.sink.closed.load(Ordering::Relaxed)
    }

    pub fn stop(mut self) {
        self.sink.closed.store(true, Ordering::Relaxed);
        if let Some(c) = self.control.take() {
            // stop() joins the capture thread; a session whose window vanished may already be done.
            let _ = c.stop();
        }
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.sink.closed.store(true, Ordering::Relaxed);
        if let Some(c) = self.control.take() {
            let _ = c.stop();
        }
    }
}
