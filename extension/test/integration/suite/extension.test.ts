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

  before(async () => {
    const ext = vscode.extensions.getExtension<KiwrapApi>(EXTENSION_ID);
    assert.ok(ext, `extension ${EXTENSION_ID} not found`);
    api = await ext.activate();
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
    assert.ok(api.session.lastErc, 'ERC did not record a result');
    assert.equal(typeof api.session.lastErc.numbers.errors, 'number');

    await vscode.commands.executeCommand('kiwrap.runDrc');
    assert.ok(api.session.lastDrc, 'DRC did not record a result');
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

  it('the tree shows server, installs (default marked), engines, project with stage + numbers, live', () => {
    const roots = api.tree.roots();
    const labels = roots.map(label);
    assert.deepEqual(labels.slice(0, 3), ['Server', 'KiCad installs', 'Engines']);
    assert.equal(labels[3], 'ecc83-pp');
    assert.equal(labels[4], 'Live KiCad');

    const server = roots[0];
    assert.match(String(server.description), /^running/);

    const installs = roots[1];
    assert.ok(installs.children.length >= 2, `installs: ${installs.children.map(label).join(', ')}`);
    const defaults = installs.children.filter((c) => /\bdefault\b/.test(String(c.description)));
    assert.equal(defaults.length, 1, `exactly one row is the default (finding 18): ${installs.children.map((c) => `${label(c)} [${c.description}]`).join(', ')}`);
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
    assert.notEqual(fields.stage, '?');
    assert.match(fields.ERC, /^(ok|FAIL) errors=\d+ warnings=\d+/);
    assert.match(fields.DRC, /^(ok|FAIL) must_fix=\d+ review=\d+ cosmetic=\d+ unconnected=\d+/);
    const drcNode = project.children.find((c) => label(c) === 'DRC');
    assert.ok(drcNode && drcNode.children.some((c) => label(c) === 'must_fix'));

    const live = roots[4];
    assert.ok(String(live.description).length > 0);
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
