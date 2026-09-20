// Live KiCad webview client: one KiCad *surface* (pcb / sch / 3d / ...) per tab.
// Frames arrive on ws://127.0.0.1:<port>/stream?surface=<id>&token=<t> as [u32 frame_id][u16 w][u16 h] + JPEG/WebP,
// are decoded with createImageBitmap and painted onto a <canvas>. In the live view (mode 'view', the product
// path - docs/DECISIONS.md D20) nothing is sent back but the resize hint: the picture is live, editing by hand
// happens in the real KiCad window ("Raise KiCad" button, double-click). Only the experimental input modes
// ('mirror' / 'hidden' pages) forward mouse / wheel / keys as JSON (see protocol.js and
// extension/src/shared/types.ts). The extension host configures the page through postMessage ({type:'config'}).
import * as P from './protocol.js';

const vscode = typeof acquireVsCodeApi === 'function' ? acquireVsCodeApi() : { postMessage: (m) => window.__liveHostMessages?.push(m) };
const $ = (id) => document.getElementById(id);
const viewport = $('viewport'), canvas = $('canvas'), stage = $('stage'), hud = $('hud'), card = $('card');
const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });

const state = {
  cfg: null,                    // last HostToWebview config
  surface: null,
  mode: 'view',
  options: { pointerLock: false, hud: true, fit: 'fit', hiddenCloseDelayMs: 1500, raiseOnDoubleClick: true },
  ws: null, connected: false, attempt: 0, wantOpen: false, reconnectTimer: null, hiddenTimer: null,
  frameW: 0, frameH: 0, dpiScale: 1, lastMeta: null,
  layout: { cssW: 0, cssH: 0, left: 0, top: 0, scale: 1 },
  decoding: false, pending: null,
  stats: new P.FrameStats(),
  focused: false, downKeys: new Set(), buttons: 0, capturedPointer: null,
  locked: false, virt: { x: 0, y: 0 },
  visible: !document.hidden, hostVisible: true,
  inputCount: 0, lastInput: null,
  clipboardWaiters: new Map(), clipboardSeq: 0,
};
window.__live = state;   // test hook

// ---------------------------------------------------------------------------------- layout / painting
function relayout() {
  const l = P.fitLayout({ frameW: state.frameW, frameH: state.frameH, viewW: viewport.clientWidth, viewH: viewport.clientHeight, dpr: devicePixelRatio, mode: state.options.fit });
  state.layout = l;
  viewport.classList.toggle('scrolls', !!l.scrolls);   // native mode: a frame larger than the tab scrolls
  stage.style.width = l.cssW + 'px'; stage.style.height = l.cssH + 'px';
  stage.style.left = l.left + 'px'; stage.style.top = l.top + 'px';
  canvas.style.width = l.cssW + 'px'; canvas.style.height = l.cssH + 'px';
}
// The viewport size goes to the helper as a `resize` event (with --fit-to-view it sizes the KiCad
// window to the tab so the stream is 1:1); trailing-debounced so a drag does not resize the window
// on every step.
let resizeTimer = null;
function scheduleResize() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { resizeTimer = null; sendResize(); }, 150);
}
new ResizeObserver(() => { relayout(); scheduleResize(); }).observe(viewport);
window.addEventListener('resize', relayout);

function ensureCanvasSize(w, h) {
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w; canvas.height = h;
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h);
  }
  if (state.frameW !== w || state.frameH !== h) { state.frameW = w; state.frameH = h; relayout(); }
}

async function paint(job) {
  const t0 = performance.now();
  let bmp;
  try {
    bmp = await createImageBitmap(new Blob([job.payload], { type: job.header.mime }));
  } catch (e) {
    log('warn', 'decode failed', { frameId: job.header.frameId, codec: job.header.codec, bytes: job.payload.byteLength, error: String(e) });
    return;
  }
  const hdr = job.header;
  const dirty = state.lastMeta?.dirty;
  const partial = state.frameW > 0 && (hdr.w !== state.frameW || hdr.h !== state.frameH) && Array.isArray(dirty) && dirty.length === 1 && dirty[0].w === hdr.w && dirty[0].h === hdr.h;
  if (partial) {
    ctx.drawImage(bmp, dirty[0].x | 0, dirty[0].y | 0);
  } else {
    ensureCanvasSize(hdr.w, hdr.h);
    ctx.drawImage(bmp, 0, 0);
  }
  bmp.close();
  state.stats.onFrame(job.bytes, performance.now() - t0, hdr.frameId);
  card.classList.add('hidden');
}

