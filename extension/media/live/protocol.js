// Pure helpers of the Live KiCad webview client: no DOM, no globals, importable from node for tests.
// The shapes mirror extension/src/shared/types.ts (helper protocol section).

export const STREAM_HEADER_BYTES = 8;
export const LINE_PX = 40;   // WheelEvent.deltaMode === DOM_DELTA_LINE -> px

/** Parse the 8-byte little-endian frame header `[u32 frame_id][u16 w][u16 h]` and sniff the codec. */
export function parseFrameHeader(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (bytes.byteLength < STREAM_HEADER_BYTES) {
    throw new Error(`frame too short: ${bytes.byteLength} bytes`);
  }
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const frameId = dv.getUint32(0, true);
  const w = dv.getUint16(4, true);
  const h = dv.getUint16(6, true);
  const codec = detectCodec(bytes, STREAM_HEADER_BYTES);
  return { frameId, w, h, codec, mime: codec === 'webp' ? 'image/webp' : codec === 'jpeg' ? 'image/jpeg' : 'application/octet-stream', payloadOffset: STREAM_HEADER_BYTES };
}

/** JPEG starts FF D8 FF; WebP is RIFF....WEBP. */
export function detectCodec(bytes, offset = 0) {
  if (bytes.byteLength >= offset + 3 && bytes[offset] === 0xff && bytes[offset + 1] === 0xd8 && bytes[offset + 2] === 0xff) { return 'jpeg'; }
  if (bytes.byteLength >= offset + 12 && bytes[offset] === 0x52 && bytes[offset + 1] === 0x49 && bytes[offset + 2] === 0x46 && bytes[offset + 3] === 0x46 &&
    bytes[offset + 8] === 0x57 && bytes[offset + 9] === 0x45 && bytes[offset + 10] === 0x42 && bytes[offset + 11] === 0x50) { return 'webp'; }
  return 'unknown';
}

/** Build a frame (used by the fake helper and the tests). */
export function encodeFrame(frameId, w, h, payload) {
  const out = new Uint8Array(STREAM_HEADER_BYTES + payload.byteLength);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, frameId >>> 0, true);
  dv.setUint16(4, w & 0xffff, true);
  dv.setUint16(6, h & 0xffff, true);
  out.set(payload instanceof Uint8Array ? payload : new Uint8Array(payload), STREAM_HEADER_BYTES);
  return out;
}

/** Parse a text frame; returns null unless it is a `meta` message. */
export function parseMeta(text) {
  let m;
  try { m = JSON.parse(text); } catch { return null; }
  if (!m || typeof m !== 'object' || m.type !== 'meta') { return null; }
  return { type: 'meta', w: m.w | 0, h: m.h | 0, dpi_scale: Number(m.dpi_scale) || 1, dirty: Array.isArray(m.dirty) ? m.dirty.map(rectOf).filter(Boolean) : [], inject: m.inject, clipboard: typeof m.clipboard === 'string' ? m.clipboard : undefined, frameId: m.frame_id };
}

/** A rect as `{x,y,w,h}` from either that object or the helper's `[x, y, w, h]` array. */
export function rectOf(r) {
  if (Array.isArray(r) && r.length === 4) { return { x: r[0] | 0, y: r[1] | 0, w: r[2] | 0, h: r[3] | 0 }; }
  if (r && typeof r === 'object') { return { x: r.x | 0, y: r.y | 0, w: r.w | 0, h: r.h | 0 }; }
  return null;
}

/**
 * Where the frame goes inside the viewport, in CSS px.
 * - `native`: 1 surface px = 1 device px always (cssW = frameW / dpr); a frame larger than the
 *   viewport is shown at that size and the viewport scrolls (`scrolls: true`), so a 3840x2088
 *   frame in a 792x430 tab stays legible instead of being shrunk to 0.31 (finding 21).
 * - `fit`: scaled to fill the viewport (never upscaled beyond native to keep it crisp).
 * `scale` is surface px per CSS px. A frame smaller than the viewport is centred in both modes.
 */
export function fitLayout({ frameW, frameH, viewW, viewH, dpr = 1, mode = 'fit' }) {
  if (!(frameW > 0) || !(frameH > 0) || !(viewW > 0) || !(viewH > 0)) {
    return { cssW: 0, cssH: 0, left: 0, top: 0, scale: 1, scrolls: false };
  }
  const nativeCssW = frameW / dpr, nativeCssH = frameH / dpr;
  const fitScale = Math.min(viewW / nativeCssW, viewH / nativeCssH);
  const s = mode === 'native' ? 1 : Math.min(1, fitScale);
  const cssW = nativeCssW * s, cssH = nativeCssH * s;
  return {
    cssW, cssH,
    left: Math.max(0, Math.floor((viewW - cssW) / 2)),
    top: Math.max(0, Math.floor((viewH - cssH) / 2)),
    scale: frameW / cssW,
    scrolls: mode === 'native' && (cssW > viewW || cssH > viewH),
  };
}

/**
 * DPI-exact mapping from a client (CSS px) point to surface px, using the canvas's bounding rect so
 * that VS Code zoom, OS scaling and our own fit scale are all folded into rect.width / frameW.
 * Result is clamped to the frame and left fractional; callers round when serialising.
 */
export function clientToSurface(clientX, clientY, rect, frameW, frameH) {
  if (!(rect.width > 0) || !(rect.height > 0)) { return { x: 0, y: 0 }; }
  const x = (clientX - rect.left) * (frameW / rect.width);
  const y = (clientY - rect.top) * (frameH / rect.height);
  return { x: clamp(x, 0, Math.max(0, frameW - 1)), y: clamp(y, 0, Math.max(0, frameH - 1)) };
}

