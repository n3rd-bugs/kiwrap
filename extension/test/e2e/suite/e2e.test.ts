/**
 * The Phase 2 end-to-end, for real, inside an Extension Development Host (test/e2e/runTest.ts).
 * The product path (docs/DECISIONS.md D20: the live view of a stock KiCad, native interaction):
 *
 *  1. activate; Configure Claude Code writes .mcp.json (+ CLAUDE.md block, skill copy)
 *  2. the .kicad_pcb opens in the kiwrap.preview custom editor and renders
 *  3. Open Live KiCad: kw_live launches a sandboxed stock KiCad in view mode (a visible window,
 *     never activated), the Rust helper attaches with --inject none, the kiwrap.live custom editor
 *     streams the real pcbnew frame; "Raise KiCad" brings the window to the front
 *     (GetForegroundWindow verified); the .kicad_sch tab shows eeschema (a second, schematic
 *     session); the same on stock 10.0.6 when it is installed
 *  4. an agent-style edit through a SECOND kiwrap MCP client (test/e2e/agent.py, its own
 *     `kiwrap serve`): kw_pcb_place on the bound project -> the footprint moves in the live tab
 *     (frame timing + before/after screenshots)
 *  5. Run ERC / DRC -> Problems panel entries with locations, while the session is live; the
 *     keybinding shield stays disarmed (VS Code's keys are VS Code's in the live view)
 *  6. close everything: no KiCad / helper processes left, the project's lock files gone
 *
 * Opt-in (KIWRAP_E2E_HIDDEN=1 or an install root; the experimental, parked path): the cloaked
 * mode on a build carrying mr/display-input - KiCad invisible on the desktop, input from the tab
 * (click, wheel, Home, select-all + R read back over IPC, dialog), the user's cursor and
 * foreground untouched, the shield armed.
 *
 * Evidence: PNGs under KIWRAP_E2E_IMG (phase2-*.png, downscaled to stay < 300 KB) and the
 * measurements in <out>/e2e-report.json.
 */
import * as assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { KiwrapApi } from '../../../src/extension';
import type { LiveView } from '../../../src/live/webviewHost';
import type { Surface } from '../../../src/shared/types';

const EXTENSION_ID = 'kiwrap-private.kiwrap';
const WS = process.env.KIWRAP_E2E_WORKSPACE ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
const REPO = process.env.KIWRAP_E2E_REPO ?? path.resolve(__dirname, '..', '..', '..', '..', '..');
const OUT = process.env.KIWRAP_E2E_OUT ?? path.join(WS, '..', 'out');
const IMG = process.env.KIWRAP_E2E_IMG ?? OUT;
const UDD = process.env.KIWRAP_E2E_UDD ?? '';
const STOCK = process.env.KIWRAP_E2E_STOCK ?? '10.99';
/** The second stock install to open the live view on (a version spec kw_env lists); '' / 'skip' skips that step. */
const STOCK2 = process.env.KIWRAP_E2E_STOCK2 ?? '10.0';
/** Opt-in: '1' = the first install with gui.input_from_events, a path = that install root, '' / 'skip' = skipped. */
const HIDDEN = process.env.KIWRAP_E2E_HIDDEN ?? '';
const PROJECT = path.join(WS, 'ecc83');
const PRO = path.join(PROJECT, 'ecc83-pp.kicad_pro');
const PCB = path.join(PROJECT, 'ecc83-pp.kicad_pcb');
const SCH = path.join(PROJECT, 'ecc83-pp.kicad_sch');
/** The Phase 1b blinker board (10.0 format): what the 10.0.x live-view step opens, since the ecc83 copy is re-saved in the 10.99 format by then. */
const BLINKER_PROJECT = path.join(WS, 'blinker');
const BLINKER_PCB = path.join(BLINKER_PROJECT, 'blinky.kicad_pcb');
const CAPTURE = path.resolve(__dirname, '..', '..', '..', '..', 'test', 'e2e', 'capture.ps1');
const AGENT = path.resolve(__dirname, '..', '..', '..', '..', 'test', 'e2e', 'agent.py');

interface Snapshot {
  connected: boolean;
  mode: string;
  surface: string | null;
  frameW: number;
  frameH: number;
  canvasRect: { left: number; top: number; width: number; height: number };
  layout: { scale: number };
  dpr: number;
  inputCount: number;
  stats: { frames: number; fps: number; lastFrameId: number; inputToFrameMs: number | null };
  focused: boolean;
  cardVisible: boolean;
}
interface AgentLine { event: string; t: number; [k: string]: unknown }

const report: Record<string, unknown> = { started: new Date().toISOString(), stock: STOCK };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function waitFor<T>(what: string, fn: () => T | Promise<T>, timeoutMs = 30000, every = 100): Promise<NonNullable<T>> {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) {
      return v as NonNullable<T>;
    }
    if (Date.now() - t0 > timeoutMs) {
      throw new Error(`timeout (${timeoutMs} ms) waiting for ${what}`);
    }
    await sleep(every);
  }
}
const snap = (v: LiveView) => v.testQuery(8000) as Promise<Snapshot>;

function ps(args: string[]): string {
  const r = spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', CAPTURE, ...args], { encoding: 'utf8', timeout: 60000 });
  if (r.status !== 0) {
    throw new Error(`capture.ps1 ${args.join(' ')} failed: ${r.stderr || r.stdout}`);
  }
  return r.stdout.trim();
}
function captureHost(name: string): string {
  const out = path.join(IMG, `${name}.png`);
  const msg = ps(['-Mode', 'window', '-Out', out, '-Udd', UDD, '-MaxPx', '1400']);
  console.log(`[e2e] ${msg}`);
  return out;
}
function captureRect(name: string, r: Surface['rect_px']): string {
  const out = path.join(IMG, `${name}.png`);
  const msg = ps(['-Mode', 'rect', '-Out', out, '-Rect', `${r.x},${r.y},${r.x + r.w},${r.y + r.h}`, '-MaxPx', '1024']);
  console.log(`[e2e] ${msg}`);
  return out;
}
/** Bring the dev host to the foreground and type a chord (capture.ps1 -Mode keys); `sent` is false when it could not. */
function sendKeys(keys: string): { sent: boolean; foreground_pid?: number; host_pid?: number; reason?: string } {
  return JSON.parse(ps(['-Mode', 'keys', '-Udd', UDD, '-Keys', keys]));
}
/** Bring the dev host back to the foreground (after "Raise KiCad" handed it to KiCad). */
function activateHost(): { activated: boolean; foreground_pid?: number; host_pid?: number } {
  return JSON.parse(ps(['-Mode', 'activate', '-Udd', UDD]));
}
/**
 * "Raise KiCad" from the tab (the primary action of the live view) and verify with
 * GetForegroundWindow that the real KiCad window took the foreground; then hand it back to the
 * dev host so the run continues in VS Code.
 */
