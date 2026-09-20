// Unit tests of the webview client's pure module (media/live/protocol.js): frame header parsing, DPI-exact
// coordinate mapping, input event serialisation, HUD statistics. Run: node --test extension/test/live/unit
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as P from '../../../media/live/protocol.js';

const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0]);
const WEBP = new Uint8Array([0x52, 0x49, 0x46, 0x46, 0x24, 0, 0, 0, 0x57, 0x45, 0x42, 0x50, 0x56, 0x50, 0x38, 0x20]);

test('frame header: little-endian [u32 id][u16 w][u16 h] + JPEG magic', () => {
  const f = P.encodeFrame(0x01020304, 1920, 1080, JPEG);
  assert.equal(f.byteLength, 8 + JPEG.byteLength);
  assert.deepEqual([...f.subarray(0, 8)], [0x04, 0x03, 0x02, 0x01, 0x80, 0x07, 0x38, 0x04]);
  const h = P.parseFrameHeader(f);
  assert.deepEqual(h, { frameId: 0x01020304, w: 1920, h: 1080, codec: 'jpeg', mime: 'image/jpeg', payloadOffset: 8 });
  // also from an ArrayBuffer (what a WebSocket delivers) and from a view with a byteOffset
  assert.equal(P.parseFrameHeader(f.buffer).frameId, 0x01020304);
  const padded = new Uint8Array(f.byteLength + 3); padded.set(f, 3);
  assert.equal(P.parseFrameHeader(padded.subarray(3)).w, 1920);
});

test('frame header: WebP is recognised, unknown payloads flagged, u32 ids wrap, short buffers throw', () => {
  assert.equal(P.parseFrameHeader(P.encodeFrame(7, 8, 8, WEBP)).codec, 'webp');
  assert.equal(P.parseFrameHeader(P.encodeFrame(7, 8, 8, WEBP)).mime, 'image/webp');
  assert.equal(P.parseFrameHeader(P.encodeFrame(7, 8, 8, new Uint8Array(12))).codec, 'unknown');
  assert.equal(P.parseFrameHeader(P.encodeFrame(0xffffffff, 65535, 65535, JPEG)).frameId, 0xffffffff);
  assert.equal(P.parseFrameHeader(P.encodeFrame(0xffffffff, 65535, 65535, JPEG)).w, 65535);
  assert.throws(() => P.parseFrameHeader(new Uint8Array(7)), /too short/);
});

test('meta text frames are parsed, other text ignored', () => {
  assert.deepEqual(P.parseMeta('{"type":"meta","w":1280,"h":800,"dpi_scale":1.5,"dirty":[{"x":0,"y":0,"w":10,"h":10}]}'),
    { type: 'meta', w: 1280, h: 800, dpi_scale: 1.5, dirty: [{ x: 0, y: 0, w: 10, h: 10 }], inject: undefined, clipboard: undefined, frameId: undefined });
  // kiwrap-live sends dirty rects as [x, y, w, h] arrays plus frame_id / surface / rect_px
  const real = P.parseMeta('{"type":"meta","w":1920,"h":1080,"dpi_scale":1.5,"dirty":[[0,0,1920,1080]],"frame_id":7,"surface":"pcb","rect_px":[60,60,1980,1140]}');
  assert.deepEqual(real.dirty, [{ x: 0, y: 0, w: 1920, h: 1080 }]); assert.equal(real.frameId, 7);
  assert.equal(P.parseMeta('{"type":"probe","probe":3}'), null);
  assert.equal(P.parseMeta('{"type":"pong","id":1}'), null);
  assert.equal(P.parseMeta('{"type":"other"}'), null);
  assert.equal(P.parseMeta('not json'), null);
  assert.equal(P.parseMeta('{"type":"meta","w":"12","h":8}').w, 12);
  assert.equal(P.parseMeta('{"type":"meta","w":1,"h":1,"dpi_scale":0}').dpi_scale, 1);
  assert.equal(P.parseMeta('{"type":"meta","w":1,"h":1,"clipboard":"abc","inject":"events"}').clipboard, 'abc');
});

