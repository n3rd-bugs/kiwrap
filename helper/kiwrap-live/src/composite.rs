//! One compositor thread per surface: on wake, blit the root frame and its owned dialogs (and the
//! monitor crop of any popup menu) into one image the size of the surface rect, compare it with
//! the previous composite tile by tile, and JPEG-encode only when something changed. A wake with
//! no visible change costs one memcmp and no bytes on the wire (idle = 0 frames, as measured for
//! raw WGC in winstream.md 2c).

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::codec::{self, Bgra, JpegEncoder};
use crate::hub::{Encoded, Hub, SurfaceChannel};
use crate::surfaces::Rect;

pub fn spawn(hub: Arc<Hub>, ch: Arc<SurfaceChannel>) {
    let name = format!("composite-{}", ch.id);
    std::thread::Builder::new()
        .name(name)
        .spawn(move || run(hub, ch))
        .expect("spawn compositor");
}

fn run(hub: Arc<Hub>, ch: Arc<SurfaceChannel>) {
    let mut enc = match JpegEncoder::new(hub.cfg.quality) {
        Ok(e) => e,
        Err(e) => {
            log::error!("[{}] jpeg encoder: {e}", ch.id);
            return;
        }
    };
    let min_interval = if hub.cfg.fps > 0 { Duration::from_micros(1_000_000 / hub.cfg.fps as u64) } else { Duration::ZERO };
    let mut prev: Option<Bgra> = None;
    let mut last_sent = Instant::now() - Duration::from_secs(1);
    let mut next = Bgra::default();
    loop {
        // wait for a wake (or a periodic check so a stop request is noticed)
        {
            let (m, cv) = &ch.wake;
            let mut flag = m.lock().unwrap();
            while !*flag && !ch.stop.load(Ordering::Relaxed) {
                let (f, _) = cv.wait_timeout(flag, Duration::from_millis(500)).unwrap();
                flag = f;
            }
            *flag = false;
        }
        if ch.stop.load(Ordering::Relaxed) {
            break;
        }
        // fps pacing: coalesce bursts (several windows of one surface updating together)
        let since = last_sent.elapsed();
        if since < min_interval {
            std::thread::sleep(min_interval - since);
            let (m, _) = &ch.wake;
            *m.lock().unwrap() = false;
        }
        let t0 = Instant::now();
        let Some((rect, dpi)) = compose(&hub, &ch, &mut next) else { continue };
        let keyframe = ch.keyframe.swap(false, Ordering::Relaxed);
        let dirty: Vec<[i32; 4]> = match prev.as_ref().and_then(|p| codec::dirty_rects(p, &next, hub.cfg.tile)) {
            Some(d) if d.is_empty() && !keyframe => {
                ch.skipped_unchanged.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            Some(d) if !d.is_empty() && !keyframe => d,
            _ => vec![[0, 0, next.w as i32, next.h as i32]],
        };
        let te = Instant::now();
        let jpeg = match enc.encode(&next) {
            Ok(j) => j,
            Err(e) => {
                log::warn!("[{}] encode: {e}", ch.id);
                continue;
            }
        };
        let encode_us = te.elapsed().as_micros() as u64;
        let id = ch.frame_id.fetch_add(1, Ordering::Relaxed) + 1;
        let probe = ch.probe.swap(0, Ordering::Relaxed);
        let frame = Arc::new(Encoded {
            id,
            w: next.w,
            h: next.h,
            jpeg,
            dirty,
            rect,
            dpi,
            probe: if probe != 0 { Some(probe) } else { None },
            t_capture: t0,
            t_encoded: Instant::now(),
            encode_us,
        });
        ch.frames.fetch_add(1, Ordering::Relaxed);
        ch.bytes.fetch_add(frame.jpeg.len() as u64, Ordering::Relaxed);
        ch.encode_us_total.fetch_add(encode_us, Ordering::Relaxed);
        *ch.last_frame_at.lock().unwrap() = Some(Instant::now());
        *ch.last_rect.lock().unwrap() = rect;
        // keep the composite for screenshots / late joiners, swap buffers
        {
            let mut last = ch.last.lock().unwrap();
            let old = last.take();
            *last = Some(next.clone());
            prev = Some(std::mem::replace(&mut next, old.unwrap_or_default()));
        }
        let _ = ch.frame_tx.send(Some(frame));
        last_sent = Instant::now();
    }
    log::debug!("[{}] compositor stopped", ch.id);
}

/// Build the composite of the surface into `out`. Returns (surface rect, dpi) or None when the
/// surface is gone or has no root frame yet.
fn compose(hub: &Hub, ch: &SurfaceChannel, out: &mut Bgra) -> Option<(Rect, u32)> {
    let (surface, dpi, monitors) = {
        let st = hub.state.read().unwrap();
        let s = st.surface(&ch.id)?.clone();
        let dpi = *st.dpi.get(&s.root).unwrap_or(&96);
        (s, dpi, st.monitors.clone())
    };
    let rect = surface.rect;
    if rect.is_empty() {
        return None;
    }
    let slots = hub.slots.lock().unwrap().clone();
    let root_slot = slots.get(&surface.root)?;
    if !root_slot.has_frame() {
        return None;
    }
    let (w, h) = (rect.w() as u32, rect.h() as u32);
    if out.w != w || out.h != h {
        *out = Bgra::new(w, h);
    } else {
        out.data.fill(0);
    }
    // bottom-most first: iterate members in reverse z-order
    let mut monitor_cache: Option<(usize, Arc<crate::capture::Slot>)> = None;
    for m in surface.members.iter().rev() {
        let dx = m.rect.l - rect.l;
        let dy = m.rect.t - rect.t;
        if m.is_fragile() {
            if !hub.cfg.menus {
                continue;
            }
            // crop from the monitor capture that shows this popup
            let Some((hmon, mrect)) = monitors.iter().find(|(_, r)| r.intersects(&m.rect)).map(|(h, r)| (*h, *r)) else {
                continue;
            };
            let slot = match &monitor_cache {
                Some((h, s)) if *h == hmon => s.clone(),
                _ => {
                    let Some(s) = hub.monitor_slots.lock().unwrap().get(&hmon).cloned() else { continue };
                    monitor_cache = Some((hmon, s.clone()));
                    s
                }
            };
            let img = slot.img.lock().unwrap();
            if img.is_empty() {
                continue;
            }
            // popup rect in monitor-image coordinates (frame bounds of menus == window rect)
            codec::blit_rect(out, &img, m.rect.l - mrect.l, m.rect.t - mrect.t, m.rect.w(), m.rect.h(), dx, dy);
            continue;
        }
        let Some(slot) = slots.get(&m.hwnd) else { continue };
        let img = slot.img.lock().unwrap();
        if img.is_empty() {
            continue;
        }
        codec::blit(out, &img, dx, dy);
    }
    Some((rect, dpi))
}
