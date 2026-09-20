#!/usr/bin/env node
// Fake kiwrap-live helper: speaks the helper protocol (extension/src/shared/types.ts, section 1) without
// capturing anything. Used by the integration test and for developing the webview without KiCad.
//
//   node fake_helper.mjs --pid <pid> --listen 127.0.0.1:0 [--token <t>] [--inject none|mirror|events] [--cloak-expected]
//                        [--fit-to-view] [--allow-any-process]
//   (like kiwrap-live, a token is minted when none is given and reported in the hello line)
//
// stdout: one JSON line {port, token, version}. Endpoints: /control (hello with the surfaces on connect,
// surfaces / screenshot / raise / keyframe requests, `{type:'event', event:'create'}` push of a 3D surface after
// KIWRAP_FAKE_3D_DELAY_MS; rect_px is [l,t,r,b] like kiwrap-live) and /stream?surface=<id> (meta + synthetic JPEG frames: gradient,
// a bar that moves with the frame id, a crosshair at the last pointer position received, a key ticker).
// Frames flow for KIWRAP_FAKE_ACTIVE_MS after a connect and after every input event, else nothing (idle = 0).
// Every input event received is appended to KIWRAP_FAKE_HELPER_LOG (JSONL) when set.
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { acceptUpgrade } from './ws.mjs';
import { encodeJpeg } from './jpeg.mjs';
import { encodePng } from './png.mjs';

const VERSION = 'fake-0.1.0';
const argv = process.argv.slice(2);
const opt = (name, dflt) => { const i = argv.indexOf(name); return i >= 0 && i + 1 < argv.length ? argv[i + 1] : dflt; };
const pid = Number(opt('--pid', '0'));
const listen = opt('--listen', '127.0.0.1:0');
const token = opt('--token', '') || crypto.randomBytes(18).toString('base64url');
const fitToView = argv.includes('--fit-to-view');
const inject = opt('--inject', 'none');
const cloak = argv.includes('--cloak-expected');
const [host, portStr] = listen.split(':');
const fps = Number(process.env.KIWRAP_FAKE_FPS ?? 15);
const activeMs = Number(process.env.KIWRAP_FAKE_ACTIVE_MS ?? 3000);
const logPath = process.env.KIWRAP_FAKE_HELPER_LOG ?? '';
const delay3d = Number(process.env.KIWRAP_FAKE_3D_DELAY_MS ?? 1500);
const quality = Number(process.env.KIWRAP_FAKE_QUALITY ?? 60);

const log = (rec) => { if (logPath) { try { fs.appendFileSync(logPath, JSON.stringify({ t: Date.now(), ...rec }) + '\n'); } catch { /* ignore */ } } };
const stderr = (s) => process.stderr.write(`[fake-helper] ${s}\n`);

// ---- surfaces ----------------------------------------------------------------------------------------
const mk = (id, kind, title, x, y, w, h) => ({ id, kind, title, hwnd: Math.floor(Math.random() * 1e6) + 1000, rect_px: [x, y, x + w, y + h], frame_px: [x, y, x + w, y + h], dpi: 144, cloaked: cloak, cloak_flags: cloak ? 2 : 0, enabled: true, foreground: false, dialogs: [] });
const sizeOf = (s) => ({ w: s.rect_px[2] - s.rect_px[0], h: s.rect_px[3] - s.rect_px[1] });
const surfaces = new Map();
const initial = process.env.KIWRAP_FAKE_SURFACES ? JSON.parse(process.env.KIWRAP_FAKE_SURFACES) : [
  mk('pcb-1', 'pcb', 'ecc83-pp.kicad_pcb [ecc83-pp/] - PCB Editor', 100, 100, 960, 600),
  mk('sch-1', 'sch', 'ecc83-pp.kicad_sch [ecc83-pp/] - Schematic Editor', 200, 150, 960, 600),
];
for (const s of initial) { surfaces.set(s.id, s); }
const controls = new Set();
const broadcast = (m) => { const t = JSON.stringify(m); for (const c of controls) { c.sendText(t); } };
if (delay3d > 0) {
  setTimeout(() => { const s = mk('3d-1', '3d', 'ecc83-pp.kicad_pcb - 3D Viewer', 400, 300, 960, 640); surfaces.set(s.id, s); broadcast({ type: 'event', event: 'create', surface: s }); }, delay3d).unref();
}

