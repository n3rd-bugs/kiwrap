/*
 * Mocha unit tests of the vscode-free parts of the Live KiCad module: helper process (hello line, argv,
 * resolution, a real spawn of the fake helper incl. restart after a crash), /control client (id and
 * FIFO matching, surface pushes), surfaces model, keybinding shield generation, mode decision and
 * kw_live result unwrapping. Run: npm run --prefix extension test:live-unit (see test/live/README.md).
 */
import * as assert from 'assert';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { HelperProcess, helperArgs, parseHelloLine, resolveHelper, resolveHelperPath } from '../../../src/live/helperProcess';
import { ControlClient, controlUrl, replyError, type WebSocketLike } from '../../../src/live/controlClient';
import { SurfacesModel, editorForFile, kindForFile, normalizeSurface, projectDirFor, projectKey, surfaceKindFor, tabShapeFor } from '../../../src/live/surfaces';
import { FOCUS_NEGATIVES, PALETTE_CHORDS, SHIELD_CHORDS, generateKeybindings, whenClause } from '../../../src/live/keybindings';
import { inputModeOf, launchModeFor, resolveInject } from '../../../src/live/mode';
import { asSession, sessionSupportsEvents, unwrapToolResult } from '../../../src/live/kwLive';
import { LIVE_COMMANDS, LIVE_EDITOR_VIEW_TYPE, LIVE_PANEL_VIEW_TYPE, type Surface } from '../../../src/shared/types';

const EXT = path.resolve(__dirname, '..', '..', '..', '..', '..', '..');   // test/live/.out/test/live/unit -> extension/
const FAKE_HELPER = path.join(EXT, 'test', 'live', 'fake_helper', 'fake_helper.mjs');
const NODE = process.execPath;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const surf = (id: string, kind: Surface['kind'], title = id): Surface => ({ id, kind, title, hwnd: 1, rect_px: { x: 0, y: 0, w: 10, h: 10 }, dpi: 96, cloaked: false, dialogs: [] });

