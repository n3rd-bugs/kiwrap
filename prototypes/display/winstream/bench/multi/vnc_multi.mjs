// TightVNC -shareapp <pid> + noVNC: what does the framebuffer contain with pcbnew + eeschema + manager + 3D viewer
// open in ONE kicad.exe?  usage: node vnc_multi.mjs <step>  (steps: base | overlap | minimised | dialog | chooser | input | load)
// Writes shots/vnc_<step>_*.png (canvas screenshots = what the client sees) and results/vnc_<step>.json.
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { PNG } from 'pngjs';

const step = process.argv[2] || 'base';
const HERE = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi';
const shots = path.join(HERE, 'shots'); fs.mkdirSync(shots, { recursive: true });
const sess = JSON.parse(fs.readFileSync(path.join(HERE, 'session.json'), 'utf8'));
const results = { step, steps: [] };
const log = (m, extra = {}) => { console.log(m, JSON.stringify(extra)); results.steps.push({ m, ...extra, t: Date.now() }); };

const browser = await chromium.launch({ headless: true });
// viewport big enough for a 3840x2160 framebuffer at 1:1 (Chromium clamps, so use fit=window instead and read the fb size)
const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
page.on('console', msg => { if (msg.type() === 'error') console.log('[page]', msg.text()); });
await page.goto('http://127.0.0.1:6080/kicad.html?nostatus=1&fit=window');
await page.waitForFunction(() => window.__connected === true, null, { timeout: 15000 });
await page.waitForTimeout(2500);
const fb = await page.evaluate(() => ({ w: window.rfb._fbWidth, h: window.rfb._fbHeight, name: window.rfb._fbName }));
log('connected', fb);
const canvas = page.locator('#screen canvas');
const box = await canvas.boundingBox();
const scale = box.width / fb.w;
log('canvas box', { ...box, scale: +scale.toFixed(4) });
// framebuffer (x,y) -> page coords
const P = (x, y) => ({ x: box.x + x * scale, y: box.y + y * scale });
// The -shareapp framebuffer: is it the full virtual desktop (7680x2160) or a monitor?  fb.w tells us.
// KiCad windows are on monitor 2 (origin 3840,0): fb coords = screen - origin if the fb is one monitor.
async function fbOrigin() {
  // probe: the pcbnew frame's known screen rect [5700,60,7620,1140]; find whether its title bar exists at fb x 5700 or 1860
  return fb.w >= 7000 ? { x: 0, y: 0 } : { x: 3840, y: 0 };
}
const origin = await fbOrigin();
const FB = (sx, sy) => P(sx - origin.x, sy - origin.y);
async function shot(name, full = false) {
  const f = path.join(shots, `vnc_${step}_${name}.png`);
  if (full) await page.screenshot({ path: f }); else await canvas.screenshot({ path: f });
  return f;
}
// full-resolution framebuffer read: the noVNC display has a backing canvas at fb resolution
async function fbPng(name) {
  const dataUrl = await page.evaluate(() => window.rfb._display._backCanvas ? window.rfb._display._backCanvas.toDataURL('image/png') : window.rfb._canvas.toDataURL('image/png'));
  const f = path.join(shots, `vnc_${step}_${name}_fb.png`);
  fs.writeFileSync(f, Buffer.from(dataUrl.split(',')[1], 'base64'));
  return f;
}
function readPng(f) { return PNG.sync.read(fs.readFileSync(f)); }
function regionStats(p, x0, y0, x1, y1) {
  let sum = 0, black = 0, tot = 0; const colours = new Set();
  x1 = Math.min(x1, p.width); y1 = Math.min(y1, p.height);
  for (let y = y0; y < y1; y += 2) for (let x = x0; x < x1; x += 2) { const i = (y * p.width + x) * 4; const l = (p.data[i] + p.data[i + 1] + p.data[i + 2]) / 3; sum += l; if (l < 8) black++; tot++; colours.add((p.data[i] >> 4) << 8 | (p.data[i + 1] >> 4) << 4 | (p.data[i + 2] >> 4)); }
  return { meanLum: +(sum / tot).toFixed(1), blackPct: +(100 * black / tot).toFixed(1), colours: colours.size };
}
function diffRegion(a, b, x0, y0, x1, y1) {
  let n = 0, tot = 0; x1 = Math.min(x1, a.width, b.width); y1 = Math.min(y1, a.height, b.height);
  for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) { const i = (y * a.width + x) * 4, j = (y * b.width + x) * 4; tot++; if (Math.abs(a.data[i] - b.data[j]) + Math.abs(a.data[i + 1] - b.data[j + 1]) + Math.abs(a.data[i + 2] - b.data[j + 2]) > 30) n++; }
  return { changed: n, total: tot, pct: +(100 * n / tot).toFixed(2) };
}
async function fbu() { return page.evaluate(() => window.__stats.fbu); }
async function waitChange(prev, timeout = 3000) { const t0 = performance.now(); try { await page.waitForFunction(p => window.__stats.fbu > p, prev, { timeout, polling: 5 }); } catch { return -1; } return +(performance.now() - t0).toFixed(1); }
const rectOf = (hwnd) => { const w = sess.windows_now?.find(w => w.hwnd === hwnd); return w ? w.rect : null; };
// fb-space rects of the frames (from session.json written by the python side, screen px)
const R = (r) => [r[0] - origin.x, r[1] - origin.y, r[2] - origin.x, r[3] - origin.y];