async function raiseAndVerify(view: LiveView, kicadPid: number, how: 'button' | 'dblclick' | 'command'): Promise<{ how: string; ms: number; foreground_after: { hwnd: number; pid: number; title: string }; kicad_foreground: boolean; host_back: boolean }> {
  const before = (await snap(view)) as Snapshot & { raiseCount?: number };
  const t0 = Date.now();
  if (how === 'button') { view.testDispatch({ kind: 'mouse', name: 'click', target: 'btn-raise', init: {} }); }
  else if (how === 'dblclick') { const r = before.canvasRect; view.testDispatch({ kind: 'mouse', name: 'dblclick', init: { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 } }); }
  else { await vscode.commands.executeCommand('kiwrap.live.raise'); }
  let fg = probe().foreground;
  for (let i = 0; i < 40 && fg.pid !== kicadPid; i++) { await sleep(100); fg = probe().foreground; }
  const ms = Date.now() - t0;
  const kicadForeground = fg.pid === kicadPid;
  if (how !== 'command') {
    const after = (await snap(view)) as Snapshot & { raiseCount?: number; lastRaise?: string };
    assert.ok((after.raiseCount ?? 0) > (before.raiseCount ?? 0) && after.lastRaise === how, `the page sent the raise (${how}): ${JSON.stringify({ before: before.raiseCount, after: after.raiseCount, last: after.lastRaise })}`);
  }
  await sleep(300);
  const back = activateHost();
  await sleep(300);
  return { how, ms, foreground_after: fg, kicad_foreground: kicadForeground, host_back: !!back.activated };
}
function untitledEditors(): number {
  return vscode.window.tabGroups.all.flatMap((g) => g.tabs).filter((t) => t.input instanceof vscode.TabInputText && t.input.uri.scheme === 'untitled').length;
}
function probe(rect?: Surface['rect_px']): { cursor: number[]; foreground: { hwnd: number; pid: number; title: string }; window_from_point?: Array<{ at: number[]; hwnd: number; pid: number; title: string }> } {
  const args = ['-Mode', 'probe'];
  if (rect) {
    args.push('-Rect', `${rect.x},${rect.y},${rect.x + rect.w},${rect.y + rect.h}`);
  }
  return JSON.parse(ps(args));
}
async function saveHelperShot(api: KiwrapApi, surfaceId: string, name: string, maxPx = 1024): Promise<{ file: string; bytes: number; w?: number; h?: number }> {
  const shot = await api.live.sessions.screenshot(surfaceId, maxPx);
  const file = path.join(IMG, `${name}.png`);
  fs.writeFileSync(file, shot.png);
  return { file, bytes: shot.png.length, w: shot.w, h: shot.h };
}
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === 'EPERM';
  }
}
function processes(names: string[]): Array<{ name: string; pid: number }> {
  const r = spawnSync('powershell', ['-NoProfile', '-Command', `Get-Process ${names.join(',')} -ErrorAction SilentlyContinue | Select-Object ProcessName,Id | ConvertTo-Json -Compress`], { encoding: 'utf8' });
  const text = r.stdout.trim();
  if (!text) {
    return [];
  }
  const parsed = JSON.parse(text) as { ProcessName: string; Id: number } | Array<{ ProcessName: string; Id: number }>;
  return (Array.isArray(parsed) ? parsed : [parsed]).map((p) => ({ name: p.ProcessName, pid: p.Id }));
}

/** Run test/e2e/agent.py (a second `kiwrap serve` over stdio) and stream its JSON lines. */
function runAgent(args: string[], onLine?: (l: AgentLine) => void): Promise<{ lines: AgentLine[]; code: number; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn('uv', ['run', '--project', REPO, 'python', AGENT, '--repo', REPO, '--project', PROJECT, ...args], { cwd: REPO, shell: true, windowsHide: true });
    const lines: AgentLine[] = [];
    let buf = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith('{')) {
          continue;
        }
        try {
          const parsed = JSON.parse(line) as AgentLine;
          lines.push(parsed);
          onLine?.(parsed);
        } catch {
          /* not json */
        }
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (c: string) => { stderr += c; });
    child.on('error', reject);
    child.on('close', (code) => resolve({ lines, code: code ?? -1, stderr }));
  });
}

/** kw_live status through the extension's OWN server (the session owner): mode_used, cloaked, foreground_*. */
async function ownerStatus(): Promise<{ mode_used?: string; cloaked?: boolean; foreground: { taken?: boolean; restored?: boolean | null }; ready_after_s?: number; warnings?: string[] }> {
  const r = await vscode.commands.executeCommand<{ data: Record<string, unknown> }>('kiwrap.core.callTool', 'kw_live', { action: 'status', project: PRO });
  const d = r?.data ?? {};
  return { mode_used: d.mode_used as string, cloaked: d.cloaked as boolean, foreground: { taken: d.foreground_taken as boolean, restored: d.foreground_restored as boolean | null }, ready_after_s: d.ready_after_s as number, warnings: d.warnings as string[] };
}

const key = (view: LiveView, code: string, keyName: string, init: Record<string, unknown> = {}) => {
  view.testDispatch({ kind: 'key', name: 'keydown', target: 'window', init: { key: keyName, code, ...init } });
  view.testDispatch({ kind: 'key', name: 'keyup', target: 'window', init: { key: keyName, code, ...init } });
};
const click = (view: LiveView, x: number, y: number) => {
  view.testDispatch({ kind: 'pointer', name: 'pointermove', init: { clientX: x, clientY: y, buttons: 0 } });
  view.testDispatch({ kind: 'pointer', name: 'pointerdown', init: { clientX: x, clientY: y, button: 0, buttons: 1 } });
  view.testDispatch({ kind: 'pointer', name: 'pointerup', init: { clientX: x, clientY: y, button: 0, buttons: 0 } });
};
/** Dispatch an input and wait for the tab to paint a new frame; returns the input-to-frame time. */
async function inputThenFrame(view: LiveView, what: string, fire: () => void, timeoutMs = 8000): Promise<number> {
  const before = (await snap(view)).stats.lastFrameId;
  const t0 = Date.now();
  fire();
  await waitFor(`frame after ${what}`, async () => (await snap(view)).stats.lastFrameId > before, timeoutMs, 40);
  return Date.now() - t0;
}