// ---- synthetic picture ------------------------------------------------------------------------------
const per = new Map();   // surface id -> { frameId, pointer:{x,y}, keys:[], lastInputAt }
const st = (id) => { let s = per.get(id); if (!s) { s = { frameId: 0, pointer: null, keys: [], lastInputAt: 0, downs: 0 }; per.set(id, s); } return s; };
function render(surface, s, w, h) {
  const rgba = new Uint8Array(w * h * 4);
  const bar = (s.frameId * 7) % w;
  const hue = surface.kind === 'pcb' ? 0 : surface.kind === 'sch' ? 1 : 2;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const g = (x * 255 / w) | 0, v = (y * 255 / h) | 0;
      let r = hue === 0 ? 40 + (g >> 1) : 30, gg = hue === 1 ? 40 + (g >> 1) : 40 + (v >> 2), b = hue === 2 ? 60 + (g >> 1) : 60;
      if (Math.abs(x - bar) < 4) { r = 255; gg = 220; b = 0; }
      if (((x >> 5) + (y >> 5)) & 1) { r += 8; gg += 8; b += 8; }
      rgba[i] = r; rgba[i + 1] = gg; rgba[i + 2] = b; rgba[i + 3] = 255;
    }
  }
  if (s.pointer) {
    const px = Math.max(0, Math.min(w - 1, s.pointer.x | 0)), py = Math.max(0, Math.min(h - 1, s.pointer.y | 0));
    for (let d = -12; d <= 12; d++) {
      const a = (py * w + Math.max(0, Math.min(w - 1, px + d))) * 4, c = (Math.max(0, Math.min(h - 1, py + d)) * w + px) * 4;
      rgba[a] = rgba[a + 1] = rgba[a + 2] = 255; rgba[c] = rgba[c + 1] = rgba[c + 2] = 255;
    }
  }
  // key ticker: one 6x6 white block per key currently down (top-left)
  s.keys.forEach((_, k) => { for (let y = 4; y < 10; y++) { for (let x = 4 + k * 8; x < 10 + k * 8; x++) { const i = (y * w + x) * 4; rgba[i] = rgba[i + 1] = rgba[i + 2] = 255; } } });
  return rgba;
}

// ---- server ----------------------------------------------------------------------------------------
const server = http.createServer((req, res) => { res.writeHead(404, { 'content-type': 'text/plain' }); res.end('kiwrap fake helper: websocket only'); });
server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  if (url.searchParams.get('token') !== token) { socket.end('HTTP/1.1 401 Unauthorized\r\n\r\n'); log({ ev: 'unauthorized', path: url.pathname }); return; }
  if (url.pathname === '/control') { const c = acceptUpgrade(req, socket, head); if (c) { handleControl(c); } return; }
  if (url.pathname === '/stream') { const c = acceptUpgrade(req, socket, head); if (c) { handleStream(c, url.searchParams.get('surface') ?? ''); } return; }
  socket.end('HTTP/1.1 404 Not Found\r\n\r\n');
});

function handleControl(c) {
  controls.add(c); log({ ev: 'control-open' });
  c.sendText(JSON.stringify({ type: 'hello', version: VERSION, pid, inject, cloak_expected: cloak, surfaces: [...surfaces.values()] }));
  c.on('message', (data, isBinary) => {
    if (isBinary) { return; }
    let m; try { m = JSON.parse(data); } catch { return; }
    log({ ev: 'control', msg: m });
    const reply = (o) => c.sendText(JSON.stringify(m.id != null ? { ...o, id: m.id } : o));
    if (m.type === 'surfaces') { reply({ type: 'surfaces', surfaces: [...surfaces.values()] }); }
    else if (m.type === 'screenshot') {
      const s = surfaces.get(m.surface);
      if (!s) { reply({ type: 'screenshot', surface: m.surface, ok: false, error: 'no such surface' }); return; }
      const max = Math.max(16, Math.min(4096, Number(m.max_px) || 1024));
      const sz = sizeOf(s);
      const k = Math.min(1, max / Math.max(sz.w, sz.h));
      const w = Math.max(8, Math.round(sz.w * k)), h = Math.max(8, Math.round(sz.h * k));
      const png = encodePng(render(s, st(s.id), w, h), w, h);
      reply({ type: 'screenshot', surface: s.id, ok: true, w, h, format: 'png', bytes: png.length, rect_px: s.rect_px, cloaked: s.cloaked, png: png.toString('base64') });
    }
    else if (m.type === 'raise') { const s = surfaces.get(m.surface); log({ ev: 'raise', surface: m.surface }); reply(s ? { type: 'raise', surface: m.surface, ok: !s.cloaked, ...(s.cloaked ? { reason: 'cloaked' } : {}) } : { type: 'raise', surface: m.surface, ok: false, reason: 'no such surface' }); }
    else if (m.type === 'keyframe') { const s = st(m.surface); s.lastInputAt = Date.now(); log({ ev: 'keyframe', surface: m.surface }); reply({ type: 'keyframe', surface: m.surface }); }
    else if (m.type === 'ping') { reply({ type: 'pong', t: Date.now() }); }
    else { reply({ type: 'error', error: `unknown request type ${JSON.stringify(m.type)}` }); }
  });
  c.on('close', () => { controls.delete(c); log({ ev: 'control-close' }); });
  c.on('error', () => { /* logged on close */ });
}