function onBinary(buf) {
  let header;
  try { header = P.parseFrameHeader(buf); } catch (e) { log('warn', String(e)); return; }
  const job = { header, payload: new Uint8Array(buf, header.payloadOffset), bytes: buf.byteLength };
  if (state.decoding) { state.pending = job; return; }   // drop-to-latest
  run(job);
}
async function run(job) {
  state.decoding = true;
  try { await paint(job); } finally { state.decoding = false; }
  if (state.pending) { const n = state.pending; state.pending = null; run(n); }
}

function onText(text) {
  const meta = P.parseMeta(text);
  if (!meta) { return; }
  state.lastMeta = meta;
  if (meta.w > 0 && meta.h > 0) { ensureCanvasSize(meta.w, meta.h); }
  if (meta.dpi_scale) { state.dpiScale = meta.dpi_scale; }
  if (meta.inject) { setMode(meta.inject === 'events' ? 'hidden' : meta.inject === 'mirror' || meta.inject === 'hybrid' ? 'mirror' : 'view'); }
  if (typeof meta.clipboard === 'string') { vscode.postMessage({ type: 'clipboard.write', text: meta.clipboard }); }
}

// ---------------------------------------------------------------------------------- websocket lifecycle
function connect() {
  clearTimeout(state.reconnectTimer); state.reconnectTimer = null;
  if (!state.wantOpen || !state.cfg?.stream || !state.surface) { return; }
  if (state.ws && (state.ws.readyState === 0 || state.ws.readyState === 1)) { return; }
  const url = P.streamUrl(state.cfg.stream.url, state.surface.id, state.cfg.stream.token);
  let ws;
  try { ws = new WebSocket(url); } catch (e) { log('error', 'WebSocket ctor failed', { url, error: String(e) }); scheduleReconnect(); return; }
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  setStatus('connecting');
  ws.onopen = () => {
    state.connected = true; state.attempt = 0; setStatus('connected');
    // A fresh connection gets a full frame from the helper: that is the resync after a hidden tab.
    sendResize();
    if (state.focused) { send(P.focus(true)); }
  };
  ws.onmessage = (ev) => { if (typeof ev.data === 'string') { onText(ev.data); } else { onBinary(ev.data); } };
  ws.onerror = () => log('warn', 'stream socket error');
  ws.onclose = (ev) => {
    if (state.ws === ws) { state.ws = null; }
    state.connected = false; setStatus(state.wantOpen ? 'disconnected' : 'idle');
    releaseAll();
    if (state.wantOpen) { scheduleReconnect(ev.code); }
  };
}
function scheduleReconnect() {
  if (state.reconnectTimer || !state.wantOpen) { return; }
  const delay = P.backoffMs(state.attempt++);
  state.reconnectTimer = setTimeout(() => { state.reconnectTimer = null; connect(); }, delay);
}
function disconnect() {
  state.wantOpen = false;
  clearTimeout(state.reconnectTimer); state.reconnectTimer = null;
  const ws = state.ws; state.ws = null;
  if (ws) { try { ws.close(1000, 'client'); } catch { /* ignore */ } }
  state.connected = false; releaseAll();
}
function send(ev) {
  const ws = state.ws;
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify(ev));
    state.inputCount++; state.lastInput = ev;
    if (ev.type === 'mouse' || ev.type === 'wheel' || ev.type === 'key') { state.stats.onInputSent(); }
    return true;
  }
  return false;
}
function sendResize() {
  if (!state.connected) { return; }
  send(P.resize(viewport.clientWidth * devicePixelRatio, viewport.clientHeight * devicePixelRatio, devicePixelRatio));
}

