// Two (three) winstream clients at once, one per surface: pcb / sch (/ 3d) of ONE kicad.exe.
// Measures: per-surface frames, input routing (wheel/keys through the pcb page change only the
// pcb frame), latency probes per surface, pan fps with both streams live, dialog/chooser
// attachment to the right surface, CPU.   usage: node bench_multi.mjs <outdir>
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

const HERE = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi';
const out = process.argv[2] || path.join(HERE, 'results', 'wgc');
fs.mkdirSync(out, { recursive: true });
const BASE = 'http://127.0.0.1:8765';
const R = { log: [] };
const log = (m, extra = {}) => { console.log(m, JSON.stringify(extra)); R.log.push({ m, ...extra, t: Date.now() }); };
const stats = async () => (await fetch(BASE + '/stats')).json();
const cpuPy = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/winstream/.venv/Scripts/python.exe';
function cpu(tag, secs) { return new Promise((res) => { const p = spawn(cpuPy, [path.join(HERE, 'cpu_sample.py'), String(secs), tag]); let o = ''; p.stdout.on('data', d => o += d); p.on('close', () => res(JSON.parse(o.trim()))); }); }

const browser = await chromium.launch({ headless: true });
async function client(surface, w, h) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', msg => { if (msg.type() === 'error') console.log(`[${surface}]`, msg.text()); });
  await page.goto(`${BASE}/?surface=${surface}&fit=0`);
  await page.waitForFunction(() => window.__state.connected && window.__state.frames > 0, null, { timeout: 15000 });
  await page.waitForTimeout(800);
  const st = await page.evaluate(() => window.__stats());
  log(`client ${surface} connected`, { w: st.w, h: st.h, windows: st.windows, surfaces: st.surfaces, main: st.main });
  return page;
}
// viewport = frame size so fit=0 gives 1:1 pixels and page coords == frame coords (+ bar 26 px)
const pcb = await client('pcb', 1920, 1106);
const sch = await client('sch', 1800, 1226);
const v3d = await client('3d', 1400, 926);
await pcb.mouse.click(1000, 626); await pcb.waitForTimeout(200); await pcb.keyboard.press('Home'); await sch.mouse.click(1000, 726); await sch.waitForTimeout(200); await sch.keyboard.press('Home'); await pcb.waitForTimeout(800);
const shot = async (page, name) => { const f = path.join(out, name + '.png'); await page.screenshot({ path: f }); return f; };
await shot(pcb, '01-pcb-tab'); await shot(sch, '02-sch-tab'); await shot(v3d, '03-3d-tab');
const frames = async (page) => page.evaluate(() => window.__state.frames);
const framesPerHwnd = async (page) => page.evaluate(() => { const o = {}; for (const [h, c] of window.__state.canvases) o[h] = c.width + 'x' + c.height; return o; });

// ---- idle: no frames for 3 s on any client
{
  const a = [await frames(pcb), await frames(sch), await frames(v3d)];
  await pcb.waitForTimeout(3000);
  const b = [await frames(pcb), await frames(sch), await frames(v3d)];
  log('idle 3 s frames (pcb, sch, 3d)', { delta: b.map((v, i) => v - a[i]) });
}
// ---- input routing: wheel in the pcb page -> only the pcb client gets frames; then the sch page
async function routing(name, page, act) {
  const a = [await frames(pcb), await frames(sch), await frames(v3d)];
  const s0 = await stats();
  await act();
  await page.waitForTimeout(900);
  const b = [await frames(pcb), await frames(sch), await frames(v3d)];
  const s1 = await stats();
  const perSession = {};
  for (const [h, v] of Object.entries(s1.sessions)) perSession[v.surface || h] = v.frames - (s0.sessions[h]?.frames ?? 0);
  log(name, { clientFrames: b.map((v, i) => v - a[i]), serverFramesPerSurface: perSession, foreground: s1.surfaces.filter(s => s.foreground).map(s => s.kind) });
}
const PC = { x: 1000, y: 600 + 26 }, SC = { x: 1000, y: 700 + 26 };
await routing('pcb page: click + wheel zoom', pcb, async () => { await pcb.mouse.click(PC.x, PC.y); await pcb.waitForTimeout(200); await pcb.mouse.wheel(0, -200); });
await routing('sch page: click + wheel zoom', sch, async () => { await sch.mouse.click(SC.x, SC.y); await sch.waitForTimeout(200); await sch.mouse.wheel(0, -200); });
await routing('sch page: key Home', sch, async () => { await sch.keyboard.press('Home'); });
await routing('pcb page: key Home', pcb, async () => { await pcb.mouse.move(PC.x, PC.y); await pcb.keyboard.press('Home'); });
await shot(pcb, '04-pcb-after-home'); await shot(sch, '05-sch-after-home');