describe('helperProcess', () => {
  it('parses the hello line and rejects junk', () => {
    assert.deepStrictEqual(parseHelloLine('{"port":5123,"token":"abc","version":"0.1.0"}'), { port: 5123, token: 'abc', version: '0.1.0' });
    assert.throws(() => parseHelloLine('hello'), /not JSON/);
    assert.throws(() => parseHelloLine('{"token":"x"}'), /port/);
    assert.throws(() => parseHelloLine('{"port":80}'), /token/);
    assert.strictEqual(parseHelloLine('{"port":1,"token":"t"}').version, '');
  });

  it('builds the contract argv in order: no token on the command line (the helper mints it)', () => {
    assert.deepStrictEqual(helperArgs({ pid: 4242, inject: 'none', cloakExpected: false }), ['--pid', '4242', '--listen', '127.0.0.1:0', '--inject', 'none']);
    assert.deepStrictEqual(helperArgs({ pid: 1, inject: 'events', cloakExpected: true, fitToView: true, extraArgs: ['--x'] }), ['--pid', '1', '--listen', '127.0.0.1:0', '--inject', 'events', '--cloak-expected', '--fit-to-view', '--x']);
    assert.deepStrictEqual(helperArgs({ pid: 7, inject: 'hybrid', cloakExpected: false, allowAnyProcess: true }), ['--pid', '7', '--listen', '127.0.0.1:0', '--inject', 'hybrid', '--allow-any-process']);
    for (const a of helperArgs({ pid: 1, inject: 'mirror', cloakExpected: false, fitToView: true })) { assert.ok(!a.includes('--token'), 'finding 11: no secret in argv'); }
  });

  it('resolves the helper: setting first, then <ext>/bin, then the repo dev build', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kiwrap-live-'));
    try {
      const ext = path.join(root, 'extension'); fs.mkdirSync(path.join(ext, 'bin'), { recursive: true });
      assert.strictEqual(resolveHelperPath('', ext), null);
      fs.mkdirSync(path.join(root, 'helper', 'target', 'release'), { recursive: true });
      fs.writeFileSync(path.join(root, 'helper', 'target', 'release', 'kiwrap-live.exe'), '');
      assert.strictEqual(resolveHelperPath(undefined, ext), path.join(ext, '..', 'helper', 'target', 'release', 'kiwrap-live.exe'));
      fs.writeFileSync(path.join(ext, 'bin', 'kiwrap-live.exe'), '');
      assert.strictEqual(resolveHelperPath('', ext), path.join(ext, 'bin', 'kiwrap-live.exe'));
      const custom = path.join(root, 'custom.exe'); fs.writeFileSync(custom, '');
      assert.strictEqual(resolveHelperPath(custom, ext), custom);
      assert.strictEqual(resolveHelperPath(path.join(root, 'missing.exe'), ext), path.join(ext, 'bin', 'kiwrap-live.exe'));
      // finding 20: a configured path that is not a file is reported, not silently ignored
      const r = resolveHelper(path.join(root, 'missing.exe'), ext);
      assert.strictEqual(r.path, path.join(ext, 'bin', 'kiwrap-live.exe'));
      assert.strictEqual(r.ignoredSetting, path.join(root, 'missing.exe'));
      assert.strictEqual(resolveHelper(custom, ext).ignoredSetting, undefined);
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
  });

  it('spawns the fake helper, reads the hello line, restarts it after a crash, and kills it on dispose', async function () {
    this.timeout(20000);
    const exits: Array<{ code: number | null; willRestart: boolean }> = [];
    const hellos: number[] = [];
    const logs: string[] = [];
    const hp = new HelperProcess({ helperPath: FAKE_HELPER, pid: process.pid, inject: 'none', cloakExpected: false, nodePath: NODE, extraArgs: ['--token', 'unit-token'], env: { KIWRAP_FAKE_CRASH_AFTER_MS: '400', KIWRAP_FAKE_3D_DELAY_MS: '0' } }, {
      onExit: (code, _s, willRestart) => exits.push({ code, willRestart }),
      onHello: (h) => hellos.push(h.port),
      onLog: (l) => logs.push(l),
    });
    const hello = await hp.start().catch((e: Error) => { throw new Error(e.message + ' | ' + logs.join(' | ')); });
    assert.ok(hello.port > 0); assert.strictEqual(hello.token, 'unit-token'); assert.match(hello.version, /fake/);
    assert.ok(hp.running);
    await sleep(1500);   // crash at 400 ms, restart after 500 ms, hello again
    assert.strictEqual(exits.length >= 1, true, 'helper exit observed');
    assert.strictEqual(exits[0].code, 3); assert.strictEqual(exits[0].willRestart, true);
    assert.ok(hellos.length >= 2, `restarted: hellos=${hellos.length}`);
    assert.strictEqual(hp.restarts >= 1, true);
    const pid = hp.pid;
    hp.dispose();
    await sleep(300);
    assert.strictEqual(hp.running, false);
    if (pid) { assert.throws(() => process.kill(pid, 0), 'child is gone'); }
  });

  it('rejects when the helper prints no hello', async () => {
    const script = path.join(os.tmpdir(), `kiwrap-silent-${process.pid}.js`);
    fs.writeFileSync(script, 'setTimeout(() => {}, 5000);');
    try {
      const hp = new HelperProcess({ helperPath: script, pid: 1, inject: 'none', cloakExpected: false, nodePath: NODE });
      await assert.rejects(hp.start(600), /no hello line/);
      hp.dispose();
    } finally { fs.rmSync(script, { force: true }); }
  });
});

// ---- a scripted WebSocket standing in for /control ---------------------------------------------------
class FakeWs implements WebSocketLike {
  static last: FakeWs;
  readyState = 0;
  sent: string[] = [];
  listeners: Record<string, Array<(ev: any) => void>> = {};
  constructor(public url: string) { FakeWs.last = this; setTimeout(() => this.open(), 0); }
  addEventListener(type: string, l: (ev: any) => void): void { (this.listeners[type] ??= []).push(l); }
  send(d: string): void { this.sent.push(d); }
  close(): void { this.readyState = 3; this.emit('close', { code: 1000, reason: '' }); }
  open(): void { this.readyState = 1; this.emit('open', {}); }
  emit(type: string, ev: unknown): void { for (const l of this.listeners[type] ?? []) { l(ev); } }
  receive(o: unknown): void { this.emit('message', { data: JSON.stringify(o) }); }
}