describe('kiwrap Phase 2 end to end (real server, helper, KiCad GUIs, second MCP client)', function () {
  this.timeout(900000);
  let api: KiwrapApi;
  let pcbView: LiveView;
  let kicadPid = 0;
  let helperPid = 0;

  before(async () => {
    fs.mkdirSync(OUT, { recursive: true });
    fs.mkdirSync(IMG, { recursive: true });
    const ext = vscode.extensions.getExtension<KiwrapApi>(EXTENSION_ID);
    assert.ok(ext, `extension ${EXTENSION_ID} not found`);
    api = await ext.activate();
    await api.whenReady();
  });

  after(async () => {
    try {
      await api.live.close();
    } catch {
      /* already closed */
    }
    report.finished = new Date().toISOString();
    fs.writeFileSync(path.join(OUT, 'e2e-report.json'), JSON.stringify(report, null, 2));
    await api.session.server?.stop();
  });

  it('1. activates: server ready, installs listed, live module + preview registered', async () => {
    assert.equal(api.session.serverState, 'ready', `server ${api.session.serverState}: ${api.session.serverDetail}`);
    const env = api.session.env;
    assert.ok(env && env.installs.length, `kw_env failed: ${api.session.envError}`);
    const versions = env.installs.map((i) => `${i.version}@${i.root}`);
    console.log(`[e2e] installs: ${versions.join(' | ')}`);
    const stockOk = env.installs.some((i) => i.version.startsWith(STOCK) || path.resolve(i.root).toLowerCase() === path.resolve(STOCK).toLowerCase());
    assert.ok(stockOk, `no ${STOCK} install`);
    assert.ok(api.live && api.preview, 'live module + preview provider on the API');
    const cmds = await vscode.commands.getCommands(true);
    for (const c of ['kiwrap.openLive', 'kiwrap.live.open', 'kiwrap.live.close', 'kiwrap.configureClaude', 'kiwrap.runErc', 'kiwrap.runDrc', 'kiwrap.live.screenshot']) {
      assert.ok(cmds.includes(c), `command ${c}`);
    }
    report.installs = env.installs.map((i) => ({ version: i.version, root: i.root, input_from_events: i.capabilities?.['gui.input_from_events'] === true }));
    report.server = { state: api.session.serverState, tools: api.session.server?.client.toolNames().length };
  });

  it('2. Configure Claude Code writes .mcp.json, the CLAUDE.md block and the skill copy', async () => {
    const edits = await vscode.commands.executeCommand<Array<{ file: string }>>('kiwrap.configureClaude', { apply: true });
    assert.ok(Array.isArray(edits) && edits.length >= 2, `edits ${JSON.stringify(edits)}`);
    const mcp = JSON.parse(fs.readFileSync(path.join(WS, '.mcp.json'), 'utf8')) as { mcpServers: Record<string, { command: string; args: string[]; env?: Record<string, string> }> };
    const entry = mcp.mcpServers.kiwrap;
    assert.ok(entry, '.mcp.json has mcpServers.kiwrap');
    assert.equal(entry.command, 'uv');
    assert.ok(entry.args.includes('serve') && entry.args.includes('kiwrap'), `args ${entry.args.join(' ')}`);
    assert.ok(!entry.env?.KIWRAP_HOME, 'no KIWRAP_HOME: the agent shares kiwrap\'s default storage home with the extension');
    const claudeMd = fs.readFileSync(path.join(WS, 'CLAUDE.md'), 'utf8');
    assert.ok(/kiwrap/i.test(claudeMd), 'CLAUDE.md carries the managed block');
    assert.ok(fs.existsSync(path.join(WS, '.claude', 'skills', 'kiwrap-flow', 'SKILL.md')), 'skill copied');
    report.configure = { files: edits.map((e) => path.relative(WS, e.file)), mcp: entry };
  });

  it('3. the .kicad_pcb opens in the kiwrap.preview custom editor and renders', async () => {
    const t0 = Date.now();
    await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(PCB), 'kiwrap.preview');
    const r = await waitFor('preview render', () => api.preview.renders.get(PCB.toLowerCase()), 120000, 250);
    assert.ok(!r.error, `render failed: ${r.error}`);
    assert.ok(/via (cli|ipc)/.test(r.info), `render info ${r.info}`);
    await sleep(800); // the webview paints the image
    const shot = captureHost('phase2-preview-pcb');
    report.preview = { info: r.info, ms: Date.now() - t0, shot };
    assert.ok(fs.statSync(shot).size > 1000);
  });

  it('4. Open Live KiCad (stock): the live view of a sandboxed pcbnew streams into the .kicad_pcb tab; Raise KiCad brings the window to the front', async () => {
    const t0 = Date.now();
    const fg0 = probe();
    await vscode.workspace.getConfiguration('kiwrap').update('kicadInstall', STOCK, vscode.ConfigurationTarget.Workspace);
    await vscode.commands.executeCommand('kiwrap.openLive', vscode.Uri.file(PCB));
    const st = await waitFor('live session', () => (api.live.status().pid ? api.live.status() : null), 240000, 250);
    kicadPid = st.pid!;
    helperPid = st.helper?.pid ?? 0;
    const tLaunched = Date.now() - t0;
    const fg1 = probe();
    const owner = await ownerStatus();
    assert.notEqual(fg1.foreground.pid, kicadPid, `KiCad holds the foreground after launch: ${JSON.stringify(fg1.foreground)} (before: ${JSON.stringify(fg0.foreground)}; supervisor: ${JSON.stringify(owner.foreground)})`);
    assert.equal(st.mode, 'view', `the live view, got ${st.mode}`);
    assert.equal(owner.mode_used, 'view', `kw_live resolved mode_used ${owner.mode_used}`);
    assert.ok(st.helper && st.helper.port > 0, `helper ${JSON.stringify(st.helper)}`);
    const pcb = await waitFor('pcb surface', () => api.live.sessions.surfaces.find('pcb'), 30000);
    assert.ok(!pcb.cloaked, 'the live view: not cloaked');
    pcbView = await waitFor('kiwrap.live editor for the board', () => api.live.editors.viewFor(PCB), 30000);
    pcbView.panel.reveal(undefined, false);
    const s = await waitFor('sized frames in the tab', async () => {
      const x = await snap(pcbView);
      return x.connected && x.stats.frames >= 1 && x.surface === pcb.id && x.frameW > 400 && x.frameH > 300 ? x : null;
    }, 90000, 250) as Snapshot & { hudMode?: string; raiseVisible?: boolean };
    const tFrame = Date.now() - t0;
    assert.ok(s.frameW > 400 && s.frameH > 300, `frame ${s.frameW}x${s.frameH}`);
    assert.equal(s.mode, 'view');
    assert.equal(s.hudMode, 'live view', 'the HUD names the live view');
    assert.equal(s.raiseVisible, true, 'the Raise KiCad button is there');
    const helperShot = await saveHelperShot(api, pcb.id, 'phase2-live-pcb-helper');
    await sleep(500);
    const hostShot = captureHost('phase2-live-pcb-tab');
    // Raise KiCad (the tab's button, then a double-click on the picture): the real window takes the
    // foreground (GetForegroundWindow), the way a person takes over; the dev host is brought back
    const raiseButton = await raiseAndVerify(pcbView, kicadPid, 'button');
    console.log(`[e2e] raise (button): KiCad foreground=${raiseButton.kicad_foreground} in ${raiseButton.ms} ms, host back=${raiseButton.host_back}`);
    const raiseDbl = await raiseAndVerify(pcbView, kicadPid, 'dblclick');
    console.log(`[e2e] raise (double-click): KiCad foreground=${raiseDbl.kicad_foreground} in ${raiseDbl.ms} ms, host back=${raiseDbl.host_back}`);
    report.raise = { button: raiseButton, dblclick: raiseDbl };
    assert.ok(raiseButton.kicad_foreground, `Raise KiCad (button) did not bring KiCad to the front: ${JSON.stringify(raiseButton.foreground_after)}`);
    assert.ok(raiseDbl.kicad_foreground, `a double-click did not bring KiCad to the front: ${JSON.stringify(raiseDbl.foreground_after)}`);
    pcbView.panel.reveal(undefined, false);
    // the core model mirrors the live state (tree / status bar)
    assert.equal(api.session.live.kind, 'running');
    // the agent's server (a second process) sees the session and the registered helper
    const agent = await runAgent(['live-status']);
    const done = agent.lines.find((l) => l.event === 'done');
    assert.ok(done, `agent live-status: ${agent.stderr.slice(-800)}`);
    assert.equal(done.pid, kicadPid, 'the agent finds the session by project (session record)');
    assert.equal(done.remote, true);
    assert.ok(done.helper && (done.helper as { port: number }).port === st.helper!.port, `helper registered with kw_live attach: ${JSON.stringify(done.helper)}`);
    report.live_pcb = {
      pid: kicadPid, helper: st.helper, mode: st.mode, surface: { id: pcb.id, title: pcb.title, rect_px: pcb.rect_px, dpi: pcb.dpi, cloaked: pcb.cloaked },
      launch_to_session_ms: tLaunched, launch_to_first_frame_ms: tFrame, frame: { w: s.frameW, h: s.frameH, scale: s.layout.scale, dpr: s.dpr },
      foreground_before_launch: fg0.foreground, foreground_after_launch: fg1.foreground, supervisor: owner,
      helper_screenshot: helperShot, host_screenshot: hostShot, agent_status: done,
    };
    console.log(`[e2e] live pcb: pid ${kicadPid}, helper :${st.helper!.port}, session ${tLaunched} ms, first frame ${tFrame} ms, frame ${s.frameW}x${s.frameH}`);
    // finding 4: kw_live screenshot from the AGENT's server goes through the registered helper
    const shotOut = path.join(OUT, 'agent-live-screenshot.png');
    const agentShot = await runAgent(['live-screenshot', '--out', shotOut, '--max-px', '512']);
    const shotDone = agentShot.lines.find((l) => l.event === 'done');
    assert.ok(shotDone, `agent live-screenshot: ${agentShot.stderr.slice(-800)}`);
    assert.equal(shotDone.source, 'helper', `the agent's kw_live screenshot came from ${shotDone.source}, not the helper (${JSON.stringify(shotDone)})`);
    assert.ok(fs.existsSync(shotOut) && fs.statSync(shotOut).size > 1000, 'a PNG came back');
    report.agent_screenshot = shotDone;
    // finding 7: the tab's Start button names the project by DIRECTORY; kiwrap.openLive by .kicad_pro:
    // both mean this session (no relaunch)
    await vscode.commands.executeCommand('kiwrap.live.open', { project: PROJECT, editor: 'pcb', file: PCB });
    assert.equal(api.live.status().pid, kicadPid, 'the same project named by its directory reuses the session');
    await vscode.commands.executeCommand('kiwrap.live.open', { project: PRO, editor: 'pcb', file: PCB });
    assert.equal(api.live.status().pid, kicadPid, 'the same project named by its .kicad_pro reuses the session');
    // finding 8: the schematic tab while a pcb session is live never silently replaces the session
    api.live.sessions.confirmReplace = async () => false;
    await vscode.commands.executeCommand('kiwrap.openLive', vscode.Uri.file(SCH));
    assert.equal(api.live.status().pid, kicadPid, 'declining the replacement keeps the pcb session');
    const schView = await waitFor('kiwrap.live editor for the schematic', () => api.live.editors.viewFor(SCH), 30000);
    schView.panel.reveal(undefined, false);
    const schSnap = await waitFor('sch card', async () => { const x = await snap(schView); return x.cardVisible ? x : null; }, 15000, 250);
    assert.equal(schSnap.surface, null, 'the sch tab shows its card, not a pcb surface');
    pcbView.panel.reveal(undefined, false);
    report.session_identity = { reuse_by_dir: true, reuse_by_pro: true, sch_replace_declined: true };
  });

  it('5. an agent (second MCP client) kw_pcb_place moves the footprint in the live tab within ~1 s', async () => {
    pcbView.panel.reveal(undefined, false);
    const pcb = api.live.sessions.surfaces.find('pcb')!;
    const before = await saveHelperShot(api, pcb.id, 'phase2-place-before');
    const idle0 = (await snap(pcbView)).stats.lastFrameId;
    await sleep(1500);
    const idle1 = (await snap(pcbView)).stats.lastFrameId;
    let placeStartT = 0;
    let placeEndT = 0;
    let frameT = 0;
    let framesBefore = idle1;
    const watcher = (async () => {
      await waitFor('place-start', () => placeStartT > 0, 120000, 20);
      framesBefore = (await snap(pcbView)).stats.lastFrameId;
      await waitFor('frame after the agent\'s commit', async () => (await snap(pcbView)).stats.lastFrameId > framesBefore, 30000, 25);
      frameT = Date.now();
    })();
    const agent = await runAgent(['place', '--ref', 'C1', '--dx', '5'], (l) => {
      if (l.event === 'place-start') {
        placeStartT = l.t;
      }
      if (l.event === 'place-end') {
        placeEndT = l.t;
      }
    });
    await watcher;
    const done = agent.lines.find((l) => l.event === 'done');
    assert.ok(done, `agent place failed: ${JSON.stringify(agent.lines.slice(-2))} ${agent.stderr.slice(-800)}`);
    assert.ok((done.provider as string).startsWith('ipc11'), `provider ${done.provider}: the agent's write went through the bound GUI`);
    const to = done.to as number[];
    const after = done.after as number[];
    assert.ok(Math.abs(after[0] - to[0]) < 1e-3 && Math.abs(after[1] - to[1]) < 1e-3, `read back ${after} vs ${to}`);
    const placeMs = (agent.lines.find((l) => l.event === 'result' && l.tool === 'kw_pcb_place')?.ms as number) ?? -1;
    const startToFrame = frameT - placeStartT;
    const endToFrame = frameT - placeEndT;
    await sleep(600);
    const afterShot = await saveHelperShot(api, pcb.id, 'phase2-place-after');
    assert.ok(!fs.readFileSync(before.file).equals(fs.readFileSync(afterShot.file)), 'the live picture changed');
    const hostShot = captureHost('phase2-place-after-tab');
    report.agent_place = {
      ref: 'C1', before: done.before, to, after, provider: done.provider, read_provider: done.read_provider, kw_pcb_place_ms: placeMs,
      tool_call_to_tab_frame_ms: startToFrame, tool_return_to_tab_frame_ms: endToFrame, idle_frames_1500ms: idle1 - idle0,
      screenshots: { before: before.file, after: afterShot.file, tab: hostShot }, agent_lines: agent.lines,
    };
    console.log(`[e2e] agent place: kw_pcb_place ${placeMs} ms via ${done.provider}; call->frame ${startToFrame} ms; return->frame ${endToFrame} ms`);
    assert.ok(startToFrame < 3000, `the tab repainted ${startToFrame} ms after the agent's call`);
  });

  it('5b. the keybinding shield is disarmed in the live view: VS Code keys stay VS Code keys (D20)', async function () {
    // kiwrap.live.inputMode is none (the product path), so the 178-chord shield is not bound
    // (context key kiwrap.live.input is false): Ctrl+N opens an untitled editor with the Explorer
    // focused AND with the live tab focused. Real keys (SendKeys), so the dev host must be
    // foreground: skipped, not failed, when the OS refuses that.
    assert.equal(api.live.sessions.inputActive, false, 'no input mode injects in the live view');
    pcbView.panel.reveal(undefined, false);
    await vscode.commands.executeCommand('workbench.view.explorer');
    await sleep(500);
    const before = untitledEditors();
    const r1 = sendKeys('^n');
    if (!r1.sent) { report.shield = { skipped: r1 }; this.skip(); return; }
    await sleep(800);
    const withSidebar = untitledEditors() - before;
    await vscode.commands.executeCommand('workbench.action.closeAllEditors').then(undefined, () => undefined);
    await sleep(300);
    await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(PCB), 'kiwrap.live');
    pcbView = await waitFor('kiwrap.live editor for the board', () => api.live.editors.viewFor(PCB), 30000);
    pcbView.panel.reveal(undefined, true);
    await sleep(600);
    const before2 = untitledEditors();
    const r2 = sendKeys('^n');
    await sleep(800);
    const withTab = untitledEditors() - before2;
    report.shield = { input_mode: 'none', input_active: api.live.sessions.inputActive, sidebar_focus_opened: withSidebar, live_tab_focus_opened: withTab, r1, r2 };
    console.log(`[e2e] shield (live view, disarmed): Ctrl+N opened ${withSidebar} editor(s) with the sidebar focused, ${withTab} with the live tab focused`);
    await vscode.commands.executeCommand('workbench.action.closeAllEditors').then(undefined, () => undefined);
    await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(PCB), 'kiwrap.live');
    pcbView = await waitFor('kiwrap.live editor for the board', () => api.live.editors.viewFor(PCB), 30000);
    assert.equal(withSidebar, 1, 'Ctrl+N reaches VS Code while the Explorer has the focus');
    if (r2.sent) { assert.equal(withTab, 1, 'Ctrl+N reaches VS Code while the live tab has the focus (no shield in the live view)'); }
  });

  it('6. Run ERC / DRC while the session is live: Problems panel entries with locations', async () => {
    await vscode.commands.executeCommand('kiwrap.runErc', vscode.Uri.file(PCB));
    await vscode.commands.executeCommand('kiwrap.runDrc', vscode.Uri.file(PCB));
    const diags: Array<{ uri: vscode.Uri; diag: vscode.Diagnostic }> = [];
    for (const [uri, list] of vscode.languages.getDiagnostics()) {
      for (const diag of list) {
        if (diag.source?.startsWith('kiwrap')) {
          diags.push({ uri, diag });
        }
      }
    }
    const erc = api.session.lastErc;
    const drc = api.session.lastDrc;
    assert.ok(erc && drc, `ERC ${JSON.stringify(erc)} DRC ${JSON.stringify(drc)}`);
    assert.ok(diags.length > 0, 'the Problems panel has kiwrap entries');
    for (const d of diags) {
      assert.ok(d.uri.fsPath.toLowerCase().startsWith(PROJECT.toLowerCase()), `diagnostic on ${d.uri.fsPath}`);
      assert.ok(d.diag.range.start.line >= 0, 'a location');
    }
    await vscode.commands.executeCommand('workbench.actions.view.problems');
    await sleep(1200);
    const shot = captureHost('phase2-problems');
    report.checks = {
      erc: erc.numbers, drc: drc.numbers, erc_provider: erc.provider, drc_provider: drc.provider,
      diagnostics: diags.slice(0, 40).map((d) => ({ file: path.basename(d.uri.fsPath), line: d.diag.range.start.line + 1, code: String(typeof d.diag.code === 'object' ? (d.diag.code as { value: string }).value : d.diag.code), severity: vscode.DiagnosticSeverity[d.diag.severity], message: d.diag.message.slice(0, 120) })),
      count: diags.length, shot,
    };
    console.log(`[e2e] ERC ${JSON.stringify(erc.numbers)} DRC ${JSON.stringify(drc.numbers)} -> ${diags.length} diagnostics`);
    assert.ok(api.live.status().pid === kicadPid && pidAlive(kicadPid), 'the live session survived the checks');
  });

  it('7. close the live session: helper and KiCad gone, lock files removed', async () => {
    await vscode.commands.executeCommand('kiwrap.closeLive');
    await waitFor('KiCad exited', () => !pidAlive(kicadPid), 20000, 200);
    if (helperPid) {
      await waitFor('helper exited', () => !pidAlive(helperPid), 20000, 200);
    }
    assert.equal(api.live.status().pid, null);
    await waitFor('lock files removed', () => fs.readdirSync(PROJECT).filter((f) => f.startsWith('~') && f.endsWith('.lck')).length === 0, 10000, 200);
    assert.equal(api.session.live.kind, 'idle');
    report.close_pcb = { ok: true };
  });

  it('7b. checkOnSave: one "Run DRC" is one DRC, not an endless loop (finding 1)', async function () {
    this.timeout(180000);
    await vscode.workspace.getConfiguration('kiwrap').update('checkOnSave', true, vscode.ConfigurationTarget.Workspace);
    await sleep(300);
    const auto0 = api.diagnostics.autoRuns.drc;
    const t0 = Date.now();
    await vscode.commands.executeCommand('kiwrap.runDrc', vscode.Uri.file(PCB));
    const first = api.session.lastDrc?.at.getTime() ?? 0;
    assert.ok(first > 0, 'the DRC ran');
    const runMs = Date.now() - t0;
    // no live session now: kw_pcb_drc refills the zone and saves the board (stock 10.99), so the
    // **/*.kicad_pcb watcher sees the rewrite - the self-write guard must recognise it as ours
    const boardMtime0 = fs.statSync(PCB).mtimeMs;
    await sleep(Math.max(12000, runMs * 3));
    const runs = api.diagnostics.autoRuns.drc - auto0;
    const second = api.session.lastDrc?.at.getTime() ?? 0;
    const boardMtime1 = fs.statSync(PCB).mtimeMs;
    await vscode.workspace.getConfiguration('kiwrap').update('checkOnSave', false, vscode.ConfigurationTarget.Workspace);
    report.check_on_save = { drc_ms: runMs, auto_runs_after: runs, lastDrc_changed: second !== first, board_rewritten_after: boardMtime1 !== boardMtime0, busy: [...api.session.busy] };
    console.log(`[e2e] checkOnSave: ${runs} automatic DRC run(s) after one Run DRC (${runMs} ms), lastDrc changed=${second !== first}`);
    assert.equal(runs, 0, 'no automatic DRC followed the command (self-write guard)');
    assert.equal(second, first, 'exactly one DRC ran');
    assert.ok(!api.session.busy.has('drc'), 'no DRC in flight');
  });

  it('8. the .kicad_sch tab: sandboxed eeschema streams into the kiwrap.live editor', async () => {
    const t0 = Date.now();
    await vscode.commands.executeCommand('kiwrap.openLive', vscode.Uri.file(SCH));
    const st = await waitFor('sch live session', () => (api.live.status().pid ? api.live.status() : null), 240000, 250);
    const sch = await waitFor('sch surface', () => api.live.sessions.surfaces.find('sch'), 60000);
    const schView = await waitFor('kiwrap.live editor for the schematic', () => api.live.editors.viewFor(SCH), 30000);
    schView.panel.reveal(undefined, false);
    const s = await waitFor('sized sch frames', async () => {
      const x = await snap(schView);
      return x.connected && x.stats.frames >= 1 && x.surface === sch.id && x.frameW > 400 && x.frameH > 300 ? x : null;
    }, 90000, 250);
    assert.equal(s.mode, 'view');
    const helperShot = await saveHelperShot(api, sch.id, 'phase2-live-sch-helper');
    await sleep(400);
    const hostShot = captureHost('phase2-live-sch-tab');
    const raise = await raiseAndVerify(schView, st.pid!, 'button');
    console.log(`[e2e] raise (sch, button): KiCad foreground=${raise.kicad_foreground} in ${raise.ms} ms`);
    report.live_sch = { pid: st.pid, mode: st.mode, surface: { id: sch.id, title: sch.title, rect_px: sch.rect_px }, launch_to_first_frame_ms: Date.now() - t0, frame: { w: s.frameW, h: s.frameH }, helper_screenshot: helperShot, host_screenshot: hostShot, raise };
    assert.ok(raise.kicad_foreground, `Raise KiCad from the schematic tab did not bring eeschema to the front: ${JSON.stringify(raise.foreground_after)}`);
    const pid = st.pid!;
    await vscode.commands.executeCommand('kiwrap.closeLive');
    await waitFor('eeschema exited', () => !pidAlive(pid), 20000, 200);
    await waitFor('lock files removed', () => fs.readdirSync(PROJECT).filter((f) => f.startsWith('~') && f.endsWith('.lck')).length === 0, 10000, 200);
  });

  it('8b. the live view on the second stock install (10.0.x): pcbnew streams, Raise KiCad works', async function () {
    const env = api.session.env!;
    const install = STOCK2 && STOCK2 !== 'skip' ? env.installs.find((i) => i.version.startsWith(STOCK2) || path.resolve(i.root).toLowerCase() === path.resolve(STOCK2).toLowerCase()) : undefined;
    if (!install) {
      report.live_pcb_stock2 = { skipped: `no ${STOCK2 || '(none)'} install` };
      this.skip();
      return;
    }
    // the ecc83 copy has been re-saved in the 10.99 format by the earlier steps (kw_pcb_place's file
    // writer, the DRC refill), which a 10.0.x refuses: open the 10.0-format blinker board instead
    const project = fs.existsSync(BLINKER_PCB) ? BLINKER_PROJECT : PROJECT;
    const board = fs.existsSync(BLINKER_PCB) ? BLINKER_PCB : PCB;
    const t0 = Date.now();
    await vscode.commands.executeCommand('kiwrap.live.open', { project, editor: 'pcb', file: board, target_kicad: install.root });
    const st = await waitFor('live session (stock 2)', () => (api.live.status().pid ? api.live.status() : null), 240000, 250);
    const pid = st.pid!;
    const owner = await vscode.commands.executeCommand<{ data: Record<string, unknown> }>('kiwrap.core.callTool', 'kw_live', { action: 'status', project }).then((r) => ({ mode_used: r?.data?.mode_used as string, cloaked: r?.data?.cloaked as boolean, foreground: { taken: r?.data?.foreground_taken as boolean, restored: r?.data?.foreground_restored as boolean | null }, ready_after_s: r?.data?.ready_after_s as number }));
    assert.equal(st.mode, 'view');
    assert.equal(owner.mode_used, 'view');
    const pcb = await waitFor('pcb surface (stock 2)', () => api.live.sessions.surfaces.find('pcb'), 30000);
    const view = await waitFor('kiwrap.live editor for the board', () => api.live.editors.viewFor(board), 30000);
    view.panel.reveal(undefined, false);
    // the predicate waits for a sized frame: the first frames of a starting editor can be 0x0
    // (the window is still being laid out) and a surface re-bind resets the page's size
    const s = await waitFor('sized frames (stock 2)', async () => {
      const x = await snap(view);
      return x.connected && x.stats.frames >= 1 && x.surface === pcb.id && x.frameW > 400 && x.frameH > 300 ? x : null;
    }, 90000, 250);
    const tFrame = Date.now() - t0;
    const helperShot = await saveHelperShot(api, pcb.id, 'phase2-live-pcb-1000-helper');
    await sleep(400);
    const hostShot = captureHost('phase2-live-pcb-1000-tab');
    const raise = await raiseAndVerify(view, pid, 'button');
    console.log(`[e2e] stock 2 (${install.version}): first frame ${tFrame} ms, frame ${s.frameW}x${s.frameH}, raise foreground=${raise.kicad_foreground} in ${raise.ms} ms`);
    const dialogs = api.live.sessions.surfaces.get(pcb.id)?.dialogs ?? [];
    report.live_pcb_stock2 = { install: { version: install.version, root: install.root }, project, pid, mode: st.mode, supervisor: owner, launch_to_first_frame_ms: tFrame, frame: { w: s.frameW, h: s.frameH }, surface_title: pcb.title, dialogs: dialogs.map((d) => d.title), helper_screenshot: helperShot, host_screenshot: hostShot, raise };
    await vscode.commands.executeCommand('kiwrap.closeLive');
    await waitFor('KiCad (stock 2) exited', () => !pidAlive(pid), 20000, 200);
    await waitFor('lock files removed', () => fs.readdirSync(project).filter((f) => f.startsWith('~') && f.endsWith('.lck')).length === 0, 10000, 200);
    assert.ok(s.frameW > 400 && s.frameH > 300, `frame ${s.frameW}x${s.frameH}`);
    assert.equal(dialogs.length, 0, `the ${install.version} editor opened the board without a dialog (got ${JSON.stringify(dialogs.map((d) => d.title))})`);
    assert.ok(raise.kicad_foreground && !/error/i.test(raise.foreground_after.title), `Raise KiCad did not bring the ${install.version} PCB editor to the front: ${JSON.stringify(raise.foreground_after)}`);
  });

  it('9. [opt-in KIWRAP_E2E_HIDDEN] the experimental cloaked mode on a display-input build: input from the tab, cursor untouched, shield armed', async function () {
    const env = api.session.env!;
    const optIn = HIDDEN !== '' && HIDDEN !== 'skip' && HIDDEN !== '0';
    const hiddenRoot = !optIn ? '' : HIDDEN === '1' || HIDDEN === 'true' ? env.installs.find((i) => i.capabilities?.['gui.input_from_events'] === true)?.root || '' : HIDDEN;
    if (!hiddenRoot) {
      report.hidden = { skipped: optIn ? 'no install with gui.input_from_events' : 'opt-in: set KIWRAP_E2E_HIDDEN=1 (experimental, parked under D20)' };
      this.skip();
      return;
    }
    const t0 = Date.now();
    const fg0 = probe();
    await vscode.commands.executeCommand('kiwrap.live.open', { project: PRO, editor: 'pcb', file: PCB, inputMode: 'events', target_kicad: hiddenRoot });
    const st = await waitFor('hidden session', () => (api.live.status().pid ? api.live.status() : null), 240000, 250);
    const pid = st.pid!;
    assert.equal(st.mode, 'hidden', `expected hidden, got ${st.mode}`);
    assert.equal(api.live.sessions.inputActive, true, 'the events injector arms the shield');
    const owner = await ownerStatus();
    const fg1 = probe();
    console.log(`[e2e] hidden launch: foreground before ${JSON.stringify(fg0.foreground)} after ${JSON.stringify(fg1.foreground)} supervisor ${JSON.stringify(owner.foreground)} ready ${owner.ready_after_s}s`);
    const pcb = await waitFor('cloaked pcb surface', () => { const s = api.live.sessions.surfaces.find('pcb'); return s && s.cloaked ? s : null; }, 30000);
    pcbView = await waitFor('kiwrap.live editor for the board', () => api.live.editors.viewFor(PCB), 30000);
    pcbView.panel.reveal(undefined, false);
    const s0 = await waitFor('frames from the cloaked window', async () => {
      const x = await snap(pcbView);
      return x.connected && x.stats.frames >= 1 && x.surface === pcb.id ? x : null;
    }, 60000, 250);
    assert.equal(s0.mode, 'hidden');
    // nothing of KiCad on the desktop: WindowFromPoint over the frame's rect never hits KiCad's pid
    const desktop = probe(pcb.rect_px);
    const hitsKicad = (desktop.window_from_point ?? []).filter((h) => h.pid === pid);
    assert.equal(hitsKicad.length, 0, `desktop shows KiCad at ${JSON.stringify(hitsKicad)}`);
    const desktopShot = captureRect('phase2-hidden-desktop', pcb.rect_px);
    const helperShot = await saveHelperShot(api, pcb.id, 'phase2-hidden-helper');
    const hostShot = captureHost('phase2-hidden-tab');
    const cursorBefore = desktop.cursor;
    const fgBefore = desktop.foreground;

    // --- input from the tab (docs/DISPLAY.md 12.5: PostMessage only; the helper's events injector)
    const r = (await snap(pcbView)).canvasRect;
    const cx = r.left + r.width * 0.5, cy = r.top + r.height * 0.5;
    const timings: Record<string, number> = {};
    let closed = false;
    const closeHidden = async () => {
      if (!closed) {
        closed = true;
        await vscode.commands.executeCommand('kiwrap.closeLive');
        await waitFor('hidden KiCad exited', () => !pidAlive(pid), 20000, 200);
      }
    };
    try {
    // warm-up: Esc, a click on the canvas (wx moves the focus onto it inside the cloaked frame)
    key(pcbView, 'Escape', 'Escape');
    click(pcbView, cx, cy);
    await sleep(300);
    // the board opens zoomed to fit, so Home alone repaints nothing: zoom in with the wheel first
    timings.wheel_ms = await inputThenFrame(pcbView, 'wheel zoom', () => pcbView.testDispatch({ kind: 'wheel', name: 'wheel', init: { clientX: cx, clientY: cy, deltaX: 0, deltaY: -120, deltaMode: 0 } }), 10000);
    await sleep(300);
    timings.wheel2_ms = await inputThenFrame(pcbView, 'wheel zoom 2', () => pcbView.testDispatch({ kind: 'wheel', name: 'wheel', init: { clientX: cx, clientY: cy, deltaX: 0, deltaY: -120, deltaMode: 0 } }));
    await sleep(300);
    timings.home_ms = await inputThenFrame(pcbView, 'Home (zoom fit)', () => key(pcbView, 'Home', 'Home'));
    // select all + rotate: read back over IPC through the agent (the live document, unsaved)
    const getBefore = await runAgent(['get']);
    const fpsBefore = (getBefore.lines.find((l) => l.event === 'done')?.footprints ?? []) as Array<{ ref: string; rotation_deg: number }>;
    assert.ok(fpsBefore.length > 5, `footprints before: ${fpsBefore.length}`);
    await sleep(300);
    click(pcbView, cx, cy);
    await sleep(200);
    timings.select_all_ms = await inputThenFrame(pcbView, 'Ctrl+A', () => key(pcbView, 'KeyA', 'a', { ctrlKey: true }));
    await sleep(300);
    timings.rotate_ms = await inputThenFrame(pcbView, 'R (rotate selection)', () => key(pcbView, 'KeyR', 'r'));
    await sleep(500);
    const getAfter = await runAgent(['get']);
    const fpsAfter = (getAfter.lines.find((l) => l.event === 'done')?.footprints ?? []) as Array<{ ref: string; rotation_deg: number }>;
    const rotated = fpsBefore.filter((b) => {
      const a = fpsAfter.find((x) => x.ref === b.ref);
      return a && Math.abs((((a.rotation_deg - b.rotation_deg) % 360) + 360) % 360) > 1;
    });
    console.log(`[e2e] hidden: ${rotated.length}/${fpsBefore.length} footprints rotated by the posted Ctrl+A, R (read back over ipc11)`);
    timings.undo_ms = await inputThenFrame(pcbView, 'Ctrl+Z', () => key(pcbView, 'KeyZ', 'z', { ctrlKey: true }));
    await sleep(500);
    const getUndo = await runAgent(['get']);
    const fpsUndo = (getUndo.lines.find((l) => l.event === 'done')?.footprints ?? []) as Array<{ ref: string; rotation_deg: number }>;
    const restored = fpsBefore.every((b) => { const u = fpsUndo.find((x) => x.ref === b.ref); return u && Math.abs(u.rotation_deg - b.rotation_deg) < 1e-3; });
    // a dialog: Escape (deselect), click, then 'A' (add footprint) opens the chooser, composited on the surface
    key(pcbView, 'Escape', 'Escape');
    click(pcbView, cx, cy);
    await sleep(200);
    key(pcbView, 'KeyA', 'a');
    const dialog = await waitFor('a dialog on the surface', () => { const s = api.live.sessions.surfaces.get(pcb.id); return s && s.dialogs.length ? s.dialogs : null; }, 40000, 200).catch(() => null);
    let dialogShot: string | undefined;
    if (dialog) {
      await sleep(1200);
      dialogShot = (await saveHelperShot(api, pcb.id, 'phase2-hidden-dialog')).file;
      await api.live.sessions.closeDialog(pcb.id).catch(() => undefined);
      await waitFor('dialog closed', () => { const s = api.live.sessions.surfaces.get(pcb.id); return s && s.dialogs.length === 0; }, 15000, 200).catch(() => undefined);
      for (let i = 0; i < 3; i++) {
        key(pcbView, 'Escape', 'Escape');
        await sleep(150);
      }
    }
    const after = probe(pcb.rect_px);
    const sEnd = await snap(pcbView);
    report.hidden = {
      install: hiddenRoot, pid, helper: st.helper, surface: { id: pcb.id, title: pcb.title, rect_px: pcb.rect_px, cloaked: pcb.cloaked },
      supervisor: owner, foreground_before_launch: fg0.foreground, foreground_after_launch: fg1.foreground,
      launch_to_first_frame_ms: Date.now() - t0, desktop_probe: desktop, input_timings_ms: timings,
      rotated_footprints: rotated.map((x) => x.ref), footprints: fpsBefore.length, undo_restored: restored,
      dialog: dialog ? dialog.map((d) => d.title) : null, cursor_before: cursorBefore, cursor_after: after.cursor, foreground_before: fgBefore, foreground_after: after.foreground,
      frames_total: sEnd.stats.frames, input_to_frame_hud_ms: sEnd.stats.inputToFrameMs,
      screenshots: { desktop_rect: desktopShot, helper: helperShot.file, tab: hostShot, dialog: dialogShot },
    };
    assert.deepEqual(after.cursor, cursorBefore, `the user's cursor moved: ${cursorBefore} -> ${after.cursor}`);
    assert.equal(after.foreground.pid, fgBefore.pid, `foreground changed: ${JSON.stringify(fgBefore)} -> ${JSON.stringify(after.foreground)}`);
    assert.notEqual(fg1.foreground.pid, pid, `the cloaked KiCad holds the foreground after launch: ${JSON.stringify(fg1.foreground)} (supervisor ${JSON.stringify(owner.foreground)})`);
    assert.ok((after.window_from_point ?? []).every((h) => h.pid !== pid), 'still nothing of KiCad on the desktop');
    assert.ok(rotated.length > 0, 'posted Ctrl+A + R rotated footprints in the cloaked KiCad (read back over IPC)');
    assert.ok(restored, 'Ctrl+Z restored the rotations');
    assert.ok(dialog, 'the A hotkey opened a dialog composited on the cloaked surface');
    } finally {
      await closeHidden();
    }
  });

  it('10. nothing left: no KiCad / helper processes of this run, project lock files gone', async () => {
    await sleep(1000);
    const left = processes(['kicad', 'pcbnew', 'eeschema', 'kiwrap-live']);
    report.processes_left = left;
    assert.equal(left.length, 0, `processes left: ${JSON.stringify(left)}`);
    assert.equal(fs.readdirSync(PROJECT).filter((f) => f.startsWith('~') && f.endsWith('.lck')).length, 0, 'no ~*.lck left');
  });
});