// ---- latency probes per surface (wheel -> painted), sch and pcb alternating, 8 each
async function probes(page, name, x, y, n = 8) {
  const v = [];
  for (let i = 0; i < n; i++) {
    await page.mouse.move(x, y);
    try { const r = await page.evaluate(([x, y, up]) => window.__probe({ t: 'wheel', x, y, dy: up ? 120 : -120, dx: 0 }), [x, y - 26, i % 2 === 0]); v.push(r); } catch (e) { v.push({ e2e_ms: -1 }); }
    await page.waitForTimeout(350);
  }
  const ok = v.map(r => r.e2e_ms).filter(x => x >= 0).sort((a, b) => a - b);
  const med = (k) => { const a = v.map(r => r[k]).filter(x => x !== undefined && x >= 0).sort((a, b) => a - b); return a[a.length >> 1]; };
  const r = { e2e: ok, median: ok[ok.length >> 1], min: ok[0], max: ok[ok.length - 1], misses: v.length - ok.length, split: { ws_to_inject: med('ws_to_inject_ms'), inject_to_capture: med('inject_to_capture_ms'), capture_to_paint: med('capture_to_paint_ms') } };
  log(name, r); return r;
}
await pcb.mouse.click(PC.x, PC.y); await pcb.waitForTimeout(300);
R.probe_pcb = await probes(pcb, 'pcb wheel probe (sch + 3d clients live)', PC.x, PC.y);
await sch.mouse.click(SC.x, SC.y); await sch.waitForTimeout(300);
R.probe_sch = await probes(sch, 'sch wheel probe (pcb + 3d clients live)', SC.x, SC.y);
// interleaved: pcb, sch, pcb, sch ... (the user flipping between tabs)
{
  const v = [];
  for (let i = 0; i < 6; i++) {
    for (const [pg, c] of [[pcb, PC], [sch, SC]]) {
      await pg.mouse.click(c.x, c.y); await pg.waitForTimeout(150); await pg.mouse.move(c.x, c.y);
      try { const r = await pg.evaluate(([x, y, up]) => window.__probe({ t: 'wheel', x, y, dy: up ? 120 : -120, dx: 0 }), [c.x, c.y - 26, i % 2 === 0]); v.push({ s: pg === pcb ? 'pcb' : 'sch', ms: r.e2e_ms }); } catch { v.push({ s: pg === pcb ? 'pcb' : 'sch', ms: -1 }); }
      await pg.waitForTimeout(250);
    }
  }
  log('interleaved pcb/sch wheel probes (tab switching)', { v });
  R.probe_interleaved = v;
}