describe('controlClient', () => {
  it('builds the url with the token', () => { assert.strictEqual(controlUrl(8765, 'a b'), 'ws://127.0.0.1:8765/control?token=a%20b'); });

  it('matches replies by id, falls back to FIFO per type, folds surface pushes', async () => {
    const events: string[] = [];
    const c = new ControlClient('ws://x', {
      onSurfaces: (l) => events.push('list:' + l.map((s) => s.id).join(',')),
      onSurfaceCreated: (s) => events.push('create:' + s.id),
      onSurfaceChanged: (s) => events.push('change:' + s.id),
      onSurfaceDestroyed: (id) => events.push('destroy:' + id),
    }, FakeWs as any);
    await c.connect();
    const ws = FakeWs.last;
    const p1 = c.surfaces();
    const req = JSON.parse(ws.sent[0]); assert.strictEqual(req.type, 'surfaces'); assert.strictEqual(typeof req.id, 'number');
    ws.receive({ type: 'surfaces', id: req.id, surfaces: [surf('pcb-1', 'pcb')] });
    assert.deepStrictEqual((await p1).map((s) => s.id), ['pcb-1']);
    // no id echoed: FIFO for the type
    const p2 = c.screenshot('pcb-1', 512);
    assert.deepStrictEqual(JSON.parse(ws.sent[1]).max_px, 512);
    ws.receive({ type: 'screenshot', surface: 'pcb-1', png: 'AAAA', w: 4, h: 4 });
    assert.deepStrictEqual(await p2, { png: 'AAAA', w: 4, h: 4 });
    // raise: kiwrap-live answers {type:'raise', ok}; ok:false rejects with the reason
    const p3 = c.raise('pcb-1'); ws.receive({ type: 'raise', surface: 'pcb-1', ok: true, id: JSON.parse(ws.sent[2]).id }); await p3;
    const p3b = c.raise('pcb-1'); ws.receive({ type: 'raise', surface: 'pcb-1', ok: false, reason: 'cloaked', id: JSON.parse(ws.sent[3]).id });
    await assert.rejects(p3b, /cloaked/);
    // error replies reject (both the {error} and the {message} spelling)
    const p4 = c.screenshot('nope'); ws.receive({ type: 'screenshot', surface: 'nope', ok: false, error: 'no such surface', id: JSON.parse(ws.sent[4]).id });
    await assert.rejects(p4, /no such surface/);
    const p4b = c.screenshot('nope'); ws.receive({ type: 'error', message: 'no surface nope', id: JSON.parse(ws.sent[5]).id });
    await assert.rejects(p4b, /no surface nope/);
    // keyframe never throws (older helpers answer with an error)
    const p4c = c.keyframe('pcb-1'); ws.receive({ type: 'error', error: 'unknown request type "keyframe"', id: JSON.parse(ws.sent[6]).id }); await p4c;
    // pushes: kiwrap-live's hello + event shapes and the older single-surface shapes
    ws.receive({ type: 'hello', version: '0.1.0', inject: 'events', surfaces: [surf('pcb-1', 'pcb'), surf('sch-1', 'sch')] });
    ws.receive({ type: 'event', event: 'create', surface: surf('3d-1', '3d') });
    ws.receive({ type: 'event', event: 'title', surface: surf('3d-1', '3d', 'renamed') });
    ws.receive({ type: 'event', event: 'rect', surface: surf('3d-1', '3d', 'renamed') });
    ws.receive({ type: 'event', event: 'destroy', id: '3d-1' });
    ws.receive({ type: 'surface_created', surface: surf('3d-1', '3d') });
    ws.receive({ type: 'surface', event: 'change', surface: surf('3d-1', '3d', 'renamed') });
    ws.receive({ type: 'surface_destroyed', surface: '3d-1' });
    ws.receive({ type: 'surface', event: 'destroy', surface: surf('pcb-1', 'pcb') });
    ws.receive({ type: 'surfaces', surfaces: [] });
    assert.deepStrictEqual(events, ['list:pcb-1', 'list:pcb-1,sch-1', 'create:3d-1', 'change:3d-1', 'change:3d-1', 'destroy:3d-1', 'create:3d-1', 'change:3d-1', 'destroy:3d-1', 'destroy:pcb-1', 'list:']);
    let exited = 0;
    (c as unknown as { events: { onProcessExit?: () => void } }).events.onProcessExit = () => { exited++; };
    ws.receive({ type: 'event', event: 'process_exit' });
    assert.strictEqual(exited, 1);
    assert.strictEqual(replyError({ type: 'keyframe', surface: 'x' }), null);
    assert.strictEqual(replyError({ type: 'error', error: 'bad request' }), 'bad request');
    // close rejects everything pending and disposing stops reconnects
    const p5 = c.surfaces(); ws.close();
    await assert.rejects(p5, /closed/);
    c.dispose();
    await assert.rejects(c.surfaces(), /not connected/);
  });
});

