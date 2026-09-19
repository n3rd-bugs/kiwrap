// Hybrid injector (real cursor + posted buttons/keys) while a FOREIGN window holds the foreground:
// does KiCad select/rotate/delete/undo/zoom/pan/open+close dialogs without ever taking focus?
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { PNG } from 'pngjs';
import { execSync } from 'node:child_process';
const HERE = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi';
const out = path.join(HERE, 'results', 'hybrid'); fs.mkdirSync(out, { recursive: true });
const BASE = 'http://127.0.0.1:8765';
const py = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/winstream/.venv/Scripts/python.exe';
const fg = () => execSync(`"${py}" -c "import win32gui,win32process;h=win32gui.GetForegroundWindow();print(win32process.GetWindowThreadProcessId(h)[1], repr(win32gui.GetWindowText(h)))"`).toString().trim();
const R = { steps: [] };
const log = (m, extra = {}) => { console.log(m, JSON.stringify(extra)); R.steps.push({ m, ...extra }); };
const stats = async () => (await fetch(BASE + '/stats')).json();
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1920, height: 986 }, deviceScaleFactor: 1 });
const pcb = await ctx.newPage();
await pcb.goto(`${BASE}/?surface=pcb&fit=0`);
await pcb.waitForFunction(() => window.__state.connected && window.__state.frames > 0, null, { timeout: 15000 });
await pcb.waitForTimeout(500);
const shot = async (name) => { const f = path.join(out, name + '.png'); await pcb.screenshot({ path: f }); return f; };
const readPng = (f) => PNG.sync.read(fs.readFileSync(f));
function diff(a, b, x0, y0, x1, y1) { let n = 0, tot = 0; for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) { const i = (y * a.width + x) * 4, j = (y * b.width + x) * 4; tot++; if (Math.abs(a.data[i] - b.data[j]) + Math.abs(a.data[i + 1] - b.data[j + 1]) + Math.abs(a.data[i + 2] - b.data[j + 2]) > 30) n++; } return +(100 * n / tot).toFixed(2); }
const CANVAS = [520, 200 + 26, 1600, 880 + 26], PANEL = [0, 150 + 26, 470, 1000 + 26];
const windows = async () => pcb.evaluate(() => window.__state.windows.map(w => w.title));
const fgBefore = fg(); log('foreground at start', { fg: fgBefore, inject: (await stats()).inject });
await pcb.mouse.move(1000, 626); await pcb.keyboard.press('Home'); await pcb.waitForTimeout(700);
let a = readPng(await shot('00-home'));
// 1. click-select the R3 reference text
await pcb.mouse.click(860, 700 + 26); await pcb.waitForTimeout(700);
let b = readPng(await shot('01-click-select'));
log('click select R3 ref', { panelDiff: diff(a, b, ...PANEL), canvasDiff: diff(a, b, ...CANVAS), fg: fg() });
// 2. R rotate
await pcb.keyboard.press('r'); await pcb.waitForTimeout(700);
let c = readPng(await shot('02-rotate'));
log('R rotate', { canvasDiff: diff(b, c, ...CANVAS), fg: fg() });
// 3. Delete, 4. Ctrl+Z
await pcb.keyboard.press('Delete'); await pcb.waitForTimeout(700);
let d = readPng(await shot('03-delete'));
log('Delete', { canvasDiff: diff(c, d, ...CANVAS), fg: fg() });
await pcb.keyboard.press('Control+z'); await pcb.waitForTimeout(700);
let e = readPng(await shot('04-undo'));
log('Ctrl+Z', { canvasDiff: diff(d, e, ...CANVAS), fg: fg() });
await pcb.keyboard.press('Control+z'); await pcb.waitForTimeout(500);
// 5. wheel zoom at (1300,700): zoom centre should be there
await pcb.mouse.move(1300, 700 + 26); await pcb.mouse.wheel(0, -240); await pcb.waitForTimeout(700);
let f = readPng(await shot('05-wheel-zoom'));
log('wheel zoom', { canvasDiff: diff(e, f, ...CANVAS), fg: fg() });
// 6. middle-drag pan
await pcb.mouse.move(1000, 626); await pcb.mouse.down({ button: 'middle' }); for (let i = 0; i < 20; i++) { await pcb.mouse.move(1000 + i * 10, 626 + i * 5); await pcb.waitForTimeout(25); } await pcb.mouse.up({ button: 'middle' }); await pcb.waitForTimeout(600);
let g = readPng(await shot('06-pan'));
log('middle-drag pan', { canvasDiff: diff(f, g, ...CANVAS), fg: fg() });
await pcb.keyboard.press('Home'); await pcb.waitForTimeout(500);
// 6b. left-drag move of the R3 reference text, then undo; the user's cursor must stay where it was
const cur0 = execSync(`"${py}" -c "import win32gui;print(win32gui.GetCursorPos())"`).toString().trim();
let g0 = readPng(await shot('06b-before-drag'));
await pcb.mouse.move(860, 726); await pcb.mouse.down(); await pcb.waitForTimeout(80);
for (let i = 1; i <= 12; i++) { await pcb.mouse.move(860 + i * 10, 726 + i * 5); await pcb.waitForTimeout(30); }
await pcb.mouse.up(); await pcb.waitForTimeout(700);
let g1 = readPng(await shot('06c-after-drag'));
const cur1 = execSync(`"${py}" -c "import win32gui;print(win32gui.GetCursorPos())"`).toString().trim();
log('left-drag move R3 ref', { canvasDiff: diff(g0, g1, ...CANVAS), cursorBefore: cur0, cursorAfter: cur1, fg: fg() });
await pcb.keyboard.press('Control+z'); await pcb.waitForTimeout(600);
let g2 = readPng(await shot('06d-after-undo'));
log('Ctrl+Z after drag', { canvasDiff: diff(g1, g2, ...CANVAS), fg: fg() });
// 7. E on selection -> modal dialog appears?  then type, then Esc (posted -> WM_CLOSE)
await pcb.mouse.click(860, 700 + 26); await pcb.waitForTimeout(500); await pcb.keyboard.press('e');
await pcb.waitForFunction(() => window.__state.windows.length > 1, null, { timeout: 5000 }).catch(() => {});
await pcb.waitForTimeout(600);
log('E -> dialog', { windows: await windows(), fg: fg() });
await shot('07-dialog');
await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(800);
log('Esc on dialog', { windows: await windows(), fg: fg() });
await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(300);
// 8. right-click context menu
await pcb.mouse.move(1000, 626); await pcb.mouse.click(1000, 626, { button: 'right' }); await pcb.waitForTimeout(800);
const st = await stats();
log('right-click context menu', { windows: await windows(), monitor_session: st.monitor_session, fragile_seen: st.fragile_seen, fg: fg() });
await shot('08-context-menu');
await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(500);
// 9. A -> chooser, type filter text, Esc
await pcb.mouse.move(1000, 626); await pcb.keyboard.press('a');
await pcb.waitForFunction(() => window.__state.windows.some(w => w.title.includes('Chooser')), null, { timeout: 15000 }).catch(() => {});
await pcb.waitForTimeout(1200);
await pcb.keyboard.type('ecc83'); await pcb.waitForTimeout(1200);
await shot('09-chooser-typed');
log('A -> chooser + typed', { windows: await windows(), fg: fg() });
await pcb.keyboard.press('Escape'); await pcb.waitForTimeout(800);
log('Esc on chooser', { windows: await windows(), fg: fg() });
await pcb.mouse.click(1586, 208 + 26); await pcb.waitForTimeout(300);  // select tool
log('foreground at end', { fg: fg(), foreground_fixes: (await stats()).foreground_fixes, timeouts: (await stats()).inject_timeouts, input_log: (await stats()).input_log.slice(-5) });
fs.writeFileSync(path.join(out, 'hybrid.json'), JSON.stringify(R, null, 1));
await browser.close();
