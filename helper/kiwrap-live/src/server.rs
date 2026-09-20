//! Loopback WebSocket server: `/control` (JSON) and `/stream?surface=<id>` (binary frames +
//! JSON input). Token in the query string of every handshake. Two measured requirements for a
//! VS Code webview client (docs/DISPLAY.md 7.2): no permessage-deflate (tungstenite does not
//! negotiate it) and exactly one writer per client (each connection has one task that owns the
//! sink; frames are drop-to-latest through a `watch` channel).

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::Message;

use crate::codec;
use crate::hub::{Hub, SurfaceChannel};
use crate::inject::{Injector, MouseButton, Target};
use crate::protocol::{self, ControlRequest, InputEvent, Meta, Modifiers};
use crate::surfaces::Rect;
use crate::win;

pub struct InputCmd {
    pub surface: String,
    pub ev: InputEvent,
    pub at: Instant,
}

pub struct Server {
    pub hub: Arc<Hub>,
    pub token: String,
    pub input_tx: std::sync::mpsc::Sender<InputCmd>,
    pub injector_stats: Arc<std::sync::Mutex<serde_json::Value>>,
}

fn query_param(uri: &str, key: &str) -> Option<String> {
    let q = uri.split_once('?')?.1;
    for kv in q.split('&') {
        let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
        if k == key {
            return Some(percent_decode(v));
        }
    }
    None
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if b[i] == b'+' { b' ' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub async fn run(server: Arc<Server>, listener: TcpListener) {
    loop {
        let (stream, peer) = match listener.accept().await {
            Ok(x) => x,
            Err(e) => {
                log::warn!("accept: {e}");
                tokio::time::sleep(Duration::from_millis(50)).await;
                continue;
            }
        };
        if !peer.ip().is_loopback() {
            continue;
        }
        let s = server.clone();
        tokio::spawn(async move {
            if let Err(e) = handle(s, stream).await {
                log::debug!("connection ended: {e}");
            }
        });
    }
}

#[derive(Clone, Debug)]
enum Route {
    Control,
    Stream(String),
}

async fn handle(server: Arc<Server>, stream: TcpStream) -> Result<(), String> {
    let route: Arc<std::sync::Mutex<Option<Route>>> = Arc::new(std::sync::Mutex::new(None));
    let token = server.token.clone();
    let route2 = route.clone();
    let cb = move |req: &Request, resp: Response| -> Result<Response, ErrorResponse> {
        let uri = req.uri().to_string();
        let path = uri.split('?').next().unwrap_or("").to_string();
        let t = query_param(&uri, "token").unwrap_or_default();
        if !token.is_empty() && t != token {
            let mut r = ErrorResponse::new(Some("forbidden".into()));
            *r.status_mut() = tokio_tungstenite::tungstenite::http::StatusCode::FORBIDDEN;
            return Err(r);
        }
        let r = match path.as_str() {
            "/control" => Route::Control,
            "/stream" => Route::Stream(query_param(&uri, "surface").unwrap_or_default()),
            _ => {
                let mut r = ErrorResponse::new(Some("not found".into()));
                *r.status_mut() = tokio_tungstenite::tungstenite::http::StatusCode::NOT_FOUND;
                return Err(r);
            }
        };
        *route2.lock().unwrap() = Some(r);
        Ok(resp)
    };
    let ws = tokio_tungstenite::accept_hdr_async(stream, cb).await.map_err(|e| e.to_string())?;
    let route = route.lock().unwrap().clone().ok_or("no route")?;
    match route {
        Route::Control => control(server, ws).await,
        Route::Stream(id) => stream_surface(server, ws, id).await,
    }
}

type Ws = tokio_tungstenite::WebSocketStream<TcpStream>;

fn json_text(v: &serde_json::Value) -> Message {
    Message::Text(v.to_string().into())
}

async fn control(server: Arc<Server>, ws: Ws) -> Result<(), String> {
    let hub = server.hub.clone();
    let (mut sink, mut source) = ws.split();
    let mut events = hub.control_tx.subscribe();
    let hello = {
        let st = hub.state.read().unwrap();
        serde_json::json!({
            "type": "hello",
            "version": protocol::VERSION,
            "pid": hub.cfg.pid,
            "inject": hub.cfg.inject.as_str(),
            "cloak_expected": hub.cfg.cloak_expected,
            "quality": hub.cfg.quality,
            "fps_cap": hub.cfg.fps,
            "surfaces": st.infos(),
        })
    };
    sink.send(json_text(&hello)).await.map_err(|e| e.to_string())?;
    loop {
        tokio::select! {
            ev = events.recv() => {
                match ev {
                    Ok(text) => sink.send(Message::Text(text.into())).await.map_err(|e| e.to_string())?,
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => break,
                }
            }
            msg = source.next() => {
                let Some(msg) = msg else { break };
                let msg = msg.map_err(|e| e.to_string())?;
                match msg {
                    Message::Text(t) => {
                        let reply = control_request(&server, t.as_str()).await;
                        if let Some(r) = reply {
                            sink.send(json_text(&r)).await.map_err(|e| e.to_string())?;
                        }
                    }
                    Message::Ping(p) => sink.send(Message::Pong(p)).await.map_err(|e| e.to_string())?,
                    Message::Close(_) => break,
                    _ => {}
                }
            }
        }
    }
    Ok(())
}

async fn control_request(server: &Arc<Server>, text: &str) -> Option<serde_json::Value> {
    let hub = &server.hub;
    let req: ControlRequest = match serde_json::from_str(text) {
        Ok(r) => r,
        Err(e) => return Some(serde_json::json!({"type": "error", "error": format!("bad request: {e}")})),
    };
    let id = req.id.clone();
    let with_id = |mut v: serde_json::Value| {
        if let Some(i) = &id {
            v["id"] = i.clone();
        }
        Some(v)
    };
    match req.kind.as_str() {
        "surfaces" => {
            let st = hub.state.read().unwrap();
            with_id(serde_json::json!({"type": "surfaces", "surfaces": st.infos()}))
        }
        "stats" => {
            let mut v = hub.stats();
            v["injector"] = server.injector_stats.lock().unwrap().clone();
            v["type"] = serde_json::json!("stats");
            with_id(v)
        }
        "ping" => with_id(serde_json::json!({"type": "pong", "t": now_ms()})),
        "keyframe" => {
            let sid = req.surface.clone().unwrap_or_default();
            if let Some(ch) = hub.channel(&sid) {
                ch.request_keyframe();
            }
            with_id(serde_json::json!({"type": "keyframe", "surface": sid}))
        }
        "raise" => {
            let sid = req.surface.clone().unwrap_or_default();
            let (root, target, cloaked) = {
                let st = hub.state.read().unwrap();
                match st.surface(&sid) {
                    Some(s) => (s.root, s.focus_target(), s.cloaked != 0),
                    None => (0, 0, false),
                }
            };
            if root == 0 {
                return with_id(serde_json::json!({"type": "raise", "surface": sid, "ok": false, "reason": "no such surface"}));
            }
            if cloaked {
                return with_id(serde_json::json!({"type": "raise", "surface": sid, "ok": false, "reason": "cloaked"}));
            }
            let ok = tokio::task::spawn_blocking(move || {
                let ok = win::bring_to_front(target.max(root));
                if target != root && target != 0 {
                    let _ = win::bring_to_front(target);
                }
                ok
            })
            .await
            .unwrap_or(false);
            with_id(serde_json::json!({"type": "raise", "surface": sid, "ok": ok}))
        }
        "close_dialog" => {
            // Post WM_CLOSE to the topmost enabled owned dialog of a surface (a DIALOG_SHIM whose
            // grid editor eats Esc will not close otherwise). {type:"close_dialog", surface}.
            let sid = req.surface.clone().unwrap_or_default();
            let target = {
                let st = hub.state.read().unwrap();
                st.surface(&sid).and_then(|s| {
                    s.members.iter().find(|m| m.owner != 0 && m.enabled && m.class != "#32768").map(|m| m.hwnd)
                })
            };
            match target {
                Some(h) => {
                    let ok = tokio::task::spawn_blocking(move || crate::inject::events::post(h, 0x0010, 0, 0)).await.unwrap_or(false);
                    with_id(serde_json::json!({"type": "close_dialog", "surface": sid, "ok": ok, "hwnd": h}))
                }
                None => with_id(serde_json::json!({"type": "close_dialog", "surface": sid, "ok": false, "reason": "no owned dialog"})),
            }
        }
        "screenshot" => {
            let sid = req.surface.clone().unwrap_or_default();
            let max_px = req.max_px.unwrap_or(1024);
            let fmt = req.format.clone().unwrap_or_else(|| "png".into());
            let quality = req.quality.unwrap_or(80);
            let Some(ch) = hub.channel(&sid) else {
                return with_id(serde_json::json!({"type": "screenshot", "surface": sid, "ok": false, "error": "no such surface"}));
            };
            let img = latest_composite(&ch).await;
            let Some(img) = img else {
                return with_id(serde_json::json!({"type": "screenshot", "surface": sid, "ok": false, "error": "no frame captured yet (window never painted?)"}));
            };
            let (rect, cloaked) = {
                let st = hub.state.read().unwrap();
                st.surface(&sid).map(|s| (s.rect, s.cloaked != 0)).unwrap_or((Rect::default(), false))
            };
            let encoded = tokio::task::spawn_blocking(move || {
                let small = codec::downscale_to(&img, max_px);
                let bytes = if fmt == "jpeg" {
                    codec::JpegEncoder::new(quality).and_then(|mut e| e.encode(&small))
                } else {
                    codec::encode_png(&small)
                };
                bytes.map(|b| (small.w, small.h, b, fmt))
            })
            .await
            .unwrap_or_else(|e| Err(e.to_string()));
            match encoded {
                Ok((w, h, bytes, fmt)) => {
                    use base64::Engine;
                    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
                    let mut v = serde_json::json!({
                        "type": "screenshot", "surface": sid, "ok": true, "w": w, "h": h, "format": fmt,
                        "bytes": bytes.len(), "rect_px": rect.as_array(), "cloaked": cloaked,
                    });
                    v[if fmt == "jpeg" { "jpeg" } else { "png" }] = serde_json::json!(b64);
                    with_id(v)
                }
                Err(e) => with_id(serde_json::json!({"type": "screenshot", "surface": sid, "ok": false, "error": e})),
            }
        }
        "hittest" => {
            // debugging / drivers: which child window is under a surface point, and its rect
            let sid = req.surface.clone().unwrap_or_default();
            let v: serde_json::Value = serde_json::from_str(text).ok()?;
            let (x, y) = (v["x"].as_f64().unwrap_or(0.0).round() as i32, v["y"].as_f64().unwrap_or(0.0).round() as i32);
            let target = {
                let st = hub.state.read().unwrap();
                st.surface(&sid).map(|s| (Target {
                    surface_id: sid.clone(),
                    root: s.root,
                    members: s.member_hwnds(),
                    focus_target: s.focus_target(),
                    thread_id: s.thread_id,
                    pid: hub.cfg.pid,
                    windows: st.windows.clone(),
                }, s.rect))
            };
            let Some((t, rect)) = target else {
                return with_id(serde_json::json!({"type": "hittest", "surface": sid, "ok": false, "error": "no such surface"}));
            };
            let (sx, sy) = (rect.l + x, rect.t + y);
            let (h, cx, cy) = crate::inject::events::EventInjector::hit_test(&t, sx, sy);
            let (focus, active, capture) = win::thread_focus(t.thread_id);
            if h == 0 {
                return with_id(serde_json::json!({"type": "hittest", "surface": sid, "ok": true, "hwnd": 0,
                    "focus": focus, "active": active, "capture": capture, "foreground": win::foreground()}));
            }
            let wr = win::window_rect(h);
            with_id(serde_json::json!({
                "type": "hittest", "surface": sid, "ok": true, "hwnd": h, "class": win::class_name(h),
                "client": [cx, cy], "rect_px": wr.as_array(),
                "rect_surface": [wr.l - rect.l, wr.t - rect.t, wr.r - rect.l, wr.b - rect.t],
                "root": win::root_of(h), "focus": focus, "active": active, "capture": capture,
                "foreground": win::foreground(),
            }))
        }
        "place" => {
            // move/resize a surface's root frame (frame-bounds coordinates, physical px, no
            // activation): kw_live uses it to keep a cloaked window on-screen and tests to get a
            // known geometry. {type:"place", surface, x, y, w, h} (w/h optional).
            let sid = req.surface.clone().unwrap_or_default();
            let v: serde_json::Value = serde_json::from_str(text).ok()?;
            let root = { hub.state.read().unwrap().surface(&sid).map(|s| s.root).unwrap_or(0) };
            if root == 0 {
                return with_id(serde_json::json!({"type": "place", "surface": sid, "ok": false, "error": "no such surface"}));
            }
            let x = v["x"].as_i64().map(|x| x as i32);
            let y = v["y"].as_i64().map(|y| y as i32);
            let w = v["w"].as_i64().map(|w| w as i32);
            let h = v["h"].as_i64().map(|h| h as i32);
            let ok = tokio::task::spawn_blocking(move || win::place_window_frame(root, x, y, w, h)).await.unwrap_or(false);
            let rect = win::frame_bounds(root);
            with_id(serde_json::json!({"type": "place", "surface": sid, "ok": ok, "rect_px": rect.as_array()}))
        }
        "input" => {
            // convenience: input through the control channel {type:"input", surface, event:{...}}
            let v: serde_json::Value = serde_json::from_str(text).ok()?;
            let sid = req.surface.clone().unwrap_or_default();
            if let Ok(ev) = serde_json::from_value::<InputEvent>(v["event"].clone()) {
                dispatch_input(server, &sid, ev);
            }
            None
        }
        other => with_id(serde_json::json!({"type": "error", "error": format!("unknown request type {other:?}")})),
    }
}

fn now_ms() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64() * 1000.0).unwrap_or(0.0)
}

