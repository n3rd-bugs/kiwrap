//! kiwrap-live: the Live KiCad helper (docs/DISPLAY.md 7.2 / 12.6, Phase-2 helper contract).
//!
//! Attaches to a KiCad GUI process by PID (the extension / `kw_live` launch it under the kiwrap
//! sandbox), captures every editor frame of that process as its own *surface* with WGC, streams
//! JPEG frames over a loopback WebSocket and injects the client's input back in one of three
//! modes. Prints one JSON line `{port, token, version, pid, helper_pid}` on stdout when ready.

// The helper is one binary crate that also carries small self-contained modules (surface model,
// codec, Win32 seam) with a few helpers kept for the extension/kw_live side and future modes.
#![allow(dead_code)]
#![allow(clippy::too_many_arguments, clippy::result_large_err, clippy::doc_lazy_continuation)]

mod capture;
mod codec;
mod composite;
mod hub;
mod inject;
mod lifetime;
mod protocol;
mod server;
mod surfaces;
mod tracker;
mod win;

use std::sync::Arc;

use clap::Parser;

#[derive(Parser, Debug)]
#[command(name = "kiwrap-live", version, about = "Live KiCad helper: WGC surfaces over a loopback WebSocket")]
struct Args {
    /// PID of the KiCad GUI process to attach to (launched by the caller under the kiwrap sandbox).
    #[arg(long)]
    pid: u32,
    /// Loopback address to listen on; port 0 picks a free port (printed on stdout).
    #[arg(long, default_value = "127.0.0.1:0")]
    listen: String,
    /// Auth token required as `?token=` on every WebSocket handshake (random when omitted).
    #[arg(long)]
    token: Option<String>,
    /// Input injector: none (capture-only), mirror (SendInput), events (PostMessage-only, needs a
    /// KiCad with mr/display-input), hybrid (posted input with a borrowed cursor, stock KiCad).
    #[arg(long, default_value = "none", value_parser = ["none", "mirror", "events", "hybrid"])]
    inject: String,
    /// The KiCad was started with KICAD_INPUT_FROM_EVENTS=cloak: warn when no surface is cloaked.
    #[arg(long)]
    cloak_expected: bool,
    /// JPEG quality 1-100.
    #[arg(long, default_value_t = 70)]
    quality: u32,
    /// Frame-rate cap per surface (0 = uncapped).
    #[arg(long, default_value_t = 30)]
    fps: u32,
    /// Move an owned dialog / chooser that opens outside its editor frame onto the frame (once).
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    gather_dialogs: bool,
    /// Capture popup menus / drop-downs through a monitor crop (they are OS windows that a
    /// per-window capture would dismiss; a cloaked KiCad still shows them on the desktop).
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    menus: bool,
    /// Esc on an owned dialog -> WM_CLOSE: auto (events: native #32770 dialogs only; hybrid:
    /// always), always, never.
    #[arg(long, default_value = "auto", value_parser = ["auto", "always", "never"])]
    esc_close: String,
    /// Dirty-detection tile size in px.
    #[arg(long, default_value_t = 32)]
    tile: u32,
    /// PID to watch for self-termination (default: the parent process).
    #[arg(long)]
    parent_pid: Option<u32>,
    /// Do not exit when the parent dies (debugging).
    #[arg(long)]
    no_parent_watch: bool,
    /// Size a surface's frame to the client's viewport on its `resize` event (1:1 stream).
    #[arg(long)]
    fit_to_view: bool,
    /// Attach to a process that is not a KiCad editor (kicad.exe / pcbnew.exe / eeschema.exe).
    /// Refused otherwise: the helper streams and drives whatever windows the pid owns.
    #[arg(long)]
    allow_any_process: bool,
}

/// Image names the helper attaches to without `--allow-any-process` (reviewer finding 15,
/// 2026-09-20: any pid used to be accepted, so a talked-into `kiwrap.live.attach` could stream
/// and drive a browser or a password manager into the webview).
const KICAD_IMAGES: &[&str] = &["kicad.exe", "pcbnew.exe", "eeschema.exe"];

