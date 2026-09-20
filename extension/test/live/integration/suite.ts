/*
 * In-host integration suite for the Live KiCad module (runs inside the Extension Development Host,
 * loaded through --extensionTestsPath). Drives the harness extension (test/live/harness) against the
 * fake helper (test/live/fake_helper): session open through the fake kw_live client, helper spawn +
 * hello, /control surfaces, the custom editor showing frames, DOM-level input forwarded to /stream with
 * DPI-exact coordinates, the clipboard bridge both ways, the 3D panel, hidden-tab stream close +
 * resync on reveal, the screenshot command, view-only click-to-raise, and clean shutdown.
 *
 * A tiny sequential runner instead of mocha so nothing has to be resolvable from the scratch folder.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import type { HarnessExports } from '../harness/extension';
import type { LiveView } from '../../../src/live/webviewHost';

const SCRATCH = process.env.KIWRAP_LIVE_SCRATCH ?? path.join(process.env.TEMP ?? '.', 'kiwrap-live-it');
const LOG = process.env.KIWRAP_FAKE_HELPER_LOG ?? path.join(SCRATCH, 'fake-helper.jsonl');
const WS = process.env.KIWRAP_LIVE_WS ?? path.join(SCRATCH, 'kiwrap-live-ws');
const PCB = path.join(WS, 'ecc83-pp', 'ecc83-pp.kicad_pcb');
const SCH = path.join(WS, 'ecc83-pp', 'ecc83-pp.kicad_sch');
const PROJECT = path.dirname(PCB);

interface LogRec { t: number; ev: string; surface?: string; event?: Record<string, unknown>; msg?: Record<string, unknown>; [k: string]: unknown }
interface Snapshot { connected: boolean; mode: string; surface: string | null; frameW: number; frameH: number; canvasRect: { left: number; top: number; width: number; height: number }; layout: { scale: number }; dpr: number; inputCount: number; lastInput: unknown; stats: { frames: number; fps: number; lastFrameId: number }; focused: boolean; cardVisible: boolean }

const results: Array<{ name: string; ok: boolean; ms: number; error?: string }> = [];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
function readLog(): LogRec[] { try { return fs.readFileSync(LOG, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l) as LogRec); } catch { return []; } }
async function waitFor<T>(what: string, fn: () => T | Promise<T>, timeoutMs = 15000, every = 150): Promise<NonNullable<T>> {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) { return v as NonNullable<T>; }
    if (Date.now() - t0 > timeoutMs) { throw new Error(`timeout waiting for ${what}`); }
    await sleep(every);
  }
}
function assert(cond: unknown, msg: string): asserts cond { if (!cond) { throw new Error(`assert: ${msg}`); } }
function near(a: number, b: number, tol: number, what: string): void { if (Math.abs(a - b) > tol) { throw new Error(`${what}: ${a} vs ${b} (tol ${tol})`); } }
const snap = (v: LiveView) => v.testQuery() as Promise<Snapshot>;
const inputs = (surface: string, since = 0) => readLog().filter((r) => r.ev === 'input' && r.surface === surface && r.t >= since).map((r) => r.event as Record<string, unknown>);

export async function run(): Promise<void> {
  fs.mkdirSync(SCRATCH, { recursive: true });
  const h = await waitFor('harness exports', () => (globalThis as unknown as { __kiwrapLiveHarness?: HarnessExports }).__kiwrapLiveHarness, 20000);
  const { api, calls } = h;
  let pcbView: LiveView;
  const tests: Array<[string, () => Promise<void>]> = [
    ['commands are registered', async () => {
      const cmds = await vscode.commands.getCommands(true);
      for (const c of ['kiwrap.live.open', 'kiwrap.live.close', 'kiwrap.live.status', 'kiwrap.live.raise', 'kiwrap.live.forwardKey', 'kiwrap.live.openSurface', 'kiwrap.live.screenshot', 'kiwrap.live.attach']) { assert(cmds.includes(c), `command ${c}`); }
      assert(vscode.workspace.getConfiguration('kiwrap.live').get<string>('helperPath', '').endsWith('fake_helper.mjs'), 'helperPath setting points at the fake helper');
    }],
    ['kiwrap.live.open: kw_live launch (fake) -> helper spawned -> surfaces listed -> hidden mode', async () => {
      const st = await api.open({ project: PROJECT, editor: 'pcb', file: PCB });
      assert(calls[0]?.name === 'kw_live' && calls[0].args.action === 'launch' && calls[0].args.mode === 'auto' && calls[0].args.project === PROJECT, `launch call ${JSON.stringify(calls[0])}`);
      assert(st.pid === process.pid, 'session pid from kw_live');
      assert(st.helper && st.helper.port > 0 && /fake/.test(st.helper.version), `helper hello ${JSON.stringify(st.helper)}`);
      assert(st.mode === 'hidden', `mode hidden (patched install), got ${st.mode}`);
      const kinds = st.surfaces.map((s) => s.kind).sort();
      assert(kinds.includes('pcb') && kinds.includes('sch'), `surfaces ${kinds.join(',')}`);
      const hello = readLog().find((r) => r.ev === 'hello');
      assert(hello && hello.inject === 'events' && hello.cloak === true, `helper started with --inject events --cloak-expected: ${JSON.stringify(hello)}`);
      assert(st.surfaces.every((s) => s.cloaked), 'surfaces reported cloaked');
      const pcb = st.surfaces.find((s) => s.kind === 'pcb');
      assert(pcb && pcb.rect_px.w === 960 && pcb.rect_px.h === 600 && pcb.rect_px.x === 100, `rect_px normalised from [l,t,r,b]: ${JSON.stringify(pcb?.rect_px)}`);
    }],
    ['custom editor kiwrap.live shows the pcb surface and paints frames', async () => {
      pcbView = await waitFor('pcb editor view', () => api.editors.viewFor(PCB));
      assert(pcbView.panel.viewType === 'kiwrap.live', `viewType ${pcbView.panel.viewType}`);
      await waitFor('bound surface', () => pcbView.boundSurface?.id === 'pcb-1');
      const s = await waitFor('frames painted', async () => { const x = await snap(pcbView); return x.connected && x.stats.frames >= 3 && !x.cardVisible ? x : null; }, 20000, 250);
      assert(s.frameW === 960 && s.frameH === 600, `frame size ${s.frameW}x${s.frameH}`);
      assert(s.mode === 'hidden', `page mode ${s.mode}`);
      assert(s.canvasRect.width > 100 && s.canvasRect.height > 50, `canvas laid out ${JSON.stringify(s.canvasRect)}`);
      near(s.canvasRect.width / s.canvasRect.height, 960 / 600, 0.02, 'aspect ratio kept');
      assert(s.stats.lastFrameId > 0, 'frame ids parsed');
    }],
    ['DOM input is forwarded to /stream with DPI-exact surface coordinates', async () => {
      const s = await snap(pcbView);
      const r = s.canvasRect;
      const cx = r.left + r.width * 0.25, cy = r.top + r.height * 0.5;
      const since = Date.now();
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerdown', init: { clientX: cx, clientY: cy, button: 0, buttons: 1 } });
      pcbView.testDispatch({ kind: 'pointer', name: 'pointermove', init: { clientX: cx + r.width * 0.25, clientY: cy, button: 0, buttons: 1 } });
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerup', init: { clientX: cx + r.width * 0.25, clientY: cy, button: 0, buttons: 0 } });
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerdown', init: { clientX: cx, clientY: cy, button: 1, buttons: 4 } });
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerup', init: { clientX: cx, clientY: cy, button: 1, buttons: 0 } });
      pcbView.testDispatch({ kind: 'wheel', name: 'wheel', init: { clientX: cx, clientY: cy, deltaX: 0, deltaY: 100, deltaMode: 0, ctrlKey: true } });
      pcbView.testDispatch({ kind: 'key', name: 'keydown', target: 'window', init: { key: 'r', code: 'KeyR', shiftKey: true } });
      pcbView.testDispatch({ kind: 'key', name: 'keyup', target: 'window', init: { key: 'r', code: 'KeyR', shiftKey: true } });
      const evs = await waitFor('7 input events in the helper log', () => { const e = inputs('pcb-1', since).filter((x) => x.type !== 'focus' && x.type !== 'resize'); return e.length >= 8 ? e : null; }, 10000);
      const down = evs.find((e) => e.type === 'mouse' && e.down === true && e.button === 0) as Record<string, number>;
      assert(down, 'left button down');
      near(down.x, 240, 1.01, 'down.x = 25 % of 960'); near(down.y, 300, 1.01, 'down.y = 50 % of 600');
      assert(down.buttons === 1, `buttons bitmask ${down.buttons}`);
      const move = evs.find((e) => e.type === 'mouse' && e.down === undefined) as Record<string, number>;
      assert(move, 'move'); near(move.x, 480, 1.01, 'move.x = 50 %');
      const middle = evs.find((e) => e.type === 'mouse' && e.down === true && e.button === 1); assert(middle, 'middle button down forwarded (pan)');
      const wheel = evs.find((e) => e.type === 'wheel') as Record<string, number>;
      assert(wheel && wheel.dy === -100 && (wheel.modifiers as unknown as { ctrl: boolean }).ctrl === true, `wheel ${JSON.stringify(wheel)}`);
      near(wheel.x, 240, 1.01, 'wheel.x');
      const kd = evs.find((e) => e.type === 'key' && e.down === true) as Record<string, unknown>;
      assert(kd && kd.code === 'KeyR' && kd.key === 'r' && (kd.modifiers as { shift: boolean; ctrl: boolean }).shift === true && (kd.modifiers as { ctrl: boolean }).ctrl === false, `keydown ${JSON.stringify(kd)}`);
      assert(evs.some((e) => e.type === 'key' && e.down === false && e.code === 'KeyR'), 'keyup');
      const all = inputs('pcb-1');
      assert(all.some((e) => e.type === 'resize' && (e.w as number) > 0), 'resize hint sent on connect');
      const after = await snap(pcbView);
      assert(after.inputCount >= 8, `page counted ${after.inputCount} sends`);
    }],
    ['clipboard bridge: Ctrl+V hands the VS Code clipboard to the helper; helper clipboard lands in VS Code', async () => {
      await vscode.env.clipboard.writeText('paste-me-1');
      const since = Date.now();
      pcbView.testDispatch({ kind: 'key', name: 'keydown', target: 'window', init: { key: 'v', code: 'KeyV', ctrlKey: true } });
      pcbView.testDispatch({ kind: 'key', name: 'keyup', target: 'window', init: { key: 'v', code: 'KeyV', ctrlKey: true } });
      const evs = await waitFor('clipboard + key events', () => { const e = inputs('pcb-1', since); return e.some((x) => x.type === 'clipboard') && e.some((x) => x.type === 'key' && x.code === 'KeyV' && x.down === true) ? e : null; }, 8000);
      const iClip = evs.findIndex((x) => x.type === 'clipboard'), iKey = evs.findIndex((x) => x.type === 'key' && x.code === 'KeyV' && x.down === true);
      assert(evs[iClip].text === 'paste-me-1', `clipboard text ${JSON.stringify(evs[iClip])}`);
      assert(iClip < iKey, 'clipboard text arrives before the paste chord');
      await vscode.env.clipboard.writeText('');
      pcbView.testDispatch({ kind: 'key', name: 'keydown', target: 'window', init: { key: 'c', code: 'KeyC', ctrlKey: true } });
      pcbView.testDispatch({ kind: 'key', name: 'keyup', target: 'window', init: { key: 'c', code: 'KeyC', ctrlKey: true } });
      await waitFor('helper clipboard relayed to vscode.env.clipboard', async () => (await vscode.env.clipboard.readText()) === (process.env.KIWRAP_FAKE_ECHO_CLIPBOARD ?? 'from-kicad-1'), 8000);
    }],
    ['a surface pushed later (3D viewer) opens as a WebviewPanel and streams', async () => {
      const s3 = await waitFor('3d surface pushed on /control', () => api.sessions.surfaces.find('3d'), 10000);
      const view = api.panels.open(s3, vscode.ViewColumn.Beside, true);
      assert(view.panel.viewType === 'kiwrap.live.panel', 'panel view type');
      const s = await waitFor('3d frames', async () => { const x = await snap(view); return x.connected && x.stats.frames >= 2 ? x : null; }, 20000, 250);
      assert(s.surface === s3.id, 'panel bound to the 3d surface');
      assert(api.panels.open(s3) === view, 'reopening reveals the same panel');
      view.panel.dispose();
      await sleep(300);
      assert(!api.panels.views.includes(view), 'panel removed on dispose');
    }],
    ['hidden tab closes its stream after the grace period and resyncs on reveal', async () => {
      const t0 = Date.now();
      await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(SCH), 'kiwrap.live', pcbView.panel.viewColumn);
      const schView = await waitFor('sch editor view', () => api.editors.viewFor(SCH));
      await waitFor('sch frames', async () => { const x = await snap(schView); return x.connected && x.stats.frames >= 1 ? x : null; }, 20000, 250);
      await waitFor('pcb stream closed while hidden', () => readLog().some((r) => r.ev === 'stream-close' && r.surface === 'pcb-1' && r.t > t0), 10000);
      assert(!pcbView.visible, 'pcb view reports hidden');
      const framesBefore = (await snap(pcbView)).stats.frames;
      pcbView.panel.reveal(undefined, false);
      await waitFor('pcb stream reopened', () => readLog().some((r) => r.ev === 'stream-open' && r.surface === 'pcb-1' && r.t > t0), 10000);
      // a quick hide/show inside the grace period keeps the socket and asks for a keyframe instead
      await waitFor('pcb frames after reopen', async () => (await snap(pcbView)).stats.frames > framesBefore, 15000, 250);
      const t1 = Date.now();
      schView.panel.reveal(undefined, false);
      await sleep(150);
      pcbView.panel.reveal(undefined, false);
      await waitFor('keyframe requested on the still-open stream', () => inputs('pcb-1', t1).some((e) => e.type === 'keyframe'), 8000);
      assert(!readLog().some((r) => r.ev === 'stream-close' && r.surface === 'pcb-1' && r.t > t1), 'stream stayed open across the quick switch');
      await waitFor('pcb frames resumed (full-frame resync)', async () => (await snap(pcbView)).stats.frames > framesBefore, 15000, 250);
      await waitFor('pcb view visible again', () => pcbView.visible, 5000);
    }],
    ['kiwrap.live.screenshot returns a PNG through the helper', async () => {
      const save = path.join(SCRATCH, 'screenshot-pcb-1.png');
      const r = await vscode.commands.executeCommand<{ surface: string; bytes: number; w?: number; h?: number; png_base64: string }>('kiwrap.live.screenshot', { surface: 'pcb-1', save, max_px: 512 });
      assert(r && r.bytes > 100 && r.surface === 'pcb-1', `screenshot result ${JSON.stringify(r && { ...r, png_base64: r.png_base64.length })}`);
      const head = fs.readFileSync(save).subarray(0, 8);
      assert(head.equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])), 'PNG magic');
      assert((r.w ?? 0) <= 512 && (r.h ?? 0) <= 512, `<= max_px: ${r.w}x${r.h}`);
    }],
    ['desktop screenshot of the dev host (for the report)', async () => {
      // the outside runner captures the window while the pcb tab is showing frames; not a failure if absent
      pcbView.panel.reveal(undefined, false);
      pcbView.testDispatch({ kind: 'pointer', name: 'pointermove', init: { clientX: 200, clientY: 200, buttons: 0 } });
      fs.writeFileSync(path.join(SCRATCH, 'shot-request.json'), JSON.stringify({ name: 'live-editor-hidden-mode', pid: process.pid }));
      try { await waitFor('shot-done', () => fs.existsSync(path.join(SCRATCH, 'shot-done')), 15000, 250); } catch { /* no driver */ }
    }],
    ['kiwrap.live.close kills the helper and detaches through kw_live', async () => {
      const helperPid = api.status().helper?.pid ?? 0;
      assert(helperPid > 0, 'helper pid known');
      await api.close();
      const st = api.status();
      assert(st.pid === null && st.helper === null && st.surfaces.length === 0, `status cleared ${JSON.stringify(st)}`);
      assert(calls.some((c) => c.args.action === 'detach' && c.args.session_id === 'fake-session-1'), 'kw_live detach called');
      await waitFor('helper process gone', () => { try { process.kill(helperPid, 0); return false; } catch { return true; } }, 8000);
      const s = await snap(pcbView);
      assert(!s.connected && s.cardVisible, 'editor shows the placeholder card after close');
    }],
    ['view-only mode: a click raises KiCad instead of injecting', async () => {
      await api.open({ project: PROJECT, editor: 'pcb', file: PCB, mode: 'view' });
      assert(api.status().mode === 'view', 'mode view');
      const hello = readLog().filter((r) => r.ev === 'hello').pop();
      assert(hello && hello.inject === 'none' && hello.cloak === false, `helper restarted with --inject none: ${JSON.stringify(hello)}`);
      pcbView = await waitFor('pcb editor view', () => api.editors.viewFor(PCB));
      const s = await waitFor('frames in view mode', async () => { const x = await snap(pcbView); return x.connected && x.mode === 'view' && x.stats.frames >= 1 ? x : null; }, 20000, 250);
      const since = Date.now();
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerdown', init: { clientX: s.canvasRect.left + 10, clientY: s.canvasRect.top + 10, button: 0, buttons: 1 } });
      pcbView.testDispatch({ kind: 'pointer', name: 'pointerup', init: { clientX: s.canvasRect.left + 10, clientY: s.canvasRect.top + 10, button: 0, buttons: 0 } });
      pcbView.testDispatch({ kind: 'key', name: 'keydown', target: 'window', init: { key: 'a', code: 'KeyA' } });
      await waitFor('raise request on /control', () => readLog().some((r) => r.ev === 'raise' && r.surface === 'pcb-1' && r.t >= since), 8000);
      await sleep(500);
      assert(inputs('pcb-1', since).filter((e) => e.type === 'mouse' || e.type === 'key').length === 0, 'no mouse/key injected in view mode');
    }],
    ['final close leaves no helper behind', async () => {
      const helperPid = api.status().helper?.pid ?? 0;
      await api.close();
      await waitFor('helper exit logged', () => readLog().some((r) => r.ev === 'exit'), 8000);
      if (helperPid) { await waitFor('helper process gone', () => { try { process.kill(helperPid, 0); return false; } catch { return true; } }, 8000); }
    }],
  ];

  for (const [name, fn] of tests) {
    const t0 = Date.now();
    try { await fn(); results.push({ name, ok: true, ms: Date.now() - t0 }); console.log(`  ok   ${name} (${Date.now() - t0} ms)`); }
    catch (e) { const error = e instanceof Error ? e.stack ?? e.message : String(e); results.push({ name, ok: false, ms: Date.now() - t0, error }); console.error(`  FAIL ${name}: ${error}`); }
  }
  fs.writeFileSync(path.join(SCRATCH, 'results.json'), JSON.stringify(results, null, 2));
  try { await api.close(); } catch { /* ignore */ }
  const failed = results.filter((r) => !r.ok);
  console.log(`live integration: ${results.length - failed.length}/${results.length} passed`);
  if (failed.length) { throw new Error(`${failed.length} live integration test(s) failed: ${failed.map((f) => f.name).join('; ')}`); }
}