// ---- pan fps with all three clients live + CPU
async function pan(page, name, c, secs = 6) {
  await page.mouse.click(c.x, c.y); await page.waitForTimeout(300);
  const a = [await frames(pcb), await frames(sch), await frames(v3d)]; const s0 = await stats(); const t0 = Date.now();
  await page.mouse.move(c.x, c.y); await page.mouse.down({ button: 'middle' });
  const tEnd = Date.now() + secs * 1000; let i = 0;
  while (Date.now() < tEnd) { i++; await page.mouse.move(c.x + Math.sin(i / 8) * 150, c.y + Math.cos(i / 8) * 100, { steps: 2 }); await page.waitForTimeout(25); }
  await page.mouse.up({ button: 'middle' });
  const dt = (Date.now() - t0) / 1000; const b = [await frames(pcb), await frames(sch), await frames(v3d)]; const s1 = await stats();
  const st = await page.evaluate(() => window.__stats());
  const perSession = {}; for (const [h, v] of Object.entries(s1.sessions)) perSession[v.surface || h] = +((v.frames - (s0.sessions[h]?.frames ?? 0)) / dt).toFixed(1);
  const r = { seconds: +dt.toFixed(2), moves: i, clientFps: b.map((v, i) => +((v - a[i]) / dt).toFixed(1)), serverFpsPerSurface: perSession, decode_ms: +st.decode_ms_avg.toFixed(1), net_ms: +st.net_ms_avg.toFixed(1), mbit_s: +((s1.sent_bytes - s0.sent_bytes) * 8 / dt / 1e6).toFixed(1), server_cpu: s1.cpu, encode_ms: Object.values(s1.sessions).map(v => v.encode_ms_avg) };
  log(name, r); return r;
}
let c = cpu('idle', 5); await pcb.waitForTimeout(5200); R.cpu_idle = await c; log('cpu idle (3 clients)', R.cpu_idle);
c = cpu('pan_pcb', 6); R.pan_pcb = await pan(pcb, 'pan pcbnew 6 s (sch + 3d live)', PC); R.cpu_pan_pcb = await c; log('cpu pan pcb', R.cpu_pan_pcb);
c = cpu('pan_sch', 6); R.pan_sch = await pan(sch, 'pan eeschema 6 s (pcb + 3d live)', SC); R.cpu_pan_sch = await c; log('cpu pan sch', R.cpu_pan_sch);
// both editors busy at once: pan pcbnew with the mouse while eeschema is redrawn by keyboard zoom (F1/F2 = zoom in/out) from the other client
c = cpu('both_busy', 6);
{
  await sch.mouse.click(SC.x, SC.y); await sch.waitForTimeout(200);
  await pcb.mouse.click(PC.x, PC.y); await pcb.waitForTimeout(200);
  const a = [await frames(pcb), await frames(sch)]; const s0 = await stats(); const t0 = Date.now();
  await pcb.mouse.move(PC.x, PC.y); await pcb.mouse.down({ button: 'middle' });
  const tEnd = Date.now() + 6000; let i = 0;
  while (Date.now() < tEnd) { i++; await pcb.mouse.move(PC.x + Math.sin(i / 8) * 150, PC.y + Math.cos(i / 8) * 100, { steps: 2 }); if (i % 4 === 0) { await sch.mouse.move(SC.x, SC.y); await sch.mouse.wheel(0, i % 8 ? 120 : -120); } await pcb.waitForTimeout(25); }
  await pcb.mouse.up({ button: 'middle' });
  const dt = (Date.now() - t0) / 1000; const b = [await frames(pcb), await frames(sch)]; const s1 = await stats();
  const perSession = {}; for (const [h, v] of Object.entries(s1.sessions)) perSession[v.surface || h] = +((v.frames - (s0.sessions[h]?.frames ?? 0)) / dt).toFixed(1);
  R.both_busy = { seconds: +dt.toFixed(2), clientFps: b.map((v, i) => +((v - a[i]) / dt).toFixed(1)), serverFpsPerSurface: perSession, mbit_s: +((s1.sent_bytes - s0.sent_bytes) * 8 / dt / 1e6).toFixed(1), foreground_log: s1.foreground_log.slice(-12), obscured_fixes: s1.obscured_fixes, foreground_fixes: s1.foreground_fixes };
  log('both busy: pan pcbnew + wheel-zoom eeschema interleaved', R.both_busy);
}
R.cpu_both = await c; log('cpu both busy', R.cpu_both);
await pcb.keyboard.press('Home'); await sch.keyboard.press('Home');