describe('surfaces', () => {
  it('maps files and kinds to editors / panels', () => {
    assert.strictEqual(kindForFile('C:\\p\\a.kicad_pcb'), 'pcb'); assert.strictEqual(kindForFile('a.KICAD_SCH'), 'sch');
    assert.strictEqual(kindForFile('a.kicad_pro'), 'mgr'); assert.strictEqual(kindForFile('a.txt'), null);
    assert.strictEqual(editorForFile('a.kicad_pcb'), 'pcb'); assert.strictEqual(editorForFile('a.kicad_sch'), 'sch'); assert.strictEqual(editorForFile('a.kicad_pro'), 'project');
    assert.strictEqual(tabShapeFor('pcb'), 'editor'); assert.strictEqual(tabShapeFor('sch'), 'editor');
    for (const k of ['3d', 'mgr', 'fp', 'sym', 'other'] as const) { assert.strictEqual(tabShapeFor(k), 'panel'); }
  });

  it('finds the project dir by the nearest .kicad_pro', () => {
    const tree: Record<string, string[]> = { 'C:\\w\\proj': ['proj.kicad_pro', 'sub'], 'C:\\w\\proj\\sub': ['sheet.kicad_sch'], 'C:\\w': ['proj'] };
    const list = (d: string) => { if (!(d in tree)) { throw new Error('ENOENT'); } return tree[d]; };
    assert.strictEqual(projectDirFor('C:\\w\\proj\\sub\\sheet.kicad_sch', list), 'C:\\w\\proj');
    assert.strictEqual(projectDirFor('C:\\w\\proj\\proj.kicad_pcb', list), 'C:\\w\\proj');
    assert.strictEqual(projectDirFor('C:\\elsewhere\\x.kicad_pcb', list), 'C:\\elsewhere');
  });

  it('model: find by kind prefers the title with the file stem; events fire', () => {
    const m = new SurfacesModel();
    const log: string[] = [];
    m.onChange((all, ch) => log.push(`${ch.kind}:${all.length}`));
    m.reset([surf('s1', 'sch', 'other.kicad_sch - Schematic Editor'), surf('s2', 'sch', 'ecc83-pp.kicad_sch - Schematic Editor'), surf('p1', 'pcb', 'ecc83-pp.kicad_pcb')]);
    assert.strictEqual(m.find('sch', 'C:\\x\\ecc83-pp.kicad_sch')?.id, 's2');
    assert.strictEqual(m.find('sch', 'C:\\x\\unknown.kicad_sch')?.id, 's1');
    assert.strictEqual(m.find('sch')?.id, 's1');
    assert.strictEqual(m.find('3d'), undefined);
    m.upsert(surf('d1', '3d')); m.upsert({ ...surf('d1', '3d'), title: 'x' }); m.remove('d1'); m.remove('d1'); m.clear();
    assert.deepStrictEqual(log, ['reset:3', 'create:4', 'change:4', 'destroy:3', 'reset:0']);
  });

  it('normalises wire objects, including kiwrap-live [l,t,r,b] rects and dialogs', () => {
    assert.strictEqual(normalizeSurface(null), null); assert.strictEqual(normalizeSurface({}), null);
    const n = normalizeSurface({ id: 7, kind: 'weird', title: 5, rect_px: { x: '1', w: 2 }, cloaked: 1 });
    assert.deepStrictEqual(n, { id: '7', kind: 'other', title: '', hwnd: 0, rect_px: { x: 1, y: 0, w: 2, h: 0 }, dpi: 96, cloaked: true, dialogs: [] });
    const real = normalizeSurface({ id: 'pcb', kind: 'pcb', title: 'x - PCB Editor', hwnd: 1234, rect_px: [60, 60, 1980, 1140], frame_px: [60, 60, 1980, 1140], dpi: 144, cloaked: false, cloak_flags: 0, enabled: true, foreground: true, dialogs: [{ hwnd: 9, title: 'Footprint Properties', class: '#32770', rect_px: [500, 400, 900, 800], offset: [440, 340], enabled: true }] });
    assert.deepStrictEqual(real?.rect_px, { x: 60, y: 60, w: 1920, h: 1080 });
    assert.deepStrictEqual(real?.frame_px, { x: 60, y: 60, w: 1920, h: 1080 });
    assert.strictEqual(real?.foreground, true); assert.strictEqual(real?.enabled, true);
    assert.deepStrictEqual(real?.dialogs, [{ hwnd: 9, title: 'Footprint Properties', rect_px: { x: 500, y: 400, w: 400, h: 400 } }]);
  });
});