fn image_allowed(image_path: &str) -> bool {
    let name = image_path.rsplit(['\\', '/']).next().unwrap_or(image_path).to_ascii_lowercase();
    KICAD_IMAGES.contains(&name.as_str())
}

/// Read stdin until EOF, then ask the tracker to stop: the extension closes the helper's stdin
/// as its graceful shutdown signal before it terminates the process (reviewer finding 17). Only
/// armed when stdin is a pipe: a console or NUL stdin reports EOF at once and must not stop us.
fn watch_stdin_eof() -> bool {
    use windows::Win32::Storage::FileSystem::{GetFileType, FILE_TYPE_PIPE};
    use windows::Win32::System::Console::{GetStdHandle, STD_INPUT_HANDLE};
    let is_pipe = unsafe {
        match GetStdHandle(STD_INPUT_HANDLE) {
            Ok(h) if !h.is_invalid() => GetFileType(h) == FILE_TYPE_PIPE,
            _ => false,
        }
    };
    if !is_pipe {
        return false;
    }
    std::thread::Builder::new()
        .name("stdin-eof".into())
        .spawn(|| {
            use std::io::Read;
            let mut buf = [0u8; 256];
            let mut stdin = std::io::stdin().lock();
            loop {
                match stdin.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => continue,
                }
            }
            log::info!("stdin closed by the launcher; stopping");
            tracker::request_stop();
        })
        .is_ok()
}

fn random_token() -> String {
    use base64::Engine;
    use rand::RngCore;
    let mut b = [0u8; 18];
    rand::rng().fill_bytes(&mut b);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b)
}