// Visibility: a hidden tab (retainContextWhenHidden keeps the page alive) closes its stream after a
// grace period and reconnects when shown again; the reconnect delivers a full frame (resync).
function updateWantOpen() {
  const shouldOpen = !!state.cfg?.stream && !!state.surface && state.visible && state.hostVisible;
  const wasHidden = state.hiddenTimer !== null || !state.wantOpen;
  clearTimeout(state.hiddenTimer); state.hiddenTimer = null;
  if (shouldOpen) {
    state.wantOpen = true; connect();
    if (wasHidden && state.connected) { send(P.keyframe()); }   // still open: ask for a full frame instead of reconnecting
  }
  else if (state.wantOpen) {
    state.hiddenTimer = setTimeout(() => { if (!(state.visible && state.hostVisible)) { disconnect(); setStatus('hidden'); } }, state.options.hiddenCloseDelayMs);
  }
}
document.addEventListener('visibilitychange', () => { state.visible = !document.hidden; updateWantOpen(); });

// ---------------------------------------------------------------------------------- input capture
const opts = { passive: false };
function surfacePoint(e) {
  if (state.locked) { return state.virt; }
  return P.clientToSurface(e.clientX, e.clientY, canvas.getBoundingClientRect(), state.frameW || canvas.width || 1, state.frameH || canvas.height || 1);
}
function inputAllowed() { return state.mode !== 'view' && state.connected; }
let lastMoveAt = 0;
stage.addEventListener('pointermove', (e) => {
  if (!inputAllowed()) { return; }
  if (state.locked) {
    state.virt.x = P.clamp(state.virt.x + e.movementX * state.layout.scale, 0, state.frameW - 1);
    state.virt.y = P.clamp(state.virt.y + e.movementY * state.layout.scale, 0, state.frameH - 1);
    placeVirtualCursor();
  }
  const now = performance.now();
  if (now - lastMoveAt < 6) { return; }  // ~160 Hz cap; helper coalesces further
  lastMoveAt = now;
  send(P.mouseMove(surfacePoint(e), e.buttons, P.modifiersOf(e)));
}, opts);
stage.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  stage.focus({ preventScroll: true });
  // live view: a click only focuses the tab; the primary action is "Raise KiCad" (button / double-click)
  if (state.mode === 'view') { return; }
  if (!state.connected) { return; }
  if (state.options.pointerLock && !state.locked && e.pointerType === 'mouse') { try { canvas.requestPointerLock(); } catch { /* ignore */ } }
  try { stage.setPointerCapture(e.pointerId); state.capturedPointer = e.pointerId; } catch { /* ignore */ }
  state.buttons = e.buttons;
  send(P.mouseButton(surfacePoint(e), e.button, e.buttons, true, P.modifiersOf(e)));
}, opts);
stage.addEventListener('pointerup', (e) => {
  e.preventDefault();
  if (!inputAllowed()) { return; }
  state.buttons = e.buttons;
  send(P.mouseButton(surfacePoint(e), e.button, e.buttons, false, P.modifiersOf(e)));
  if (state.capturedPointer === e.pointerId && e.buttons === 0) { try { stage.releasePointerCapture(e.pointerId); } catch { /* ignore */ } state.capturedPointer = null; }
}, opts);
stage.addEventListener('dblclick', (e) => {
  e.preventDefault();
  if (state.mode === 'view' && state.options.raiseOnDoubleClick && e.button === 0) { raiseKicad('dblclick'); }
});
stage.addEventListener('pointercancel', () => releaseButtons());
stage.addEventListener('contextmenu', (e) => e.preventDefault());
stage.addEventListener('auxclick', (e) => e.preventDefault());
stage.addEventListener('dragstart', (e) => e.preventDefault());
stage.addEventListener('wheel', (e) => {
  e.preventDefault();
  if (!inputAllowed()) { return; }
  send(P.wheel(surfacePoint(e), e.deltaX, e.deltaY, e.deltaMode, P.modifiersOf(e)));
}, opts);