describe('keybinding shield', () => {
  it('generates one forwardKey binding per chord, guarded by the context keys (input mode armed) and the active view', () => {
    const kb = generateKeybindings();
    assert.strictEqual(kb.length, SHIELD_CHORDS.length + PALETTE_CHORDS.length);
    assert.strictEqual(new Set(kb.map((k) => k.key)).size, kb.length, 'no duplicate chords');
    for (const k of kb) {
      assert.strictEqual(k.command, LIVE_COMMANDS.forwardKey);
      assert.deepStrictEqual(k.args, { key: k.key });
      assert.ok(k.when.includes(`activeCustomEditorId == '${LIVE_EDITOR_VIEW_TYPE}'`));
      assert.ok(k.when.includes(`activeWebviewPanelId == '${LIVE_PANEL_VIEW_TYPE}'`));
      // D20: every chord needs kiwrap.live.input, which is set only while an experimental input mode
      // injects; on the product path (the live view) VS Code's keyboard behaviour is untouched
      assert.ok(k.when.startsWith('kiwrap.live.shield && kiwrap.live.input &&'), k.when);
    }
    const palette = kb.filter((k) => k.when.includes('kiwrap.live.shieldPalette'));
    assert.deepStrictEqual(palette.map((k) => k.key).sort(), [...PALETTE_CHORDS].sort());
    assert.ok(SHIELD_CHORDS.includes('ctrl+w') && SHIELD_CHORDS.includes('f5') && SHIELD_CHORDS.includes('ctrl+p'));
    // finding 6: the active-view keys stay set while the focus is in the sidebar / terminal / panel /
    // an input box; the shield must not fire there
    const focus = '!sideBarFocus && !auxiliaryBarFocus && !panelFocus && !terminalFocus && !inputFocus && !notificationFocus';
    assert.deepStrictEqual([...FOCUS_NEGATIVES], ['sideBarFocus', 'auxiliaryBarFocus', 'panelFocus', 'terminalFocus', 'inputFocus', 'notificationFocus']);
    assert.strictEqual(whenClause(false), `kiwrap.live.shield && kiwrap.live.input && (activeCustomEditorId == 'kiwrap.live' || activeWebviewPanelId == 'kiwrap.live.panel') && ${focus}`);
    assert.strictEqual(whenClause(true), `kiwrap.live.shield && kiwrap.live.input && kiwrap.live.shieldPalette && (activeCustomEditorId == 'kiwrap.live' || activeWebviewPanelId == 'kiwrap.live.panel') && ${focus}`);
    for (const k of kb) { for (const neg of FOCUS_NEGATIVES) { assert.ok(k.when.includes(`!${neg}`), `${k.key} guards ${neg}`); } }
  });

  it('package.json carries the same shield (gen_contributes --merge), conditional on kiwrap.live.input', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(EXT, 'package.json'), 'utf8'));
    const shield = (pkg.contributes.keybindings as Array<{ command: string; when: string }>).filter((k) => k.command === LIVE_COMMANDS.forwardKey);
    assert.strictEqual(shield.length, generateKeybindings().length);
    for (const k of shield) { assert.ok(k.when.includes('!sideBarFocus') && k.when.includes('!terminalFocus'), k.when); assert.ok(k.when.includes('kiwrap.live.input &&'), k.when); }
    // D20: the live view is the only mode in the UI; the injecting modes are one experimental setting, off by default
    const confs = Array.isArray(pkg.contributes.configuration) ? pkg.contributes.configuration : [pkg.contributes.configuration];
    const props: Record<string, { default?: unknown; enum?: string[]; description?: string }> = Object.assign({}, ...confs.map((c: { properties: Record<string, unknown> }) => c.properties));
    assert.deepStrictEqual(props['kiwrap.live.inputMode'].enum, ['none', 'mirror', 'hybrid', 'events']);
    assert.strictEqual(props['kiwrap.live.inputMode'].default, 'none');
    assert.match(props['kiwrap.live.inputMode'].description ?? '', /EXPERIMENTAL/);
    assert.strictEqual(props['kiwrap.live.raiseOnDoubleClick'].default, true);
    assert.ok(!('kiwrap.live.mode' in props) && !('kiwrap.live.mirrorInjector' in props), 'the old mode settings are gone');
    for (const c of pkg.contributes.commands as Array<{ command: string; title: string }>) {
      if (c.command.startsWith('kiwrap.live') || c.command === 'kiwrap.openLive') { assert.ok(!/hidden|mirror|cloak/i.test(c.title), `${c.command}: ${c.title}`); }
    }
    // finding 14: executable settings are machine-scoped
    for (const key of ['kiwrap.uvPath', 'kiwrap.serverProject', 'kiwrap.live.helperPath', 'kiwrap.live.nodePath']) {
      const confs = Array.isArray(pkg.contributes.configuration) ? pkg.contributes.configuration : [pkg.contributes.configuration];
      const prop = confs.map((c: { properties: Record<string, { scope?: string }> }) => c.properties[key]).find(Boolean);
      assert.ok(prop, `${key} declared`);
      assert.strictEqual(prop.scope, 'machine', `${key} scope`);
    }
  });

  it('matches the generated contributes.live.json fragment', () => {
    const frag = JSON.parse(fs.readFileSync(path.join(EXT, 'src', 'live', 'contributes.live.json'), 'utf8'));
    assert.deepStrictEqual(frag.keybindings, generateKeybindings());
    assert.deepStrictEqual(frag.commands.map((c: { command: string }) => c.command).sort(), Object.values(LIVE_COMMANDS).sort());
    assert.strictEqual(frag.customEditors[0].viewType, LIVE_EDITOR_VIEW_TYPE);
    assert.strictEqual(frag.customEditors[0].priority, 'option');
    assert.ok('kiwrap.live.keepCommandPalette' in frag.configuration.properties);
    assert.ok('kiwrap.live.inputMode' in frag.configuration.properties);
  });
});