test('fitLayout: native mode shows 1 surface px per device px when it fits', () => {
  // 150 % display: a 960x600 frame is 640x400 CSS px
  const l = P.fitLayout({ frameW: 960, frameH: 600, viewW: 1000, viewH: 800, dpr: 1.5, mode: 'native' });
  assert.equal(l.cssW, 640); assert.equal(l.cssH, 400);
  assert.equal(l.scale, 1.5);
  assert.equal(l.left, 180); assert.equal(l.top, 200);
  assert.equal(l.scrolls, false);
});

test('fitLayout: native mode never shrinks a big frame; the viewport scrolls instead (finding 21)', () => {
  // the 150 % bench: a 3840x2088 frame in a 792x430 CSS px tab
  const l = P.fitLayout({ frameW: 3840, frameH: 2088, viewW: 792, viewH: 430, dpr: 1.5, mode: 'native' });
  assert.equal(l.cssW, 2560); assert.equal(l.cssH, 1392);
  assert.equal(l.scale, 1.5); assert.equal(l.left, 0); assert.equal(l.top, 0);
  assert.equal(l.scrolls, true);
  const fit = P.fitLayout({ frameW: 3840, frameH: 2088, viewW: 792, viewH: 430, dpr: 1.5, mode: 'fit' });
  assert.ok(fit.scale > 4.8 && fit.scale < 4.9 && fit.scrolls === false);
});

test('fitLayout: fit mode scales down to the viewport, never up beyond native', () => {
  const small = P.fitLayout({ frameW: 1920, frameH: 1080, viewW: 640, viewH: 480, dpr: 1, mode: 'fit' });
  assert.equal(small.cssW, 640); assert.ok(Math.abs(small.cssH - 360) < 1e-9); assert.equal(small.scale, 3);
  const big = P.fitLayout({ frameW: 320, frameH: 200, viewW: 1600, viewH: 1200, dpr: 2, mode: 'fit' });
  assert.equal(big.cssW, 160); assert.equal(big.scale, 2);   // stays crisp: 1 surface px = 1 device px
  assert.deepEqual(P.fitLayout({ frameW: 0, frameH: 0, viewW: 10, viewH: 10 }), { cssW: 0, cssH: 0, left: 0, top: 0, scale: 1, scrolls: false });
});

test('clientToSurface: DPI-exact mapping through the canvas bounding rect', () => {
  const rect = { left: 100.5, top: 40, width: 640, height: 400 };   // 960x600 frame at 150 %
  assert.deepEqual(P.clientToSurface(100.5, 40, rect, 960, 600), { x: 0, y: 0 });
  const p = P.clientToSurface(100.5 + 320, 40 + 200, rect, 960, 600);
  assert.ok(Math.abs(p.x - 480) < 1e-9 && Math.abs(p.y - 300) < 1e-9);
  const q = P.clientToSurface(100.5 + 1, 40 + 1, rect, 960, 600);
  assert.ok(Math.abs(q.x - 1.5) < 1e-9 && Math.abs(q.y - 1.5) < 1e-9);
  // clamped to the frame
  assert.deepEqual(P.clientToSurface(-50, -50, rect, 960, 600), { x: 0, y: 0 });
  assert.deepEqual(P.clientToSurface(5000, 5000, rect, 960, 600), { x: 959, y: 599 });
  // VS Code zoom changes the rect, not the frame: the mapping follows the rect
  const zoomed = { left: 0, top: 0, width: 768, height: 480 };
  const z = P.clientToSurface(384, 240, zoomed, 960, 600);
  assert.ok(Math.abs(z.x - 480) < 1e-9 && Math.abs(z.y - 300) < 1e-9);
  // inverse
  const back = P.surfaceToClient(480, 300, rect, 960, 600);
  assert.ok(Math.abs(back.x - 420.5) < 1e-9 && Math.abs(back.y - 240) < 1e-9);
});

const M = (o = {}) => ({ ctrl: false, shift: false, alt: false, meta: false, ...o });