const f0 = await fbPng('00');
const p0 = readPng(f0);
log('fb png', { w: p0.width, h: p0.height });
const regions = sess.regions || {};
for (const [name, r] of Object.entries(regions)) {
  const rr = R(r);
  log(`region ${name}`, { fbRect: rr, ...regionStats(p0, ...rr) });
}
await shot('01_client');

if (step === 'input') {
  // click into the eeschema canvas and the pcbnew canvas, press Home in each, watch which one changes
  const pcb = R(regions.pcb), sch = R(regions.sch);
  const cPcb = FB(regions.pcb[0] + 1000, regions.pcb[1] + 600), cSch = FB(regions.sch[0] + 1000, regions.sch[1] + 700);
  let before = await fbu();
  await page.mouse.click(cPcb.x, cPcb.y); await waitChange(before); await page.waitForTimeout(300);
  const a = readPng(await fbPng('10_click_pcb'));
  before = await fbu(); await page.mouse.wheel(0, -300); const dtW = await waitChange(before); await page.waitForTimeout(600);
  const b = readPng(await fbPng('11_wheel_pcb'));
  log('wheel over pcbnew', { dt: dtW, pcbDiff: diffRegion(a, b, ...pcb), schDiff: diffRegion(a, b, ...sch) });
  before = await fbu(); await page.mouse.click(cSch.x, cSch.y); await waitChange(before); await page.waitForTimeout(300);
  const c = readPng(await fbPng('12_click_sch'));
  before = await fbu(); await page.mouse.wheel(0, -300); const dtS = await waitChange(before); await page.waitForTimeout(600);
  const d = readPng(await fbPng('13_wheel_sch'));
  log('wheel over eeschema', { dt: dtS, pcbDiff: diffRegion(c, d, ...pcb), schDiff: diffRegion(c, d, ...sch) });
  // key: Home in eeschema (focused), then Home in pcbnew after click
  before = await fbu(); await page.keyboard.press('Home'); const dtH = await waitChange(before); await page.waitForTimeout(600);
  const e = readPng(await fbPng('14_home_sch'));
  log('Home (eeschema focused)', { dt: dtH, pcbDiff: diffRegion(d, e, ...pcb), schDiff: diffRegion(d, e, ...sch) });
  await page.mouse.click(cPcb.x, cPcb.y); await page.waitForTimeout(300);
  const e2 = readPng(await fbPng('15_click_pcb'));
  before = await fbu(); await page.keyboard.press('Home'); const dtH2 = await waitChange(before); await page.waitForTimeout(600);
  const f = readPng(await fbPng('16_home_pcb'));
  log('Home (pcbnew focused)', { dt: dtH2, pcbDiff: diffRegion(e2, f, ...pcb), schDiff: diffRegion(e2, f, ...sch) });
}
if (step === 'load') {
  // middle-drag pan in pcbnew for 5 s while eeschema is also being repainted (wheel zoom alternating) -> fbu/s
  const cPcb = FB(regions.pcb[0] + 1000, regions.pcb[1] + 600), cSch = FB(regions.sch[0] + 1000, regions.sch[1] + 700);
  await page.mouse.click(cPcb.x, cPcb.y); await page.waitForTimeout(300);
  const n0 = await fbu(); const t0 = performance.now();
  await page.mouse.move(cPcb.x, cPcb.y); await page.mouse.down({ button: 'middle' });
  for (let i = 0; i < 100; i++) { await page.mouse.move(cPcb.x + Math.sin(i / 8) * 150, cPcb.y + Math.cos(i / 8) * 100, { steps: 2 }); await page.waitForTimeout(30); }
  await page.mouse.up({ button: 'middle' });
  const t1 = performance.now(); const n1 = await fbu();
  log('pan pcbnew 100 moves', { seconds: +((t1 - t0) / 1000).toFixed(2), fbu: n1 - n0, fbuPerS: +((n1 - n0) / ((t1 - t0) / 1000)).toFixed(1) });
  // both editors: alternate wheel zoom in each for 5 s
  const n2 = await fbu(); const t2 = performance.now();
  for (let i = 0; i < 20; i++) {
    await page.mouse.move(cPcb.x, cPcb.y); await page.mouse.wheel(0, i % 2 ? 200 : -200); await page.waitForTimeout(120);
    await page.mouse.move(cSch.x, cSch.y); await page.mouse.wheel(0, i % 2 ? 200 : -200); await page.waitForTimeout(120);
  }
  const t3 = performance.now(); const n3 = await fbu();
  log('alternating zoom both editors', { seconds: +((t3 - t2) / 1000).toFixed(2), fbu: n3 - n2, fbuPerS: +((n3 - n2) / ((t3 - t2) / 1000)).toFixed(1) });
  await fbPng('20_after_load');
}
fs.writeFileSync(path.join(HERE, 'results', `vnc_${step}.json`), JSON.stringify(results, null, 1));
await browser.close();