describe('input-mode decision (D20: the live view is the product path)', () => {
  const stock = asSession({ session_id: 's', pid: 1, install: { name: 'stock' }, mode_used: 'view' });
  const patchedView = asSession({ session_id: 's', pid: 1, install: { name: 'fork', capabilities: { 'gui.input_from_events': true } }, mode_used: 'view', cloaked: false });
  const hiddenApplied = asSession({ session_id: 's', pid: 1, install: 'x', mode_used: 'hidden', cloaked: true });
  it('the default (none) never injects and launches kw_live in view mode, whatever the build', () => {
    assert.deepStrictEqual(resolveInject('none', stock), { inject: 'none' });
    assert.deepStrictEqual(resolveInject('none', patchedView), { inject: 'none' });
    assert.deepStrictEqual(resolveInject('none', hiddenApplied), { inject: 'none' });
    assert.deepStrictEqual(resolveInject('none', null), { inject: 'none' });
    for (const pref of ['none', 'mirror', 'hybrid'] as const) { assert.strictEqual(launchModeFor(pref), 'view'); }
    assert.strictEqual(launchModeFor('events'), 'hidden');
    assert.strictEqual(inputModeOf(undefined), 'none'); assert.strictEqual(inputModeOf('auto'), 'none'); assert.strictEqual(inputModeOf('hidden'), 'none');
    assert.strictEqual(inputModeOf('events'), 'events'); assert.strictEqual(inputModeOf('hybrid'), 'hybrid');
  });
  it('experimental preferences: mirror / hybrid as asked, events only when kw_live really cloaked the KiCad', () => {
    assert.deepStrictEqual(resolveInject('mirror', stock), { inject: 'mirror' });
    assert.deepStrictEqual(resolveInject('hybrid', stock), { inject: 'hybrid' });
    assert.deepStrictEqual(resolveInject('events', hiddenApplied), { inject: 'events' });
    for (const s of [stock, patchedView, null]) {
      const r = resolveInject('events', s);
      assert.strictEqual(r.inject, 'none'); assert.match(r.warning ?? '', /display-input/);
    }
    assert.strictEqual(sessionSupportsEvents(stock), false);
    assert.strictEqual(sessionSupportsEvents(patchedView), false, 'a view session on a patched build was not launched cloaked');
    assert.strictEqual(sessionSupportsEvents(hiddenApplied), true);
  });
});

