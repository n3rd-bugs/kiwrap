/**
 * Integration: the extension against the real `uv run kiwrap serve` and the KiCad installs on
 * this machine (10.99 nightly / 10.0.6), in a scratch workspace holding a copy of the ecc83 demo
 * (prepared by test/integration/runTest.ts). Runs ERC and DRC through the commands, checks the
 * Problems panel and the tree. No KiCad GUI is started; kicad-cli runs under kiwrap's sandbox.
 */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { KiwrapApi } from '../../../src/extension';
import type { KiwrapNode } from '../../../src/status/tree';

const EXTENSION_ID = 'kiwrap-private.kiwrap';

function kiwrapDiagnostics(): { uri: vscode.Uri; diag: vscode.Diagnostic }[] {
  const out: { uri: vscode.Uri; diag: vscode.Diagnostic }[] = [];
  for (const [uri, diags] of vscode.languages.getDiagnostics()) {
    for (const diag of diags) {
      if (diag.source?.startsWith('kiwrap')) {
        out.push({ uri, diag });
      }
    }
  }
  return out;
}

function label(n: KiwrapNode): string {
  return typeof n.label === 'string' ? n.label : (n.label?.label ?? '');
}

describe('kiwrap extension (real server, ecc83 demo)', function () {
  this.timeout(600000);
  let api: KiwrapApi;
  const workspace = process.env.KIWRAP_IT_WORKSPACE ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
  const projectDir = path.join(workspace, 'ecc83');
  /** D21 consent modals the server's errors raised during the suite (answered Cancel: a test host cannot click). */
  const consentAsked: string[] = [];

  before(async () => {
    const ext = vscode.extensions.getExtension<KiwrapApi>(EXTENSION_ID);
    assert.ok(ext, `extension ${EXTENSION_ID} not found`);
    api = await ext.activate();
    const real = api.session.consentUi;
    api.session.consentUi = {
      ...real,
      modal: async (message) => {
        consentAsked.push(message);
        return undefined;
      },
    };
    await api.whenReady();
  });

  after(async () => {
    await api.session.server?.stop();
  });

  it('starts the kiwrap server and lists the installs (10.99 / 10.0.6 present on this machine)', () => {
    assert.equal(api.session.serverState, 'ready', `server state ${api.session.serverState}: ${api.session.serverDetail}`);
    const env = api.session.env;
    assert.ok(env, `kw_env failed: ${api.session.envError}`);
    const versions = env.installs.map((i) => i.version);
    assert.ok(versions.some((v) => v.startsWith('10.99')), `no 10.99 nightly in ${versions.join(', ')}`);
    assert.ok(versions.includes('10.0.6'), `no 10.0.6 in ${versions.join(', ')}`);
    assert.equal(env.default_install, versions[0]);
  });

  it('tracks the ecc83-pp project and reports its status', async () => {
    const p = api.session.pickWorkspaceProject();
    assert.ok(p, 'no project found in the workspace');
    assert.equal(p.name, 'ecc83-pp');
    assert.equal(path.dirname(p.pro).toLowerCase(), projectDir.toLowerCase());
    const st = api.session.projectStatus ?? (await api.session.refreshProject());
    assert.ok(st, 'kw_project status failed');
    assert.ok(st.files.pcb.includes('ecc83-pp.kicad_pcb'), JSON.stringify(st.files));
  });

  it('kiwrap.runErc and kiwrap.runDrc fill the Problems panel', async () => {
    await vscode.commands.executeCommand('kiwrap.runErc');
    assert.ok(api.session.lastErc, `ERC did not record a result${consentAsked.length ? ` (consent asked: ${consentAsked.join(' | ')})` : ''}`);
    assert.equal(typeof api.session.lastErc.numbers.errors, 'number');

    // D22: the explicit DRC does not save, so a project whose format an installed KiCad writes
    // (the demo copy) never needs the D21 consent
    await vscode.commands.executeCommand('kiwrap.runDrc');
    assert.ok(api.session.lastDrc, `DRC did not record a result${consentAsked.length ? ` (consent asked: ${consentAsked.join(' | ')})` : ''}`);
    assert.deepEqual(consentAsked, [], 'no format consent for a project an installed KiCad writes');
    assert.equal(typeof api.session.lastDrc.numbers.must_fix, 'number');

    const diags = kiwrapDiagnostics();
    assert.ok(diags.length > 0, 'expected at least one ERC/DRC diagnostic on the ecc83 demo');
    const drc = diags.filter((d) => d.diag.source === 'kiwrap DRC');
    assert.ok(drc.length > 0, 'expected DRC diagnostics (the demo has silk/edge items)');
    for (const d of drc) {
      assert.ok(d.uri.fsPath.toLowerCase().endsWith('ecc83-pp.kicad_pcb'), d.uri.fsPath);
      assert.ok(typeof d.diag.code === 'string' && d.diag.code.length > 0, 'code = KiCad type');
      assert.match(d.diag.message, /\[[a-z_]+, (error|warning|exclusion)(, (must_fix|review|cosmetic))?\]/);
    }
    // every DRC item of the demo has a uuid that exists in the board text -> anchored below line 1
    const anchored = drc.filter((d) => d.diag.range.start.line > 0);
    assert.ok(anchored.length > 0, 'no DRC diagnostic anchored on a uuid line');
    const board = fs.readFileSync(path.join(projectDir, 'ecc83-pp.kicad_pcb'), 'utf8').split('\n');
    for (const d of anchored) {
      assert.match(board[d.diag.range.start.line], /uuid|tstamp/, `line ${d.diag.range.start.line} is not a uuid line`);
    }
    const counts = api.diagnostics.counts();
    assert.equal(counts.drc.errors + counts.drc.warnings + counts.drc.infos, drc.length);
  });

  it('the tree shows server, installs (newest marked), engines, project with stage + numbers, live', () => {
    const roots = api.tree.roots();
    const labels = roots.map(label);
    assert.deepEqual(labels.slice(0, 3), ['Server', 'KiCad installs', 'Engines']);
    assert.equal(labels[3], 'ecc83-pp');
    assert.equal(labels[4], 'Live KiCad');

    const server = roots[0];
    assert.match(String(server.description), /^running/);

    const installs = roots[1];
    assert.ok(installs.children.length >= 2, `installs: ${installs.children.map(label).join(', ')}`);
    const newest = installs.children.filter((c) => /\bnewest\b/.test(String(c.description)));
    assert.equal(newest.length, 1, `exactly one row is the newest (finding 18): ${installs.children.map((c) => `${label(c)} [${c.description}]`).join(', ')}`);
    assert.ok(!installs.children.some((c) => String(c.description).includes('$(')), 'no codicon syntax in descriptions');
    const perLabel = new Map<string, number>();
    for (const c of installs.children) { perLabel.set(label(c), (perLabel.get(label(c)) ?? 0) + 1); }
    assert.ok([...perLabel.values()].every((n) => n === 1), `install labels are unique: ${[...perLabel.keys()].join(', ')}`);
    assert.ok(installs.children.some((c) => label(c).includes('10.0.6')));

    const engines = roots[2];
    assert.ok(engines.children.some((c) => label(c) === 'autorouter'));

    const project = roots[3];
    const fields = Object.fromEntries(project.children.map((c) => [label(c), String(c.description ?? '')]));
    assert.ok('stage' in fields, `fields: ${Object.keys(fields).join(', ')}`);
    // D21: the effective target (or the pin / a note on a server without the fields) and the file format
    assert.ok(fields['target KiCad'] && fields['target KiCad'].length > 0, `target KiCad: ${fields['target KiCad']}`);
    assert.match(fields['file format'] ?? '', /(sch|pcb) \d{8}/);
    assert.notEqual(fields.stage, '?');
    assert.match(fields.ERC, /^(ok|FAIL) errors=\d+ warnings=\d+/);
    assert.match(fields.DRC, /^(ok|FAIL) must_fix=\d+ review=\d+ cosmetic=\d+ unconnected=\d+/);
    const drcNode = project.children.find((c) => label(c) === 'DRC');
    assert.ok(drcNode && drcNode.children.some((c) => label(c) === 'must_fix'));

    const live = roots[4];
    assert.ok(String(live.description).length > 0);
  });

  it('an install row click reveals details and never changes kiwrap.kicadInstall; Select does (the one-click trap)', async () => {
    const cfg = () => vscode.workspace.getConfiguration('kiwrap').get<string>('kicadInstall', '');
    assert.equal(cfg(), '', "the integration workspace follows the project's format (D21)");
    const installs = api.tree.roots()[1];
    for (const row of installs.children) {
      assert.equal(row.command, undefined, `${label(row)} has a click command`);
      assert.equal(row.collapsibleState, vscode.TreeItemCollapsibleState.Collapsed, `${label(row)} does not expand`);
      assert.equal(row.contextValue, 'install');
      const details = row.children.map(label);
      assert.ok(details.includes('root') && details.includes('kicad-cli'), `${label(row)} details: ${details.join(', ')}`);
    }
    const stable = installs.children.find((c) => label(c).includes('10.0.6'));
    assert.ok(stable);
    try {
      // the inline "Select" action and the context menu pass the tree element itself
      await vscode.commands.executeCommand('kiwrap.selectInstall', stable);
      assert.equal(cfg(), stable.value);
      const again = api.tree.roots()[1].children.find((c) => label(c) === label(stable));
      assert.match(String(again?.description), /\bselected\b/);
      assert.equal((again?.iconPath as vscode.ThemeIcon | undefined)?.id, 'star-full');
    } finally {
      await vscode.commands.executeCommand('kiwrap.selectInstall', '');
    }
    assert.equal(cfg(), '');
  });

  it('Upgrade project file format asks with the D21 modal first (Cancel changes nothing)', async function () {
    const st = await api.session.refreshProject();
    const newest = api.session.env?.installs[0];
    assert.ok(st && newest);
    if (st.epochs.sch === newest.capabilities?.['formats.sch'] && st.epochs.pcb === newest.capabilities?.['formats.pcb']) {
      this.skip(); // the demo copy is already in the newest format: the command only says so
    }
    const asked: { message: string; buttons: string[] }[] = [];
    const saved = api.session.consentUi;
    const pcb = path.join(projectDir, 'ecc83-pp.kicad_pcb');
    const stamp = fs.statSync(pcb).mtimeMs;
    api.session.consentUi = {
      ...saved,
      modal: async (message, _detail, buttons) => {
        asked.push({ message, buttons });
        return undefined; // Cancel
      },
    };
    try {
      await vscode.commands.executeCommand('kiwrap.upgradeProject');
    } finally {
      api.session.consentUi = saved;
    }
    assert.equal(asked.length, 1);
    assert.match(asked[0].message, /^ecc83-pp is in KiCad .* format and .*(can|still) writes? it\. Upgrade the project files to KiCad .* format\? .* will no longer open them\.$/);
    assert.equal(asked[0].buttons[0], 'Upgrade');
    assert.match(asked[0].buttons[1], /^(Install|Use) KiCad/);
    assert.equal(asked[0].buttons[2], 'Cancel');
    assert.equal(fs.statSync(pcb).mtimeMs, stamp, 'Cancel wrote nothing');
  });

  it('review item 1 live: a 10.0 project pinned to 10.99 asks before a saving DRC; "Use KiCad 10.0.6" keeps its format', async function () {
    // KiCad's own 10.0 demo (pic_programmer: pcb 20260206), copied at test time (CLAUDE.md corpus rule)
    const stable = api.session.env?.installs.find((i) => i.version === '10.0.6');
    const nightly = api.session.env?.installs.find((i) => i.version.startsWith('10.99'));
    if (!stable || !nightly) {
      this.skip();
    }
    const demo = path.join(stable!.share, 'demos', 'pic_programmer');
    if (!fs.existsSync(path.join(demo, 'pic_programmer.kicad_pro'))) {
      this.skip();
    }
    const dir = path.join(workspace, 'pic_programmer');
    fs.rmSync(dir, { recursive: true, force: true });
    fs.cpSync(demo, dir, { recursive: true });
    const pro = path.join(dir, 'pic_programmer.kicad_pro');
    const pcb = path.join(dir, 'pic_programmer.kicad_pcb');
    const before = fs.readFileSync(pcb);
    const server = await api.session.ready();
    try {
      await server.projectRetarget(pro, nightly!.root);
    } catch (err) {
      // a server without kw_project(action='retarget') (contract (c)) cannot build the scenario
      console.log(`[it] retarget unavailable: ${(err as Error).message}`);
      this.skip();
    }
    const saved = api.session.consentUi;
    const asked: { message: string; buttons: string[] }[] = [];
    let answer: (buttons: string[]) => string | undefined = () => undefined;
    api.session.consentUi = {
      ...saved,
      modal: async (message, _detail, buttons) => {
        asked.push({ message, buttons });
        return answer(buttons);
      },
    };
    try {
      // Cancel: nothing is written, the error is the handled consent error
      await assert.rejects(server.pcbDrc(pro, { save_board: true }), (err: unknown) => {
        const e = err as { code?: string; handled?: boolean };
        assert.equal(e.code, 'E_FORMAT_UPGRADE_REQUIRED', String(err));
        assert.equal(e.handled, true);
        return true;
      });
      assert.equal(asked.length, 1);
      assert.match(asked[0].message, /^pic_programmer is in KiCad 10\.0 format and installed KiCad 10\.0\.6 still writes it\. Upgrade the project files to KiCad 10\.99 format\? KiCad 10\.0 will no longer open them\.$/);
      assert.deepEqual(asked[0].buttons, ['Upgrade', 'Use KiCad 10.0.6', 'Cancel']);
      assert.ok(fs.readFileSync(pcb).equals(before), 'Cancel left the board byte-identical');
      // "Use KiCad 10.0.6": pinned back, the DRC is retried on 10.0.6 and the format stays 10.0
      answer = (buttons) => buttons.find((b) => b.startsWith('Use KiCad'));
      const r = await server.pcbDrc(pro, { save_board: true });
      assert.equal(typeof r.data.must_fix, 'number');
      assert.equal(asked.length, 2);
      assert.match(fs.readFileSync(pcb, 'utf8').slice(0, 200), /\(version 20260206\)/, 'the board keeps the KiCad 10.0 format');
      const st = await server.projectStatus(pro);
      assert.equal(st.data.target_kicad, '10.0.6');
    } finally {
      api.session.consentUi = saved;
    }
  });

  it('opens the read-only preview editor for the board', async () => {
    const uri = vscode.Uri.file(path.join(projectDir, 'ecc83-pp.kicad_pcb'));
    await vscode.commands.executeCommand('vscode.openWith', uri, 'kiwrap.preview');
    const tab = vscode.window.tabGroups.all
      .flatMap((g) => g.tabs)
      .find((t) => t.input instanceof vscode.TabInputCustom && t.input.viewType === 'kiwrap.preview');
    assert.ok(tab, 'preview tab not opened');
    // the render itself runs asynchronously in the webview; give kicad-cli a moment, then close
    await new Promise((r) => setTimeout(r, 3000));
    await vscode.window.tabGroups.close(tab, true);
  });

  it('Configure Claude Code plans .mcp.json + CLAUDE.md + the skill for this workspace', async () => {
    // the command itself needs a modal answer; exercise the plan it is built on
    const { planConfigure } = await import('../../../src/commands/configurePlan');
    const edits = planConfigure({ workspace, serverProject: process.env.KIWRAP_IT_REPO ?? '' });
    const names = edits.map((e) => path.relative(workspace, e.file).replace(/\\/g, '/'));
    assert.ok(names.includes('.mcp.json'), names.join(', '));
    assert.ok(names.includes('CLAUDE.md'));
    assert.ok(names.includes('.claude/skills/kiwrap-flow/SKILL.md'));
    const mcp = JSON.parse(edits.find((e) => e.file.endsWith('.mcp.json'))!.after);
    assert.equal(mcp.mcpServers.kiwrap.args[0], 'run');
  });

  it('kiwrap.openLive without the live component reports the state instead of failing', async function () {
    const commands = await vscode.commands.getCommands(true);
    if (api.session.server?.liveAvailable || commands.includes('kiwrap.live.open')) {
      // the live path would start a sandboxed KiCad GUI; that belongs to the live component's tests
      this.skip();
    }
    await vscode.commands.executeCommand('kiwrap.openLive', vscode.Uri.file(path.join(projectDir, 'ecc83-pp.kicad_pcb')));
    assert.equal(api.session.live.kind, 'idle');
  });
});
