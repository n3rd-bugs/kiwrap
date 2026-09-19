// Latency + fps of the -shareapp framebuffer per editor: hooks noVNC's Display draw calls so every
// updated rect is timestamped, then measures input -> first rect landing in the target frame.
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';

const HERE = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi';
const sess = JSON.parse(fs.readFileSync(path.join(HERE, 'session.json'), 'utf8'));
const R = sess.regions;
const out = { runs: {} };
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
await page.goto('http://127.0.0.1:6080/kicad.html?nostatus=1&fit=window');
await page.waitForFunction(() => window.__connected === true, null, { timeout: 15000 });
await page.waitForTimeout(1500);
await page.evaluate(() => {
  const d = window.rfb._display; window.__rects = [];
  const wrap = (name, ax, ay, aw, ah) => { const o = d[name].bind(d); d[name] = (...a) => { window.__rects.push([performance.now(), a[ax], a[ay], a[aw], a[ah]]); if (window.__rects.length > 20000) window.__rects.splice(0, 10000); return o(...a); }; };
  wrap('fillRect', 0, 1, 2, 3); wrap('copyImage', 4, 5, 2, 3); wrap('imageRect', 0, 1, 2, 3); wrap('blitImage', 0, 1, 2, 3);
  if (d.drawImage) { const o = d.drawImage.bind(d); d.drawImage = (img, x, y) => { window.__rects.push([performance.now(), x, y, img.width || 0, img.height || 0]); return o(img, x, y); }; }
});
const box = await page.locator('#screen canvas').boundingBox();
const s = box.width / 7680;
const P = (sx, sy) => ({ x: box.x + sx * s, y: box.y + sy * s });
const cPcb = P(R.pcb[0] + 1000, R.pcb[1] + 600), cSch = P(R.sch[0] + 1000, R.sch[1] + 700);
const inter = (r, reg) => r[1] < reg[2] && r[1] + r[3] > reg[0] && r[2] < reg[3] && r[2] + r[4] > reg[1];
async function firstRectAfter(t0, reg, timeout = 4000) {
  const tEnd = Date.now() + timeout;
  while (Date.now() < tEnd) {
    const hit = await page.evaluate(([t0, reg]) => { const inter = (r) => r[1] < reg[2] && r[1] + r[3] > reg[0] && r[2] < reg[3] && r[2] + r[4] > reg[1]; const r = window.__rects.find(r => r[0] > t0 && inter(r)); return r ? r[0] : null; }, [t0, reg]);
    if (hit) return +(hit - t0).toFixed(1);
    await new Promise(r => setTimeout(r, 5));
  }
  return -1;
}
const now = () => page.evaluate(() => performance.now());
async function latency(name, reg, act, n = 8) {
  const v = [];
  for (let i = 0; i < n; i++) { const t0 = await now(); await act(i); v.push(await firstRectAfter(t0, reg)); await page.waitForTimeout(700); }
  const ok = v.filter(x => x >= 0).sort((a, b) => a - b);
  const r = { values: v, median: ok[ok.length >> 1] ?? null, min: ok[0] ?? null, max: ok[ok.length - 1] ?? null, misses: v.length - ok.length };
  console.log(name, JSON.stringify(r)); out.runs[name] = r;
}
// pcbnew: wheel zoom in/out alternating at the canvas; eeschema: same
await page.mouse.click(cPcb.x, cPcb.y); await page.waitForTimeout(500);
await latency('pcbnew wheel -> first rect in pcb frame', R.pcb, async (i) => { await page.mouse.move(cPcb.x, cPcb.y); await page.mouse.wheel(0, i % 2 ? 300 : -300); });
await page.mouse.click(cSch.x, cSch.y); await page.waitForTimeout(500);
await latency('eeschema wheel -> first rect in sch frame', R.sch, async (i) => { await page.mouse.move(cSch.x, cSch.y); await page.mouse.wheel(0, i % 2 ? 300 : -300); });
// keys: Home/Ctrl+wheel are zoom; use 'Home' then wheel so each Home changes something
await latency('eeschema key Home (after wheel) -> first rect', R.sch, async () => { await page.mouse.move(cSch.x, cSch.y); await page.mouse.wheel(0, -300); await page.waitForTimeout(500); await page.keyboard.press('Home'); }, 6);
await page.mouse.click(cPcb.x, cPcb.y); await page.waitForTimeout(500);
await latency('pcbnew key Home (after wheel) -> first rect', R.pcb, async () => { await page.mouse.move(cPcb.x, cPcb.y); await page.mouse.wheel(0, -300); await page.waitForTimeout(500); await page.keyboard.press('Home'); }, 6);
// fps: middle-drag pan in pcbnew 6 s, count distinct flips (fbu) and rects in pcb region
async function pan(name, c, reg, secs = 6) {
  await page.mouse.click(c.x, c.y); await page.waitForTimeout(300);
  const t0 = await now(); const n0 = await page.evaluate(() => window.__stats.fbu);
  const rc0 = await page.evaluate(() => window.__rects.length);
  await page.mouse.move(c.x, c.y); await page.mouse.down({ button: 'middle' });
  const tEnd = Date.now() + secs * 1000; let i = 0;
  while (Date.now() < tEnd) { i++; await page.mouse.move(c.x + Math.sin(i / 8) * 150, c.y + Math.cos(i / 8) * 100, { steps: 2 }); await page.waitForTimeout(25); }
  await page.mouse.up({ button: 'middle' });
  const t1 = await now(); const n1 = await page.evaluate(() => window.__stats.fbu);
  const rects = await page.evaluate(([rc0, reg]) => { const inter = (r) => r[1] < reg[2] && r[1] + r[3] > reg[0] && r[2] < reg[3] && r[2] + r[4] > reg[1]; const rs = window.__rects.slice(rc0); const hit = rs.filter(inter); const ts = [...new Set(hit.map(r => Math.round(r[0] / 4)))]; return { rectsInRegion: hit.length, distinctPaintTicks: ts.length, rectsTotal: rs.length }; }, [rc0, reg]);
  const r = { seconds: +((t1 - t0) / 1000).toFixed(2), moves: i, fbu: n1 - n0, fbuPerS: +((n1 - n0) / ((t1 - t0) / 1000)).toFixed(1), ...rects, paintTicksPerS: +(rects.distinctPaintTicks / ((t1 - t0) / 1000)).toFixed(1) };
  console.log(name, JSON.stringify(r)); out.runs[name] = r;
}
const cpuPy = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/winstream/.venv/Scripts/python.exe';
const cpuScript = path.join(HERE, 'cpu_sample.py');
const { spawn } = await import('node:child_process');
function cpu(tag, secs) { return new Promise((res) => { const p = spawn(cpuPy, [cpuScript, String(secs), tag]); let o = ''; p.stdout.on('data', d => o += d); p.on('close', () => res(o.trim())); }); }
let c = cpu('idle', 5); await page.waitForTimeout(5200); console.log('cpu idle', await c); out.cpu_idle = JSON.parse(await c);
c = cpu('pan_pcb', 6); await pan('pan pcbnew 6 s', cPcb, R.pcb, 6); out.cpu_pan_pcb = JSON.parse(await c); console.log('cpu pan pcb', JSON.stringify(out.cpu_pan_pcb));
c = cpu('pan_sch', 6); await pan('pan eeschema 6 s', cSch, R.sch, 6); out.cpu_pan_sch = JSON.parse(await c); console.log('cpu pan sch', JSON.stringify(out.cpu_pan_sch));
// both busy: pan pcbnew while eeschema... one cursor only -> alternate quickly between the two (wheel zoom each)
c = cpu('alt_both', 6);
{
  const t0 = await now(); const n0 = await page.evaluate(() => window.__stats.fbu); const tEnd = Date.now() + 6000; let i = 0;
  while (Date.now() < tEnd) { i++; await page.mouse.move(cPcb.x, cPcb.y); await page.mouse.wheel(0, i % 2 ? 300 : -300); await page.waitForTimeout(60); await page.mouse.move(cSch.x, cSch.y); await page.mouse.wheel(0, i % 2 ? 300 : -300); await page.waitForTimeout(60); }
  const t1 = await now(); const n1 = await page.evaluate(() => window.__stats.fbu);
  const r = { seconds: +((t1 - t0) / 1000).toFixed(2), zoomEventsPerEditor: i, fbu: n1 - n0, fbuPerS: +((n1 - n0) / ((t1 - t0) / 1000)).toFixed(1) };
  console.log('alternating zoom both editors', JSON.stringify(r)); out.runs['alternating zoom both editors'] = r;
}
out.cpu_alt_both = JSON.parse(await c); console.log('cpu alt both', JSON.stringify(out.cpu_alt_both));
await page.keyboard.press('Home');
fs.writeFileSync(path.join(HERE, 'results', 'vnc_latency.json'), JSON.stringify(out, null, 1));
await browser.close();