describe('kw_live result unwrapping', () => {
  const payload = { session_id: 'abc', pid: 4242, install: { name: 'kiwrap/6dd893f7d1' }, api_socket: 'ipc://x', token: 't', editors: ['pcb'] };
  it('accepts plain objects, MCP results, and ext-core Receipts', () => {
    assert.deepStrictEqual(unwrapToolResult(payload), payload);
    assert.deepStrictEqual(unwrapToolResult({ content: [{ type: 'text', text: JSON.stringify(payload) }], structuredContent: payload }), payload);
    assert.deepStrictEqual(unwrapToolResult({ content: [{ type: 'text', text: JSON.stringify(payload) }] }), payload);
    const receipt = { ok: true, changed: { added: [], updated: [], removed: [] }, validators: [], provider_used: 'ipc11', degraded: [], snapshot: null, stage: null, next_suggested: null, warnings: [], image_info: null, data: payload };
    assert.deepStrictEqual(unwrapToolResult(receipt), payload);
    assert.deepStrictEqual(unwrapToolResult({ structuredContent: receipt }), payload);
    assert.throws(() => unwrapToolResult({ isError: true, content: [{ type: 'text', text: 'E_NO_GUI: nope' }] }), /E_NO_GUI/);
    assert.throws(() => unwrapToolResult('x'), /returned string/);
  });
  it('asSession validates the pid and keeps optional fields', () => {
    const s = asSession(payload);
    assert.strictEqual(s.pid, 4242); assert.deepStrictEqual(s.editors, ['pcb']); assert.strictEqual(s.mode, undefined);
    assert.throws(() => asSession({ session_id: 'x' }), /no pid/);
  });
});

describe('session identity (finding 7 / 8)', () => {
  it('projectKey: a .kicad_pro path and its directory are one project, case and slashes aside', () => {
    const a = projectKey('C:\\work\\ecc83\\ecc83-pp.kicad_pro');
    assert.strictEqual(a, projectKey('c:/work/ECC83/'));
    assert.strictEqual(a, projectKey('C:/work/ecc83'));
    assert.notStrictEqual(a, projectKey('C:/work/ecc83/sub'));
    assert.ok(!a.endsWith('.kicad_pro') && !a.endsWith('\\') && !a.endsWith('/'));
  });
  it('surfaceKindFor maps editors to the surface kind their tab shows', () => {
    assert.strictEqual(surfaceKindFor('pcb'), 'pcb'); assert.strictEqual(surfaceKindFor('sch'), 'sch'); assert.strictEqual(surfaceKindFor('project'), 'mgr');
  });
});