fn main() {
    let t_start = std::time::Instant::now();
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_millis()
        .target(env_logger::Target::Stderr)
        .init();
    let args = Args::parse();
    win::set_dpi_aware();
    if !win::process_alive(args.pid) {
        eprintln!("kiwrap-live: process {} is not running", args.pid);
        std::process::exit(2);
    }
    let image = win::process_image(args.pid).unwrap_or_default();
    if !args.allow_any_process && !image_allowed(&image) {
        eprintln!(
            "kiwrap-live: process {} is {:?}, not a KiCad editor ({}); pass --allow-any-process to attach anyway",
            args.pid,
            if image.is_empty() { "<unknown>".to_string() } else { image.clone() },
            KICAD_IMAGES.join(", ")
        );
        std::process::exit(4);
    }
    let inject = match args.inject.as_str() {
        "mirror" => hub::InjectMode::Mirror,
        "events" => hub::InjectMode::Events,
        "hybrid" => hub::InjectMode::Hybrid,
        _ => hub::InjectMode::None,
    };
    let esc_close = match args.esc_close.as_str() {
        "always" => hub::EscClose::Always,
        "never" => hub::EscClose::Never,
        _ => hub::EscClose::Auto,
    };
    let cfg = hub::Config {
        pid: args.pid,
        quality: args.quality.clamp(1, 100),
        fps: args.fps,
        gather_dialogs: args.gather_dialogs,
        inject,
        cloak_expected: args.cloak_expected,
        tile: args.tile.max(8),
        esc_close,
        menus: args.menus,
        fit_to_view: args.fit_to_view,
    };
    let hub = hub::Hub::new(cfg);
    let token = args.token.clone().unwrap_or_else(random_token);
    log::info!("attached to pid {} ({})", args.pid, if image.is_empty() { "image unknown" } else { image.as_str() });

    // graceful stop: the launcher closes our stdin
    if watch_stdin_eof() {
        log::info!("stdin EOF stops the helper");
    }

    // lifetime guard
    if !args.no_parent_watch {
        let parent = args.parent_pid.or_else(lifetime::parent_pid);
        match parent {
            Some(p) => {
                let what = lifetime::guard(p, tracker::request_stop);
                log::info!("lifetime guard on parent {p}: {what}");
            }
            None => log::warn!("parent pid unknown; no lifetime guard"),
        }
    }

    // tracker thread (SetWinEventHook message loop)
    let hub_t = hub.clone();
    let tracker_thread =
        std::thread::Builder::new().name("tracker".into()).spawn(move || tracker::run(hub_t)).expect("tracker");
    let tracker_thread = Arc::new(std::sync::Mutex::new(Some(tracker_thread)));
    let tracker_probe = tracker_thread.clone();

    // injector thread
    let injector: Box<dyn inject::Injector> = match inject {
        hub::InjectMode::Mirror => Box::new(inject::mirror::MirrorInjector::new()),
        hub::InjectMode::Events => Box::new(inject::events::EventInjector::new(esc_close)),
        hub::InjectMode::Hybrid => Box::new(inject::hybrid::HybridInjector::new()),
        hub::InjectMode::None => Box::new(inject::NoInjector { dropped: 0 }),
    };
    let (input_tx, input_rx) = std::sync::mpsc::channel::<server::InputCmd>();
    let injector_stats = Arc::new(std::sync::Mutex::new(injector.stats()));
    {
        let hub_i = hub.clone();
        let stats = injector_stats.clone();
        std::thread::Builder::new()
            .name("injector".into())
            .spawn(move || server::injector_thread(hub_i, injector, input_rx, stats))
            .expect("injector");
    }

    // server
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().expect("tokio");
    let listen = args.listen.clone();
    let hub_s = hub.clone();
    let token_s = token.clone();
    rt.block_on(async move {
        let listener = match tokio::net::TcpListener::bind(&listen).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("kiwrap-live: bind {listen}: {e}");
                std::process::exit(3);
            }
        };
        let port = listener.local_addr().map(|a| a.port()).unwrap_or(0);
        let hello = protocol::Hello {
            port,
            token: &token_s,
            version: protocol::VERSION,
            pid: hub_s.cfg.pid,
            helper_pid: std::process::id(),
        };
        println!("{}", serde_json::to_string(&hello).unwrap());
        use std::io::Write;
        std::io::stdout().flush().ok();
        log::info!(
            "listening on 127.0.0.1:{port} pid={} inject={} fps={} q={} gather={} menus={} fit={} ({} ms to ready)",
            hub_s.cfg.pid,
            hub_s.cfg.inject.as_str(),
            hub_s.cfg.fps,
            hub_s.cfg.quality,
            hub_s.cfg.gather_dialogs,
            hub_s.cfg.menus,
            hub_s.cfg.fit_to_view,
            t_start.elapsed().as_millis()
        );
        let srv = Arc::new(server::Server { hub: hub_s.clone(), token: token_s, input_tx, injector_stats });
        let accept = tokio::spawn(server::run(srv, listener));
        // exit when the tracker stops (process gone / parent gone)
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            let done = tracker_probe.lock().unwrap().as_ref().map(|t| t.is_finished()).unwrap_or(true);
            if done || hub_s.exiting.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }
        }
        accept.abort();
    });
    if let Some(t) = tracker_thread.lock().unwrap().take() {
        let _ = t.join();
    }
    log::info!("bye");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_kicad_editors_are_attached_by_default() {
        assert!(image_allowed(r"C:\Program Files\KiCad\10.99\bin\pcbnew.exe"));
        assert!(image_allowed("C:/kiwrap-builds/6dd893f7d1-7/bin/EESCHEMA.EXE"));
        assert!(image_allowed(r"C:\Program Files\KiCad\10.0\bin\kicad.exe"));
        assert!(!image_allowed(r"C:\Windows\System32\charmap.exe"));
        assert!(!image_allowed(r"C:\Program Files\KiCad\10.99\bin\kicad-cli.exe"));
        assert!(!image_allowed(""));
    }
}
