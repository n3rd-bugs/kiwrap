// Generic Playwright driver for the winstream viewer: runs a JSON action list against the page.
//   node drive.mjs <outdir> <actions.json> [port]
// Actions (frame px = physical px of the KiCad window frame; the page scales them):
//   {a:"move",x,y} {a:"click",x,y,button?} {a:"down",x,y,button?} {a:"up",x,y,button?}
//   {a:"drag",x1,y1,x2,y2,button?,steps?} {a:"wheel",x,y,dy} {a:"key",code} (press+release)
//   {a:"keydown",code} {a:"keyup",code} {a:"chord",codes:[...]} {a:"type",text}
//   {a:"sleep",ms} {a:"shot",name} {a:"stats",name} {a:"windows",name} {a:"clipboard",name}
//   {a:"cursor",name} {a:"probe",name,ev:{...}} {a:"diff",name,a:"shot1",b:"shot2",rect:[x,y,w,h]}
//   {a:"waitwindows",count,timeout}
import { chromium } from 'playwright';
import fs from 'node:fs';
import { PNG } from 'pngjs';

const [out, actionsFile, portArg] = process.argv.slice(2);
const port = portArg || 8765; const base = `http://127.0.0.1:${port}`;
fs.mkdirSync(out, { recursive: true });
const actions = JSON.parse(fs.readFileSync(actionsFile, 'utf8'));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = {};
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
await page.goto(`${base}/`);
await page.waitForFunction(() => window.__state.frames > 0 && window.__state.w > 0, null, { timeout: 30000 });
await sleep(500);
const stageState = () => page.evaluate(() => { const r = document.getElementById('stage').getBoundingClientRect(); return { left: r.left, top: r.top, scale: window.__state.scale, ox: window.__state.ox, oy: window.__state.oy }; });
let st = await stageState();
// frame px -> page px; re-read per action because the stage grows/offsets when a dialog opens
const P = (x, y) => [st.left + (x + st.ox) * st.scale, st.top + (y + st.oy) * st.scale];
const btn = b => ['left', 'middle', 'right'][b || 0];
const shots = {};
const shot = async name => { const p = `${out}/${name}.png`; await page.screenshot({ path: p }); shots[name] = p; };
const png = f => PNG.sync.read(fs.readFileSync(f));

for (const act of actions) {
  const a = act.a;
  st = await stageState();
  try {
    if (a === 'move') { const [x, y] = P(act.x, act.y); await page.mouse.move(x, y); }
    else if (a === 'click') { const [x, y] = P(act.x, act.y); await page.mouse.move(x, y); await sleep(30); await page.mouse.down({ button: btn(act.button) }); await sleep(40); await page.mouse.up({ button: btn(act.button) }); }
    else if (a === 'down') { const [x, y] = P(act.x, act.y); await page.mouse.move(x, y); await page.mouse.down({ button: btn(act.button) }); }
    else if (a === 'up') { const [x, y] = P(act.x, act.y); await page.mouse.move(x, y); await page.mouse.up({ button: btn(act.button) }); }
    else if (a === 'drag') {
      const [x1, y1] = P(act.x1, act.y1), [x2, y2] = P(act.x2, act.y2); const n = act.steps || 20;
      await page.mouse.move(x1, y1); await sleep(30); await page.mouse.down({ button: btn(act.button) }); await sleep(60);
      for (let i = 1; i <= n; i++) { await page.mouse.move(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n); await sleep(act.step_ms || 15); }
      await sleep(60); await page.mouse.up({ button: btn(act.button) });
    }
    else if (a === 'wheel') { await page.evaluate(([x, y, dy]) => window.__send({ t: 'wheel', x, y, dy, dx: 0 }), [act.x, act.y, act.dy]); }
    else if (a === 'key') { await page.keyboard.press(act.code); }
    else if (a === 'keydown') { await page.keyboard.down(act.code); }
    else if (a === 'keyup') { await page.keyboard.up(act.code); }
    else if (a === 'chord') { for (const c of act.codes) { await page.keyboard.down(c); await sleep(20); } for (const c of [...act.codes].reverse()) { await page.keyboard.up(c); await sleep(20); } }
    else if (a === 'type') { for (const ch of act.text) { await page.keyboard.press(ch === ' ' ? 'Space' : ch); await sleep(30); } }
    else if (a === 'sleep') await sleep(act.ms);
    else if (a === 'shot') await shot(act.name);
    else if (a === 'stats') results[act.name] = await (await fetch(`${base}/stats`)).json();
    else if (a === 'windows') results[act.name] = (await (await fetch(`${base}/stats`)).json()).windows;
    else if (a === 'clipboard') results[act.name] = await (await fetch(`${base}/clipboard`)).json();
    else if (a === 'cursor') results[act.name] = await (await fetch(`${base}/cursor`)).json();
    else if (a === 'probe') results[act.name] = await page.evaluate(ev => window.__probe(ev), act.ev);
    else if (a === 'pagestats') results[act.name] = await page.evaluate(() => window.__stats());
    else if (a === 'waitwindows') { const t0 = Date.now(); let n = 0; while (Date.now() - t0 < (act.timeout || 5000)) { n = (await (await fetch(`${base}/stats`)).json()).windows.length; if (n >= act.count) break; await sleep(100); } results[act.name || 'waitwindows'] = { windows: n, ms: Date.now() - t0 }; }
    else if (a === 'diff') {
      const A = png(shots[act.s1]), B = png(shots[act.s2]);
      const [rx, ry, rw, rh] = act.rect ? [Math.round(st.left + (act.rect[0] + st.ox) * st.scale), Math.round(st.top + (act.rect[1] + st.oy) * st.scale), Math.round(act.rect[2] * st.scale), Math.round(act.rect[3] * st.scale)] : [0, 0, A.width, A.height];
      let changed = 0, total = 0;
      for (let y = ry; y < Math.min(ry + rh, A.height); y++) for (let x = rx; x < Math.min(rx + rw, A.width); x++) {
        const i = (y * A.width + x) * 4; total++;
        if (Math.abs(A.data[i] - B.data[i]) + Math.abs(A.data[i + 1] - B.data[i + 1]) + Math.abs(A.data[i + 2] - B.data[i + 2]) > 60) changed++;
      }
      results[act.name] = { changed, total, pct: +(100 * changed / total).toFixed(2) };
    }
    if (act.after_ms) await sleep(act.after_ms);
  } catch (e) { results[`error_${a}_${act.name || ''}`] = String(e); }
}
results._shots = shots;
fs.writeFileSync(`${out}/results.json`, JSON.stringify(results, null, 1));
console.log(JSON.stringify(results, null, 1));
await browser.close();