function handleStream(c, surfaceId) {
  const surface = surfaces.get(surfaceId);
  if (!surface) { log({ ev: 'stream-unknown', surface: surfaceId }); c.sendText(JSON.stringify({ type: 'error', error: `no such surface ${JSON.stringify(surfaceId)}`, surfaces: [...surfaces.keys()] })); c.close(1000, ''); return; }
  const s = st(surfaceId);
  const { w, h } = sizeOf(surface);
  log({ ev: 'stream-open', surface: surfaceId });
  c.sendText(JSON.stringify({ type: 'meta', w, h, dpi_scale: surface.dpi / 96, dirty: [[0, 0, w, h]], frame_id: s.frameId, surface: surfaceId, rect_px: surface.rect_px, inject }));
  s.lastInputAt = Date.now();
  let busy = false;
  const tick = () => {
    if (c.closed || busy) { return; }
    if (Date.now() - s.lastInputAt > activeMs) { return; }   // idle: 0 frames
    if (c.bufferedAmount > 512 * 1024) { return; }            // drop-to-latest on the server side
    busy = true;
    s.frameId++;
    const jpg = encodeJpeg(render(surface, s, w, h), w, h, quality);
    const out = Buffer.alloc(8 + jpg.length);
    out.writeUInt32LE(s.frameId, 0); out.writeUInt16LE(w, 4); out.writeUInt16LE(h, 6); jpg.copy(out, 8);
    c.sendBinary(out); busy = false;
    log({ ev: 'frame', surface: surfaceId, frameId: s.frameId, bytes: out.length });
  };
  tick();
  const timer = setInterval(tick, Math.max(10, 1000 / fps));
  c.on('message', (data, isBinary) => {
    if (isBinary) { return; }
    let ev; try { ev = JSON.parse(data); } catch { return; }
    log({ ev: 'input', surface: surfaceId, event: ev });
    if (ev.type === 'mouse' || ev.type === 'wheel') { s.pointer = { x: ev.x, y: ev.y }; if (ev.type === 'mouse' && ev.down === true) { s.downs++; } }
    if (ev.type === 'key') { const i = s.keys.indexOf(ev.code); if (ev.down && i < 0) { s.keys.push(ev.code); } if (!ev.down && i >= 0) { s.keys.splice(i, 1); } }
    if (ev.type === 'clipboard') { log({ ev: 'clipboard', text: ev.text }); }
    if (ev.type === 'ping') { c.sendText(JSON.stringify({ type: 'pong', id: ev.id, t_server: Date.now() })); return; }
    s.lastInputAt = Date.now();
    const ctrl = ev.modifiers && typeof ev.modifiers === 'object' ? !!ev.modifiers.ctrl : typeof ev.modifiers === 'number' ? !!(ev.modifiers & 1) : false;
    if (process.env.KIWRAP_FAKE_ECHO_CLIPBOARD && ev.type === 'key' && ev.down && ev.code === 'KeyC' && ctrl) {
      c.sendText(JSON.stringify({ type: 'meta', w, h, dpi_scale: surface.dpi / 96, clipboard: process.env.KIWRAP_FAKE_ECHO_CLIPBOARD }));
    }
  });
  c.on('close', () => { clearInterval(timer); log({ ev: 'stream-close', surface: surfaceId }); });
  c.on('error', () => { /* logged on close */ });
}

server.listen(Number(portStr) || 0, host || '127.0.0.1', () => {
  const port = server.address().port;
  process.stdout.write(JSON.stringify({ port, token, version: VERSION }) + '\n');
  stderr(`listening on ${host}:${port} pid=${pid} inject=${inject} cloak=${cloak} fit=${fitToView}`);
  log({ ev: 'hello', port, pid, inject, cloak, fit_to_view: fitToView });
});
const bye = (why) => { stderr(`exit: ${why}`); log({ ev: 'exit', why }); server.close(); setTimeout(() => process.exit(0), 100).unref(); };
process.stdin.on('end', () => bye('stdin closed'));
process.stdin.on('error', () => bye('stdin error'));
process.stdin.resume();
process.on('SIGTERM', () => bye('SIGTERM'));
process.on('SIGINT', () => bye('SIGINT'));
if (process.env.KIWRAP_FAKE_WATCH_PID === '1' && pid > 0) {
  setInterval(() => { try { process.kill(pid, 0); } catch { bye(`pid ${pid} gone`); } }, 2000).unref();
}
if (process.env.KIWRAP_FAKE_CRASH_AFTER_MS) { setTimeout(() => { stderr('simulated crash'); process.exit(3); }, Number(process.env.KIWRAP_FAKE_CRASH_AFTER_MS)); }