// ---- dialog + chooser attach to the pcb surface (hook latency)
{
  await pcb.mouse.click(PC.x, PC.y); await pcb.waitForTimeout(300); await pcb.keyboard.press('Home'); await pcb.waitForTimeout(500);
  await pcb.mouse.click(778, 820 + 26); await pcb.waitForTimeout(400);
  const t0 = Date.now(); await pcb.keyboard.press('e');
  await pcb.waitForFunction(() => window.__state.windows.length > 1, null, { timeout: 8000 }).catch(() => {});
  const tList = Date.now() - t0;
  await pcb.waitForFunction(() => { const w = window.__state.windows.find(w => w.cls === '#32770'); return w && window.__state.canvases.get(w.hwnd)?.width > 0; }, null, { timeout: 8000 }).catch(() => {});
  const tFrame = Date.now() - t0;
  await pcb.waitForTimeout(400);
  const wl = await pcb.evaluate(() => window.__state.windows.map(w => ({ hwnd: w.hwnd, cls: w.cls, title: w.title, rel: w.rel, surface: w.surface })));
  const wlSch = await sch.evaluate(() => window.__state.windows.map(w => w.title));
  log('properties dialog (E) in pcb page', { ms_to_window_list: tList, ms_to_first_frame: tFrame, pcbWindows: wl, schWindows: wlSch, hook_log: (await stats()).hook_log.slice(-8) });
  await shot(pcb, '06-pcb-dialog'); await shot(sch, '07-sch-during-pcb-dialog');
  await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(600); await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(300);
  // chooser
  await pcb.mouse.move(PC.x, PC.y);
  const t1 = Date.now(); await pcb.keyboard.press('a');
  await pcb.waitForFunction(() => window.__state.windows.some(w => w.title.includes('Chooser')), null, { timeout: 15000 }).catch(() => {});
  const tC = Date.now() - t1;
  await pcb.waitForTimeout(1500);
  const st = await pcb.evaluate(() => ({ uw: window.__state.uw, uh: window.__state.uh, ox: window.__state.ox, oy: window.__state.oy, windows: window.__state.windows.map(w => ({ title: w.title, rel: w.rel, surface: w.surface })) }));
  log('footprint chooser (A) in pcb page', { ms_to_window_list: tC, stage: st });
  await shot(pcb, '08-pcb-chooser');
  await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(800);
  // leave the placement tool
  await pcb.mouse.click(1586, 208 + 26); await pcb.waitForTimeout(300);
}
// ---- 3D viewer: rotate by left-drag and count frames
{
  const a = await frames(v3d); const t0 = Date.now();
  await v3d.mouse.move(600, 500); await v3d.mouse.down(); for (let i = 0; i < 40; i++) { await v3d.mouse.move(600 + i * 6, 500 + i * 2); await v3d.waitForTimeout(30); } await v3d.mouse.up();
  const dt = (Date.now() - t0) / 1000; const b = await frames(v3d);
  log('3D viewer drag-rotate', { seconds: +dt.toFixed(2), fps: +((b - a) / dt).toFixed(1) });
  await shot(v3d, '09-3d-after-rotate');
}
const fin = await stats();
R.final_stats = { surfaces: fin.surfaces, foreground_log: fin.foreground_log, hook_events: fin.hook_events, rescans: fin.rescans, uptime_s: fin.uptime_s, failed: fin.failed_windows, obscured_fixes: fin.obscured_fixes, foreground_fixes: fin.foreground_fixes };
log('final', R.final_stats);
fs.writeFileSync(path.join(out, 'bench_multi.json'), JSON.stringify(R, null, 1));
await browser.close();