/** Inverse mapping (surface px -> client CSS px), used by the pointer-lock virtual cursor. */
export function surfaceToClient(x, y, rect, frameW, frameH) {
  return { x: rect.left + x * (rect.width / frameW), y: rect.top + y * (rect.height / frameH) };
}

export function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

/** Modifier state as the unambiguous object form the helper accepts (`{ctrl, shift, alt, meta}`). */
export function modifiersOf(e) {
  return { ctrl: !!e.ctrlKey, shift: !!e.shiftKey, alt: !!e.altKey, meta: !!e.metaKey };
}
export const NO_MODS = Object.freeze({ ctrl: false, shift: false, alt: false, meta: false });
const mods = (m) => (m && typeof m === 'object' ? { ctrl: !!m.ctrl, shift: !!m.shift, alt: !!m.alt, meta: !!m.meta } : { ...NO_MODS });

// ---- input event serialisation (surface px in, protocol object out) ---------------------------------

export function mouseMove(p, buttons, modifiers) {
  return { type: 'mouse', x: Math.round(p.x), y: Math.round(p.y), buttons: buttons | 0, modifiers: mods(modifiers) };
}

export function mouseButton(p, button, buttons, down, modifiers) {
  return { type: 'mouse', x: Math.round(p.x), y: Math.round(p.y), button: button | 0, buttons: buttons | 0, down: !!down, modifiers: mods(modifiers) };
}

/** Browser: +deltaY = towards the user. Windows WHEEL_DELTA: + = away from the user. Flip dy. */
export function wheel(p, deltaX, deltaY, deltaMode, modifiers) {
  const k = deltaMode === 1 ? LINE_PX : deltaMode === 2 ? LINE_PX * 20 : 1;
  return { type: 'wheel', x: Math.round(p.x), y: Math.round(p.y), dx: Math.round(deltaX * k), dy: Math.round(-deltaY * k), modifiers: mods(modifiers) };
}

export function key(e, down) {
  const ev = { type: 'key', key: String(e.key ?? ''), code: String(e.code ?? ''), modifiers: modifiersOf(e), down: !!down };
  if (e.repeat) { ev.repeat = true; }
  return ev;
}

export function focus(focused) { return { type: 'focus', down: !!focused }; }

/** Ask the helper for a full frame on this stream (resync after a hidden tab). */
export function keyframe() { return { type: 'keyframe' }; }

export function resize(w, h, dpiScale) { return { type: 'resize', w: Math.round(w), h: Math.round(h), dpi_scale: Number(dpiScale) || 1 }; }

export function clipboard(text) { return { type: 'clipboard', text: String(text ?? '') }; }

/** Keys VS Code already handled or that must never reach KiCad from the shell. */
export function shouldSwallowKey(e) {
  // Never forward the OS/window level chords; the shield keeps the rest.
  if (e.key === 'Meta' || e.key === 'OS') { return true; }
  return false;
}

/** Whether a wheel event is a browser pinch-zoom (ctrl+wheel from a trackpad); still forwarded as ctrl+wheel. */
export function isPinch(e) { return !!e.ctrlKey && e.deltaMode === 0 && Math.abs(e.deltaY) < 1; }

// ---- rolling statistics for the HUD -----------------------------------------------------------------

export class FrameStats {
  constructor(now = () => Date.now()) {
    this.now = now;
    this.frames = 0; this.bytes = 0;
    this.paintTimes = []; this.byteLog = []; this.decodeMs = [];
    this.lastInputAt = null; this.inputToFrame = [];
    this.lastFrameId = 0;
  }
  onInputSent() { this.lastInputAt = this.now(); }
  onFrame(bytes, decodeMs, frameId) {
    const t = this.now();
    this.frames++; this.bytes += bytes; this.lastFrameId = frameId;
    this.paintTimes.push(t); this.byteLog.push([t, bytes]); this.decodeMs.push(decodeMs);
    if (this.lastInputAt != null) { this.inputToFrame.push(t - this.lastInputAt); this.lastInputAt = null; }
    const cut = t - 2000;
    while (this.paintTimes.length && this.paintTimes[0] < cut) { this.paintTimes.shift(); }
    while (this.byteLog.length && this.byteLog[0][0] < cut) { this.byteLog.shift(); }
    if (this.decodeMs.length > 120) { this.decodeMs.shift(); }
    if (this.inputToFrame.length > 60) { this.inputToFrame.shift(); }
  }
  sample() {
    const t = this.now();
    const fps = this.paintTimes.filter((x) => x > t - 1000).length;
    const kbps = Math.round(this.byteLog.filter(([x]) => x > t - 1000).reduce((a, [, b]) => a + b, 0) * 8 / 1000);
    const avg = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
    return {
      fps, kbps, frames: this.frames, decodeMs: +avg(this.decodeMs).toFixed(1),
      inputToFrameMs: this.inputToFrame.length ? +median(this.inputToFrame).toFixed(0) : null,
      lastFrameId: this.lastFrameId,
    };
  }
}

export function median(a) {
  const s = [...a].sort((x, y) => x - y);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** Reconnect delay with capped exponential backoff (ms). */
export function backoffMs(attempt) { return Math.min(5000, 250 * 2 ** Math.min(attempt, 5)); }

/** Build the /stream URL for a surface. */
export function streamUrl(base, surfaceId, token) {
  const u = new URL(base);
  u.pathname = '/stream';
  u.searchParams.set('surface', surfaceId);
  if (token) { u.searchParams.set('token', token); }
  return u.toString();
}
