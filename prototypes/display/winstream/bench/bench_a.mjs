// Stage A: connect, first frame, coordinate mapping, wheel-zoom latency probes, idle/active fps.
// node bench_a.mjs <outdir> [port]
import { chromium } from 'playwright';
import fs from 'node:fs';

const out = process.argv[2]; const port = process.argv[3] || 8765;
const base = `http://127.0.0.1:${port}`;
fs.mkdirSync(out, { recursive: true });
const stats = async () => (await fetch(`${base}/stats`)).json();
const cursor = async () => (await fetch(`${base}/cursor`)).json();
const sleep = ms => new Promise(r => setTimeout(r, ms));
const res = {};

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
await page.goto(`${base}/`);
await page.waitForFunction(() => window.__state.frames > 0 && window.__state.w > 0, null, { timeout: 30000 });
await sleep(1000);
res.hello = await page.evaluate(() => window.__state.hello);
res.stage = await page.evaluate(() => { const r = document.getElementById('stage').getBoundingClientRect(); return { left: r.left, top: r.top, w: r.width, h: r.height, scale: window.__state.scale }; });
await page.screenshot({ path: `${out}/a1_first_frame.png` });

// ---- coordinate mapping (HiDPI): move the page pointer, compare the real cursor position
const map = [];
for (const [fx, fy] of [[200, 200], [960, 540], [1900, 1060], [17, 900]]) {
  const px = res.stage.left + fx * res.stage.scale, py = res.stage.top + fy * res.stage.scale;
  await page.mouse.move(px, py); await sleep(120);
  const c = await cursor();
  map.push({ frame_px: [fx, fy], page_px: [Math.round(px), Math.round(py)], cursor_frame: c.frame, err: [c.frame[0] - fx, c.frame[1] - fy] });
}
res.mapping = map;

// ---- idle fps / bandwidth for 3 s
const s0 = await page.evaluate(() => window.__stats());
await sleep(3000);
const s1 = await page.evaluate(() => window.__stats());
res.idle = { fps: (s1.frames - s0.frames) / 3, kbps: (s1.bytes - s0.bytes) * 8 / 3000 };

// ---- active: sweep the mouse over the canvas (KiCad redraws its crosshair) for 5 s
const cx = res.stage.left + 1000 * res.stage.scale, cy = res.stage.top + 600 * res.stage.scale;
await page.mouse.move(cx, cy);
const a0 = await page.evaluate(() => window.__stats()); const st0 = await stats();
const t0 = Date.now();
while (Date.now() - t0 < 5000) { const t = (Date.now() - t0) / 1000; await page.mouse.move(cx + Math.sin(t * 3) * 300, cy + Math.cos(t * 2) * 200); await sleep(8); }
const a1 = await page.evaluate(() => window.__stats()); const st1 = await stats();
res.active_move = { fps: (a1.frames - a0.frames) / 5, kbps: (a1.bytes - a0.bytes) * 8 / 5000, decode_ms: a1.decode_ms_avg, net_ms: a1.net_ms_avg,
  server_cpu_pct: st1.cpu.server_pct, kicad_cpu_pct: st1.cpu.kicad_pct, encode_ms: st1.sessions[st1.main_hwnd].encode_ms_avg, gpu: st1.gpu };
await page.screenshot({ path: `${out}/a2_after_move.png` });

// ---- wheel zoom latency probes (input -> paint)
const probes = [];
for (let i = 0; i < 12; i++) {
  const dir = i % 2 ? -120 : 120;
  try { probes.push(await page.evaluate(([x, y, dy]) => window.__probe({ t: 'wheel', x, y, dy, dx: 0 }), [1000, 600, dir])); } catch (e) { probes.push({ error: String(e) }); }
  await sleep(250);
}
res.wheel_probes = probes;
const ok = probes.filter(p => p.e2e_ms !== undefined);
const med = a => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
res.wheel_latency = ok.length ? { n: ok.length, e2e_median_ms: med(ok.map(p => p.e2e_ms)), e2e_min_ms: Math.min(...ok.map(p => p.e2e_ms)), e2e_max_ms: Math.max(...ok.map(p => p.e2e_ms)),
  inject_to_capture_median_ms: med(ok.map(p => p.inject_to_capture_ms)), capture_to_paint_median_ms: med(ok.map(p => p.capture_to_paint_ms)), ws_to_inject_median_ms: med(ok.map(p => p.ws_to_inject_ms)) } : null;
res.ping = await page.evaluate(() => window.__ping());
await page.screenshot({ path: `${out}/a3_after_zoom.png` });

// ---- continuous wheel zoom for 4 s (worst-case full-canvas redraws)
const z0 = await page.evaluate(() => window.__stats()); const zs0 = await stats();
const tz = Date.now(); let n = 0;
while (Date.now() - tz < 4000) { await page.evaluate(([x, y, dy]) => window.__send({ t: 'wheel', x, y, dy, dx: 0 }), [1000, 600, n++ % 2 ? -120 : 120]); await sleep(60); }
await sleep(300);
const z1 = await page.evaluate(() => window.__stats()); const zs1 = await stats();
res.active_zoom = { fps: (z1.frames - z0.frames) / 4.3, kbps: (z1.bytes - z0.bytes) * 8 / 4300, decode_ms: z1.decode_ms_avg, server_cpu_pct: zs1.cpu.server_pct, kicad_cpu_pct: zs1.cpu.kicad_pct, encode_ms: zs1.sessions[zs1.main_hwnd].encode_ms_avg, gpu: zs1.gpu };
res.server_stats = await stats();
fs.writeFileSync(`${out}/bench_a.json`, JSON.stringify(res, null, 1));
console.log(JSON.stringify(res, null, 1));
await browser.close();