test('input serialisation: mouse move / button events carry integer surface px, DOM buttons and object modifiers', () => {
  assert.deepEqual(P.mouseMove({ x: 10.4, y: 20.6 }, 1, { shift: true }), { type: 'mouse', x: 10, y: 21, buttons: 1, modifiers: M({ shift: true }) });
  assert.deepEqual(P.mouseButton({ x: 1.5, y: 2.5 }, 2, 2, true, { ctrl: true, alt: true }),
    { type: 'mouse', x: 2, y: 3, button: 2, buttons: 2, down: true, modifiers: M({ ctrl: true, alt: true }) });
  assert.deepEqual(P.mouseButton({ x: 0, y: 0 }, 1, 0, false, undefined), { type: 'mouse', x: 0, y: 0, button: 1, buttons: 0, down: false, modifiers: M() });
  assert.deepEqual(P.NO_MODS, M());
});

test('input serialisation: wheel flips dy to the Windows sign and converts line/page deltas', () => {
  assert.deepEqual(P.wheel({ x: 5, y: 6 }, 0, 100, 0, P.NO_MODS), { type: 'wheel', x: 5, y: 6, dx: 0, dy: -100, modifiers: M() });   // roll towards user
  assert.deepEqual(P.wheel({ x: 5, y: 6 }, 0, -3, 1, { ctrl: true }), { type: 'wheel', x: 5, y: 6, dx: 0, dy: 120, modifiers: M({ ctrl: true }) }); // 3 lines away
  assert.equal(P.wheel({ x: 0, y: 0 }, 2, 0, 1, P.NO_MODS).dx, 80);
  assert.equal(P.wheel({ x: 0, y: 0 }, 0, 1, 2, P.NO_MODS).dy, -800);
});

test('input serialisation: keys carry key/code/modifiers, repeat only when set', () => {
  const e = { key: 'r', code: 'KeyR', ctrlKey: false, shiftKey: true, altKey: false, metaKey: false, repeat: false };
  assert.deepEqual(P.key(e, true), { type: 'key', key: 'r', code: 'KeyR', modifiers: M({ shift: true }), down: true });
  assert.deepEqual(P.key({ ...e, ctrlKey: true, metaKey: true, repeat: true }, false), { type: 'key', key: 'r', code: 'KeyR', modifiers: M({ shift: true, ctrl: true, meta: true }), down: false, repeat: true });
  assert.deepEqual(P.modifiersOf({ altKey: true }), M({ alt: true }));
  assert.deepEqual(P.focus(true), { type: 'focus', down: true });
  assert.deepEqual(P.keyframe(), { type: 'keyframe' });
  assert.deepEqual(P.resize(1919.6, 1080.2, 1.5), { type: 'resize', w: 1920, h: 1080, dpi_scale: 1.5 });
  assert.deepEqual(P.clipboard('abc'), { type: 'clipboard', text: 'abc' });
  assert.equal(P.shouldSwallowKey({ key: 'Meta' }), true);
  assert.equal(P.shouldSwallowKey({ key: 'Escape' }), false);
});

test('FrameStats: fps over the last second, input-to-frame latency median', () => {
  let now = 1000;
  const s = new P.FrameStats(() => now);
  s.onInputSent(); now += 40; s.onFrame(1000, 5, 1);
  s.onInputSent(); now += 60; s.onFrame(1000, 7, 2);
  now += 100; s.onFrame(2000, 3, 3);
  const r = s.sample();
  assert.equal(r.fps, 3); assert.equal(r.frames, 3); assert.equal(r.lastFrameId, 3);
  assert.equal(r.inputToFrameMs, 50);
  assert.equal(r.kbps, Math.round(4000 * 8 / 1000));
  assert.equal(r.decodeMs, 5);
  now += 2000;
  assert.equal(s.sample().fps, 0);
});

test('streamUrl and backoff', () => {
  assert.equal(P.streamUrl('ws://127.0.0.1:4321', 'pcb-1', 't ok'), 'ws://127.0.0.1:4321/stream?surface=pcb-1&token=t+ok');
  assert.equal(P.streamUrl('ws://127.0.0.1:4321/', 'sch', ''), 'ws://127.0.0.1:4321/stream?surface=sch');
  assert.deepEqual([0, 1, 2, 5, 9].map(P.backoffMs), [250, 500, 1000, 5000, 5000]);
});