window.addEventListener('keydown', (e) => {
  if (e.target && e.target.closest && e.target.closest('#hud')) { return; }
  e.preventDefault();
  if (P.shouldSwallowKey(e)) { return; }
  if (state.mode === 'view') { return; }
  if (e.repeat && !state.downKeys.has(e.code)) { return; }
  state.downKeys.add(e.code);
  if (e.ctrlKey && !e.altKey && (e.code === 'KeyV' || (e.shiftKey && e.code === 'Insert'))) {
    // clipboard bridge: hand the host clipboard text to the helper before the paste chord.
    const ev = P.key(e, true);
    readHostClipboard(150).then((text) => { if (text) { send(P.clipboard(text)); } send(ev); });
    return;
  }
  send(P.key(e, true));
}, opts);
window.addEventListener('keyup', (e) => {
  if (e.target && e.target.closest && e.target.closest('#hud')) { return; }
  e.preventDefault();
  if (state.mode === 'view') { return; }
  state.downKeys.delete(e.code);
  send(P.key(e, false));
}, opts);
window.addEventListener('paste', (e) => {
  const text = e.clipboardData?.getData('text');
  if (text && state.mode !== 'view') { e.preventDefault(); send(P.clipboard(text)); }
});
function releaseButtons() {
  if (state.buttons) {
    for (let b = 0; b < 5; b++) { if (state.buttons & (1 << b)) { send(P.mouseButton(state.virt, [0, 2, 1, 3, 4][b], 0, false, P.NO_MODS)); } }
    state.buttons = 0;
  }
}
function releaseAll() {
  for (const code of state.downKeys) { send({ type: 'key', key: '', code, modifiers: P.NO_MODS, down: false }); }
  state.downKeys.clear();
  releaseButtons();
}
stage.addEventListener('focus', () => { state.focused = true; send(P.focus(true)); hud.classList.add('focused'); });
stage.addEventListener('blur', () => { state.focused = false; releaseAll(); send(P.focus(false)); hud.classList.remove('focused'); });
window.addEventListener('blur', () => { releaseAll(); });
window.addEventListener('focus', () => { if (state.cfg?.stream) { stage.focus({ preventScroll: true }); } });

document.addEventListener('pointerlockchange', () => {
  state.locked = document.pointerLockElement === canvas;
  $('cursor').classList.toggle('hidden', !state.locked);
  if (state.locked) { placeVirtualCursor(); }
});
function placeVirtualCursor() {
  const r = canvas.getBoundingClientRect();
  const c = P.surfaceToClient(state.virt.x, state.virt.y, r, state.frameW || 1, state.frameH || 1);
  const cur = $('cursor'); cur.style.left = c.x + 'px'; cur.style.top = c.y + 'px';
}

// ---------------------------------------------------------------------------------- clipboard bridge
function readHostClipboard(timeoutMs) {
  return new Promise((resolve) => {
    const id = ++state.clipboardSeq;
    const t = setTimeout(() => { state.clipboardWaiters.delete(id); resolve(''); }, timeoutMs);
    state.clipboardWaiters.set(id, (text) => { clearTimeout(t); resolve(text); });
    vscode.postMessage({ type: 'clipboard.read', id });
  });
}

// ---------------------------------------------------------------------------------- HUD / status
// 'view' is the live view (the product path); the other two are the experimental input modes.
const modeLabel = { view: 'live view', mirror: 'mirror input (experimental)', hidden: 'cloaked input (experimental)' };
function setMode(mode) {
  state.mode = mode;
  hud.dataset.mode = mode;
  $('hud-mode').textContent = modeLabel[mode] ?? mode;
  $('btn-raise').classList.toggle('hidden', mode !== 'view');
  $('btn-lock').classList.toggle('hidden', mode === 'view' || !state.options.pointerLock);
  stage.classList.toggle('view-only', mode === 'view');
  stage.title = mode === 'view' ? 'Live view of the real KiCad editor. To edit by hand: Raise KiCad (button, or double-click).' : '';
}
function setStatus(s) { $('hud-conn').textContent = s; hud.dataset.conn = s; }
function raiseKicad(source) { state.raiseCount = (state.raiseCount ?? 0) + 1; state.lastRaise = source; vscode.postMessage({ type: 'raise' }); }
$('btn-raise').addEventListener('click', () => raiseKicad('button'));
$('btn-lock').addEventListener('click', () => { if (state.locked) { document.exitPointerLock(); } else { canvas.requestPointerLock(); } });
$('btn-hud').addEventListener('click', () => hud.classList.toggle('collapsed'));
$('btn-start').addEventListener('click', () => vscode.postMessage({ type: 'start' }));
setInterval(() => {
  const s = state.stats.sample();
  const stats = { ...s, w: state.frameW, h: state.frameH, scale: +state.layout.scale.toFixed(3), dpr: devicePixelRatio, connected: state.connected, focused: state.focused };
  $('hud-fps').textContent = `${s.fps} fps`;
  $('hud-kbps').textContent = `${s.kbps} kbit/s`;
  $('hud-lat').textContent = s.inputToFrameMs == null ? 'lat -' : `lat ${s.inputToFrameMs} ms`;
  $('hud-size').textContent = state.frameW ? `${state.frameW}x${state.frameH} @${(1 / state.layout.scale * devicePixelRatio).toFixed(2)}` : '';
  vscode.postMessage({ type: 'stats', stats });
}, 1000);