/// The surface's latest composite; waits up to 1.5 s for the first one (a window that has not
/// painted since the session started produces nothing until it changes).
async fn latest_composite(ch: &Arc<SurfaceChannel>) -> Option<codec::Bgra> {
    let deadline = Instant::now() + Duration::from_millis(1500);
    loop {
        if let Some(img) = ch.last.lock().unwrap().as_ref() {
            return Some(img.clone());
        }
        if Instant::now() > deadline {
            return None;
        }
        ch.wake();
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

async fn stream_surface(server: Arc<Server>, ws: Ws, sid: String) -> Result<(), String> {
    let hub = server.hub.clone();
    let (mut sink, mut source) = ws.split();
    let Some(ch) = hub.channel(&sid) else {
        let ids: Vec<String> = hub.channels.lock().unwrap().keys().cloned().collect();
        let _ = sink
            .send(json_text(&serde_json::json!({"type": "error", "error": format!("no such surface {sid:?}"), "surfaces": ids})))
            .await;
        let _ = sink.close().await;
        return Ok(());
    };
    ch.clients.fetch_add(1, Ordering::Relaxed);
    struct Dec(Arc<SurfaceChannel>);
    impl Drop for Dec {
        fn drop(&mut self) {
            self.0.clients.fetch_sub(1, Ordering::Relaxed);
        }
    }
    let _dec = Dec(ch.clone());
    let mut rx = ch.frame_tx.subscribe();
    rx.mark_changed(); // deliver whatever is already there
    ch.request_keyframe();
    let mut last_size: Option<(u32, u32)> = None;
    let mut sent_frames: u64 = 0;
    let mut sent_bytes: u64 = 0;
    loop {
        tokio::select! {
            changed = rx.changed() => {
                if changed.is_err() {
                    break; // channel gone: surface destroyed
                }
                let frame = rx.borrow_and_update().clone();
                let Some(frame) = frame else {
                    if ch.stop.load(Ordering::Relaxed) { break } else { continue }
                };
                if last_size != Some((frame.w, frame.h)) {
                    let meta = Meta {
                        kind: "meta",
                        w: frame.w,
                        h: frame.h,
                        dpi_scale: frame.dpi as f32 / 96.0,
                        dirty: vec![[0, 0, frame.w as i32, frame.h as i32]],
                        frame_id: frame.id,
                        surface: sid.clone(),
                        rect_px: frame.rect.as_array(),
                    };
                    sink.send(Message::Text(serde_json::to_string(&meta).unwrap().into())).await.map_err(|e| e.to_string())?;
                    last_size = Some((frame.w, frame.h));
                }
                if let Some(p) = frame.probe {
                    let v = serde_json::json!({"type": "probe", "probe": p, "frame_id": frame.id, "t_server": now_ms(),
                        "encode_ms": frame.encode_us as f64 / 1000.0, "dirty": frame.dirty});
                    sink.send(json_text(&v)).await.map_err(|e| e.to_string())?;
                }
                let mut buf = Vec::with_capacity(protocol::FRAME_HEADER_LEN + frame.jpeg.len());
                buf.extend_from_slice(&protocol::frame_header(frame.id, frame.w, frame.h));
                buf.extend_from_slice(&frame.jpeg);
                sent_bytes += buf.len() as u64;
                sent_frames += 1;
                sink.send(Message::Binary(buf.into())).await.map_err(|e| e.to_string())?;
            }
            msg = source.next() => {
                let Some(msg) = msg else { break };
                let msg = msg.map_err(|e| e.to_string())?;
                match msg {
                    Message::Text(t) => {
                        match serde_json::from_str::<InputEvent>(t.as_str()) {
                            Ok(ev) => {
                                match ev.kind.as_str() {
                                    "ping" => {
                                        let v = serde_json::json!({"type": "pong", "id": ev.id, "t_server": now_ms(),
                                            "sent_frames": sent_frames, "sent_bytes": sent_bytes});
                                        sink.send(json_text(&v)).await.map_err(|e| e.to_string())?;
                                    }
                                    "keyframe" => ch.request_keyframe(),
                                    "resize" => {
                                        // the client's viewport in physical px: with --fit-to-view
                                        // the frame is sized to it so the stream is 1:1 in the tab
                                        // (reviewer finding 5: a 3840x2088 frame shown at 0.31 is
                                        // illegible); ignored otherwise (view-only mode)
                                        if hub.cfg.fit_to_view {
                                            fit_surface_to_view(&hub, &sid, ev.w, ev.h).await;
                                        }
                                    }
                                    _ => dispatch_input(&server, &sid, ev),
                                }
                            }
                            Err(e) => log::debug!("[{sid}] bad input event: {e}"),
                        }
                    }
                    Message::Ping(p) => sink.send(Message::Pong(p)).await.map_err(|e| e.to_string())?,
                    Message::Close(_) => break,
                    _ => {}
                }
            }
        }
    }
    let _ = sink.close().await;
    Ok(())
}

/// Resize the surface's root frame so its DWM frame bounds are `w` x `h` (physical px), when
/// that differs from the current size by more than a couple of pixels and the request is sane
/// (>= 200 x 150: KiCad's frames refuse smaller sizes anyway). Runs on the blocking pool.
async fn fit_surface_to_view(hub: &Arc<Hub>, sid: &str, w: Option<f64>, h: Option<f64>) {
    let (Some(w), Some(h)) = (w, h) else { return };
    let (w, h) = (w.round() as i32, h.round() as i32);
    if !(200..=16384).contains(&w) || !(150..=16384).contains(&h) {
        return;
    }
    let (root, rect) = {
        let st = hub.state.read().unwrap();
        match st.surface(sid) {
            Some(s) => (s.root, s.rect),
            None => return,
        }
    };
    if root == 0 || ((rect.w() - w).abs() <= 2 && (rect.h() - h).abs() <= 2) {
        return;
    }
    let sid = sid.to_string();
    let _ = tokio::task::spawn_blocking(move || {
        let ok = win::place_window_frame(root, None, None, Some(w), Some(h));
        let now = win::frame_bounds(root);
        log::info!("[{sid}] fit to view {w}x{h}: {} (frame now {}x{})", if ok { "ok" } else { "refused" }, now.w(), now.h());
    })
    .await;
}

/// Whether an input event's surface coordinates fall inside the surface (`[0, w) x [0, h)`).
/// Events outside are dropped by `injector_thread` (reviewer finding 3: a client naming a
/// point beyond the rect could otherwise drive real input anywhere on the desktop in mirror mode).
pub fn in_surface(rect: &Rect, x: f64, y: f64) -> bool {
    x.is_finite() && y.is_finite() && x >= 0.0 && y >= 0.0 && (x.round() as i64) < rect.w() as i64 && (y.round() as i64) < rect.h() as i64
}

fn dispatch_input(server: &Arc<Server>, sid: &str, ev: InputEvent) {
    server.hub.input_events.fetch_add(1, Ordering::Relaxed);
    if let Some(p) = ev.probe {
        if let Some(ch) = server.hub.channel(sid) {
            ch.probe.store(p, Ordering::Relaxed);
        }
    }
    let _ = server.input_tx.send(InputCmd { surface: sid.to_string(), ev, at: Instant::now() });
}

/// The injector thread: one `Injector`, events applied in arrival order (the ordering of
/// move -> down -> up matters), coordinates mapped from surface px to screen px per event.
pub fn injector_thread(
    hub: Arc<Hub>,
    mut inj: Box<dyn Injector>,
    rx: std::sync::mpsc::Receiver<InputCmd>,
    stats: Arc<std::sync::Mutex<serde_json::Value>>,
) {
    let mut last_buttons: std::collections::HashMap<String, u32> = std::collections::HashMap::new();
    let mut n: u64 = 0;
    let mut dropped_outside: u64 = 0;
    while let Ok(cmd) = rx.recv() {
        let target = {
            let st = hub.state.read().unwrap();
            let Some(s) = st.surface(&cmd.surface) else { continue };
            (
                Target {
                    surface_id: cmd.surface.clone(),
                    root: s.root,
                    members: s.member_hwnds(),
                    focus_target: s.focus_target(),
                    thread_id: s.thread_id,
                    pid: hub.cfg.pid,
                    windows: st.windows.clone(),
                },
                s.rect,
            )
        };
        let (t, rect) = target;
        let ev = cmd.ev;
        let mods = Modifiers::parse(&ev.modifiers);
        let (ex, ey) = (ev.x.unwrap_or(0.0), ev.y.unwrap_or(0.0));
        let kind = ev.kind.as_str();
        let pointer = matches!(kind, "mouse" | "pointer" | "mousemove" | "pointermove" | "mousedown" | "pointerdown" | "mouseup" | "pointerup" | "move" | "down" | "up" | "wheel");
        if pointer && !in_surface(&rect, ex, ey) {
            // never map a point outside the surface onto the screen (mirror = real OS input)
            dropped_outside += 1;
            if dropped_outside.is_multiple_of(50) || dropped_outside == 1 {
                log::warn!("[{}] {kind} at ({ex},{ey}) outside the {}x{} surface dropped ({dropped_outside} so far)", cmd.surface, rect.w(), rect.h());
            }
            let mut s = inj.stats();
            s["dropped_outside"] = serde_json::json!(dropped_outside);
            *stats.lock().unwrap() = s;
            continue;
        }
        let sx = rect.l + ex.round() as i32;
        let sy = rect.t + ey.round() as i32;
        match kind {
            "mouse" | "pointer" | "mousemove" | "pointermove" | "mousedown" | "pointerdown" | "mouseup" | "pointerup" | "move" | "down" | "up" => {
                let forced_down = match kind {
                    "mousedown" | "pointerdown" | "down" => Some(true),
                    "mouseup" | "pointerup" | "up" => Some(false),
                    _ => ev.down,
                };
                let prev = *last_buttons.get(&cmd.surface).unwrap_or(&0);
                if let (Some(down), Some(b)) = (forced_down, ev.button.and_then(MouseButton::from_dom)) {
                    inj.button(&t, sx, sy, b, down, mods);
                    let nb = if down { prev | b.dom_bit() } else { prev & !b.dom_bit() };
                    last_buttons.insert(cmd.surface.clone(), ev.buttons.unwrap_or(nb));
                } else if let Some(bits) = ev.buttons.filter(|b| *b != prev) {
                    // derive presses / releases from the bitmask difference
                    for b in MouseButton::all() {
                        let bit = b.dom_bit();
                        if bits & bit != 0 && prev & bit == 0 {
                            inj.button(&t, sx, sy, b, true, mods);
                        } else if bits & bit == 0 && prev & bit != 0 {
                            inj.button(&t, sx, sy, b, false, mods);
                        }
                    }
                    last_buttons.insert(cmd.surface.clone(), bits);
                } else {
                    inj.mouse_move(&t, sx, sy, mods);
                }
            }
            "wheel" => {
                inj.wheel(&t, sx, sy, ev.dx.unwrap_or(0.0).round() as i32, ev.dy.unwrap_or(0.0).round() as i32, mods);
            }
            "key" | "keydown" | "keyup" => {
                let down = match kind {
                    "keydown" => true,
                    "keyup" => false,
                    _ => ev.down.unwrap_or(true),
                };
                let code = ev.code.clone().unwrap_or_default();
                if !inj.key(&t, &code, ev.key.as_deref(), down, mods) {
                    log::debug!("[{}] unknown key code={code:?} key={:?}", cmd.surface, ev.key);
                }
            }
            "focus" | "blur" => {
                let gained = if kind == "blur" { false } else { ev.down.unwrap_or(true) };
                inj.focus(&t, gained);
            }
            _ => {}
        }
        n += 1;
        if n.is_multiple_of(16) || kind != "mouse" {
            let mut s = inj.stats();
            s["queue_latency_ms"] = serde_json::json!(cmd.at.elapsed().as_secs_f64() * 1000.0);
            s["dropped_outside"] = serde_json::json!(dropped_outside);
            *stats.lock().unwrap() = s;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surface_bounds() {
        let r = Rect::new(100, 50, 100 + 800, 50 + 600);
        assert!(in_surface(&r, 0.0, 0.0));
        assert!(in_surface(&r, 799.0, 599.0));
        assert!(in_surface(&r, 799.4, 599.4));
        assert!(!in_surface(&r, 800.0, 10.0));
        assert!(!in_surface(&r, 10.0, 600.0));
        assert!(!in_surface(&r, -1.0, 10.0));
        assert!(!in_surface(&r, 1200.0, 900.0));
        assert!(!in_surface(&r, f64::NAN, 1.0));
        assert!(!in_surface(&r, 1.0, f64::INFINITY));
    }

    #[test]
    fn query_params() {
        assert_eq!(query_param("/stream?surface=pcb&token=abc", "surface").as_deref(), Some("pcb"));
        assert_eq!(query_param("/stream?surface=pcb&token=abc", "token").as_deref(), Some("abc"));
        assert_eq!(query_param("/control", "token"), None);
        assert_eq!(query_param("/control?token=a%20b", "token").as_deref(), Some("a b"));
    }
}