function showCard(title, text, canStart) {
  $('card-title').textContent = title; $('card-text').textContent = text ?? '';
  $('btn-start').classList.toggle('hidden', !canStart);
  card.classList.remove('hidden');
}
function log(level, message, data) { vscode.postMessage({ type: 'log', level, message, data }); }

// ---------------------------------------------------------------------------------- host messages
window.addEventListener('message', (ev) => {
  const m = ev.data;
  if (!m || typeof m !== 'object') { return; }
  switch (m.type) {
    case 'config': {
      state.cfg = m; state.options = { ...state.options, ...(m.options ?? {}) };
      hud.classList.toggle('hidden', !state.options.hud);
      setMode(m.mode ?? 'view');
      applySurface(m.surface ?? null);
      if (!m.stream || !m.surface) { showCard(m.surface ? 'Waiting for the stream' : 'No live KiCad surface', m.reason ?? '', !!m.canStart); }
      relayout(); updateWantOpen();
      break;
    }
    case 'surface': applySurface(m.surface ?? null); updateWantOpen(); if (!m.surface) { showCard('Surface closed', 'The KiCad window for this tab was closed.', !!state.cfg?.canStart); } break;
    case 'visible': state.hostVisible = !!m.visible; updateWantOpen(); if (m.active && m.visible && state.cfg?.stream) { stage.focus({ preventScroll: true }); } break;
    case 'clipboard.text': { const w = state.clipboardWaiters.get(m.id); if (w) { state.clipboardWaiters.delete(m.id); w(m.text ?? ''); } break; }
    case 'forwardedKey': break;   // the DOM already saw the chord; the command only swallowed VS Code's action
    case 'test.dispatch': testDispatch(m.event); break;
    case 'test.query': vscode.postMessage({ type: 'test.result', id: m.id, result: testSnapshot() }); break;
    default: break;
  }
});
function applySurface(surface) {
  const changed = (surface?.id ?? null) !== (state.surface?.id ?? null);
  state.surface = surface;
  $('hud-title').textContent = surface ? `${surface.kind}: ${surface.title}` : 'no surface';
  if (changed) { disconnect(); state.frameW = 0; state.frameH = 0; }
}

// ---------------------------------------------------------------------------------- test hooks
function testDispatch(spec) {
  const target = spec.target === 'window' ? window : spec.target ? (document.getElementById(spec.target) ?? stage) : stage;
  const init = { bubbles: true, cancelable: true, ...spec.init };
  let ev;
  if (spec.kind === 'pointer') { ev = new PointerEvent(spec.name, { pointerId: 1, pointerType: 'mouse', isPrimary: true, ...init }); }
  else if (spec.kind === 'mouse') { ev = new MouseEvent(spec.name, init); }
  else if (spec.kind === 'wheel') { ev = new WheelEvent(spec.name ?? 'wheel', init); }
  else if (spec.kind === 'key') { ev = new KeyboardEvent(spec.name, init); }
  else if (spec.kind === 'focus') { if (spec.name === 'focus') { stage.focus(); } else { stage.blur(); } return; }
  else { ev = new Event(spec.name, init); }
  target.dispatchEvent(ev);
}
function testSnapshot() {
  const r = canvas.getBoundingClientRect();
  return {
    connected: state.connected, mode: state.mode, surface: state.surface?.id ?? null, frameW: state.frameW, frameH: state.frameH,
    canvasRect: { left: r.left, top: r.top, width: r.width, height: r.height }, layout: state.layout, dpr: devicePixelRatio,
    inputCount: state.inputCount, lastInput: state.lastInput, stats: state.stats.sample(), focused: state.focused,
    cardVisible: !card.classList.contains('hidden'), raiseCount: state.raiseCount ?? 0, lastRaise: state.lastRaise ?? null,
    hudMode: $('hud-mode').textContent, raiseVisible: !$('btn-raise').classList.contains('hidden'),
  };
}

setMode('view'); setStatus('idle'); relayout();
vscode.postMessage({ type: 'ready' });
