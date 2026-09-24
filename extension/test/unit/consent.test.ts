/**
 * D21 consent flow (review item 1 / B1): the E_FORMAT_UPGRADE_REQUIRED modal, Upgrade -> kw_project
 * upgrade(confirm=true) -> retry, Install KiCad <x>..., Cancel, E_FORMAT_NEWER, E_READONLY; driven
 * through McpClient's onToolError against the fake stdio server with a stubbed window API.
 */
import * as assert from 'node:assert/strict';
import * as path from 'node:path';
import {
  E_FORMAT_UPGRADE_REQUIRED,
  FormatConsent,
  formatErrorInfo,
  formatLabel,
  hintVersion,
  majorMinor,
  newerMessage,
  proactiveUpgradeInfo,
  projectKey,
  projectNameOf,
  readonlyMessage,
  upgradePrompt,
  type ConsentUi,
} from '../../src/consent';
import { KiwrapServer } from '../../src/kiwrapServer';
import { KiwrapError, isHandledError, type InstallRow, type ProjectStatusData } from '../../src/types';

const FAKE = path.join(__dirname, 'fakeServer.js');

const UPGRADE_DATA = {
  files: [
    { path: 'C:/work/kit/kit.kicad_pcb', kind: 'pcb', stamp: 20260206 },
    { path: 'C:/work/kit/kit.kicad_sch', kind: 'sch', stamp: 20260206 },
  ],
  project_epoch: { pcb: 20260206, sch: 20260206 },
  candidates: [{ version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } }],
  upgrade_to: '10.99.0',
  install_hint: 'winget install KiCad.KiCad --version 10.0.6',
};

/** A ConsentUi that answers the modal with `answer` and records everything. */
class StubUi implements ConsentUi {
  modals: { message: string; detail: string; buttons: string[] }[] = [];
  errors: { message: string; buttons: string[] }[] = [];
  infos: string[] = [];
  installs: { hint: string | null; version: string | null }[] = [];
  logs: string[] = [];
  constructor(
    public answer: (buttons: string[]) => string | undefined,
    private readonly modalDelayMs = 0,
  ) {}
  async modal(message: string, detail: string, buttons: string[]) {
    this.modals.push({ message, detail, buttons });
    if (this.modalDelayMs) {
      await new Promise((r) => setTimeout(r, this.modalDelayMs));
    }
    return this.answer(buttons);
  }
  /** The button a non-modal error message is answered with (default: dismissed). */
  errorAnswer: (buttons: string[]) => string | undefined = () => undefined;
  async error(message: string, buttons: string[] = []) {
    this.errors.push({ message, buttons });
    return this.errorAnswer(buttons);
  }
  async info(message: string) {
    this.infos.push(message);
    return undefined;
  }
  async install(hint: string | null, version: string | null) {
    this.installs.push({ hint, version });
  }
  log(line: string) {
    this.logs.push(line);
  }
}

describe('consent: error data and texts (D21)', () => {
  it('normalises the E_FORMAT_UPGRADE_REQUIRED data and names the release from the install hint', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: UPGRADE_DATA }, { project: 'C:/work/kit/kit.kicad_pro' });
    assert.equal(info.project, 'C:/work/kit/kit.kicad_pro');
    assert.equal(info.projectName, 'kit');
    assert.equal(info.files.length, 2);
    assert.deepEqual(info.projectEpoch, { pcb: 20260206, sch: 20260206 });
    assert.equal(info.upgradeTo, '10.99.0');
    assert.equal(info.installVersion, '10.0.6');
    assert.equal(info.formatKicad, '10.0');
    assert.equal(formatLabel(info), 'KiCad 10.0');
  });

  it('the modal reads exactly as the owner asked, with Upgrade / Install KiCad <x>... / Cancel', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: UPGRADE_DATA }, { project: 'C:/work/kit' });
    const p = upgradePrompt(info);
    assert.equal(
      p.message,
      'kit is in KiCad 10.0 format and no installed KiCad 10.0 can write it. Upgrade the project files to KiCad 10.99 format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad 10.0\u2026', 'Cancel']);
    assert.match(p.detail, /kit\.kicad_pcb \(pcb, format 20260206\)/);
    assert.match(p.detail, /Installed: KiCad 10\.99\.0 \(sch 20260830, pcb 20260901\)/);
    assert.match(p.detail, /snapshot/);
    assert.match(p.detail, /winget install KiCad\.KiCad --version 10\.0\.6/);
  });

  it('falls back to the format stamp when the release is unknown, and prefers an explicit project_kicad', () => {
    const bare = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { project_epoch: { pcb: 20250114 } } }, { project: 'C:/a/b.kicad_pro' });
    assert.equal(bare.formatKicad, null);
    const p = upgradePrompt(bare);
    assert.match(p.message, /^b is in KiCad file format 20250114 format and no installed KiCad can write it\. Upgrade the project files to the newest installed KiCad format\?/);
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad\u2026', 'Cancel']);
    const named = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { ...UPGRADE_DATA, project_kicad: '9.0.9' } });
    assert.equal(named.formatKicad, '9.0');
    const mixed = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { files: [{ path: 'x.kicad_sch', kind: 'sch', stamp: 20250114 }, { path: 'x.kicad_pcb', kind: 'pcb', stamp: 20241229 }] } });
    assert.deepEqual(mixed.projectEpoch, { sch: 20250114, pcb: 20241229 });
    assert.equal(formatLabel(mixed), 'KiCad file format sch 20250114, pcb 20241229');
  });

  it('the proactive prompt names an installed KiCad that still writes the format', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { project_epoch: { sch: 20250114, pcb: 20241229 }, project_kicad: '9.0.9', upgrade_to: '10.99.0' } }, { project: 'C:/w/ecc83/ecc83-pp.kicad_pro' });
    info.writer = '9.0.9';
    assert.equal(
      upgradePrompt(info).message,
      'ecc83-pp is in KiCad 9.0 format and installed KiCad 9.0.9 still writes it. Upgrade the project files to KiCad 10.99 format? KiCad 9.0 will no longer open them.',
    );
  });

  it('proactive command, no installed writer (fixer finding 3): the modal names KiCad 10.0 and the winget command', () => {
    // the D21 headline case: pic_programmer (10.0 format) with only the 10.99 nightly installed
    const nightly: InstallRow = {
      version: '10.99.0', major_minor: '10.99', root: 'C:/Program Files/KiCad/10.99', kicad_cli: 'C:/Program Files/KiCad/10.99/bin/kicad-cli.exe',
      share: 'C:/Program Files/KiCad/10.99/share/kicad', source: 'registry', python: null,
      capabilities: { 'formats.sch': 20260830, 'formats.pcb': 20260901 },
    };
    const st = {
      ok: true, project: 'C:/w/pic_programmer/pic_programmer.kicad_pro', dir: 'C:/w/pic_programmer', name: 'pic_programmer',
      stage: 'created', next_suggested: null, target_kicad: null, target_installed: null, hashes: {},
      epochs: { sch: 20260101, pcb: 20260206 },
      files: { sch: ['pic_programmer.kicad_sch', 'pic_sockets.kicad_sch'], pcb: ['pic_programmer.kicad_pcb'] },
      editor_locks: [], snapshot_head: null, external_edit: null, history: [],
      target_effective: '10.99.0', target_source: 'file_epoch_newer_readonly', epoch_match: false,
      project_kicad: '10.0', install_hint: 'winget install KiCad.KiCad --version 10.0.6',
      file_epochs: [
        { path: 'pic_programmer.kicad_sch', kind: 'sch', stamp: 20260101 },
        { path: 'pic_sockets.kicad_sch', kind: 'sch', stamp: 20260101 },
        { path: 'pic_programmer.kicad_pcb', kind: 'pcb', stamp: 20260206 },
      ],
      candidates: [{ version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 }, reads: true, matches: false }],
    } as ProjectStatusData;
    const { info, writers } = proactiveUpgradeInfo(st.project, st, [nightly], '10.99.0');
    assert.deepEqual(writers, []);
    assert.equal(info.writer, null);
    assert.equal(info.formatKicad, '10.0');
    assert.equal(info.installHint, 'winget install KiCad.KiCad --version 10.0.6');
    const p = upgradePrompt(info);
    assert.equal(
      p.message,
      'pic_programmer is in KiCad 10.0 format and no installed KiCad 10.0 can write it. Upgrade the project files to KiCad 10.99 format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad 10.0\u2026', 'Cancel']);
    assert.match(p.detail, /winget install KiCad\.KiCad --version 10\.0\.6/);
    assert.match(p.detail, /pic_sockets\.kicad_sch \(sch, format 20260101\)/);
    // an older server without the fields still gets a modal (the format stamps name it)
    const bare = proactiveUpgradeInfo(st.project, { ...st, project_kicad: undefined, install_hint: undefined, file_epochs: undefined, candidates: undefined }, [nightly], '10.99.0');
    assert.equal(bare.info.formatKicad, null);
    assert.match(upgradePrompt(bare.info).message, /^pic_programmer is in KiCad file format sch 20260101, pcb 20260206 format/);
  });

  it('proactive command: the server\'s generation match names an installed writer the exact stamps miss', () => {
    // 10.0 demo sheets are stamped 20260101; 10.0.6 writes 20260306 - the server still says it matches
    const stable: InstallRow = {
      version: '10.0.6', major_minor: '10.0', root: 'C:/Program Files/KiCad/10.0', kicad_cli: 'C:/Program Files/KiCad/10.0/bin/kicad-cli.exe',
      share: 'C:/Program Files/KiCad/10.0/share/kicad', source: 'registry', python: null,
      capabilities: { 'formats.sch': 20260306, 'formats.pcb': 20260206 },
    };
    const st = {
      ok: true, project: 'C:/w/pic/pic.kicad_pro', dir: 'C:/w/pic', name: 'pic', stage: 'created', next_suggested: null,
      target_kicad: null, target_installed: null, hashes: {}, epochs: { sch: 20260101, pcb: 20260206 },
      files: { sch: ['pic.kicad_sch'], pcb: ['pic.kicad_pcb'] }, editor_locks: [], snapshot_head: null, external_edit: null, history: [],
      project_kicad: '10.0', install_hint: null,
      candidates: [{ version: '10.0.6', matches: true }],
    } as ProjectStatusData;
    const { info, writers } = proactiveUpgradeInfo(st.project, st, [stable], '10.99.0');
    assert.deepEqual(writers, ['10.0.6']);
    assert.equal(info.writer, '10.0.6');
    assert.deepEqual(upgradePrompt(info).buttons, ['Upgrade', 'Use KiCad 10.0.6', 'Cancel']);
  });

  it('helpers: hint versions, major.minor, project names and keys', () => {
    assert.equal(hintVersion('winget install KiCad.KiCad --version 10.0.6'), '10.0.6');
    assert.equal(hintVersion('winget install --id KiCad.KiCad --version=9.0.9 -e'), '9.0.9');
    assert.equal(hintVersion('download KiCad 8.0.9 from kicad.org'), '8.0.9');
    assert.equal(hintVersion(null), null);
    assert.equal(majorMinor('10.0.6'), '10.0');
    assert.equal(majorMinor('10.99.0-rc1'), '10.99');
    assert.equal(majorMinor('nightly'), 'nightly');
    assert.equal(projectNameOf('C:\\work\\kit\\kit.kicad_pro'), 'kit');
    assert.equal(projectNameOf('C:/work/pic_programmer/'), 'pic_programmer');
    assert.equal(projectKey('C:\\Work\\Kit\\kit.kicad_pro'), projectKey('c:/work/kit/'));
  });

  it('E_FORMAT_NEWER names the KiCad that is needed; E_READONLY says nothing changed', () => {
    const info = formatErrorInfo({ code: 'E_FORMAT_NEWER', data: { project_epoch: { pcb: 20270101 }, candidates: [{ version: '10.99.0', formats: {} }, { version: '10.0.6', formats: {} }], needed_kicad: '11.0' } }, { project: 'C:/w/next' });
    assert.equal(
      newerMessage(info, { message: 'E_FORMAT_NEWER: newer' }),
      'kiwrap: next is in KiCad 11.0 format, newer than every installed KiCad (10.99.0, 10.0.6). Install KiCad 11.0 or newer to work on it.',
    );
    assert.equal(
      readonlyMessage('kw_pcb_drc', { message: 'E_READONLY: save_board needs a writable server', fix: 'restart without --readonly' }),
      'kiwrap is in read-only mode: kw_pcb_drc changed nothing (save_board needs a writable server). restart without --readonly',
    );
  });
});

describe('consent: modal flow through McpClient (fake server, stubbed window)', function () {
  this.timeout(30000);
  let server: KiwrapServer;
  let ui: StubUi;
  let upgradedHook: string[];

  const start = async (answer: (buttons: string[]) => string | undefined, modalDelayMs = 0) => {
    ui = new StubUi(answer, modalDelayMs);
    upgradedHook = [];
    const consent = new FormatConsent(ui, { onProjectChanged: (p) => upgradedHook.push(p) });
    server = new KiwrapServer({ command: process.execPath, args: [FAKE], env: { ...(process.env as Record<string, string>) } }, { onToolError: consent.handle });
    await server.start();
  };
  const recorded = async () => {
    const r = await server.client.call<{ calls: { tool: string; args: Record<string, unknown> }[] }>('fake_calls');
    return r.data.calls;
  };

  afterEach(async () => {
    await server?.stop();
  });

  it('Upgrade: kw_project upgrade(confirm=true) runs, then the original DRC is retried and succeeds', async () => {
    await start((b) => b[0]);
    const r = await server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true });
    assert.equal(r.data.must_fix, 1, 'the retried DRC result comes back');
    assert.equal(ui.modals.length, 1);
    assert.match(ui.modals[0].message, /^oldfmt is in KiCad 10\.0 format and no installed KiCad 10\.0 can write it\. Upgrade the project files to KiCad 10\.99 format\? KiCad 10\.0 will no longer open them\.$/);
    const calls = await recorded();
    const tools = calls.map((c) => `${c.tool}${c.args.action ? `:${c.args.action}` : ''}`);
    assert.deepEqual(tools, ['kw_pcb_drc', 'kw_project:upgrade', 'kw_pcb_drc']);
    const upgrade = calls[1].args;
    assert.equal(upgrade.confirm, true);
    assert.equal(upgrade.to_kicad, '10.99.0');
    assert.equal(upgrade.project, 'C:/fake/oldfmt/oldfmt.kicad_pro');
    assert.equal(ui.infos.length, 1);
    assert.match(ui.infos[0], /upgraded oldfmt to KiCad 10\.99 format \(erc 2 -> 2, drc 5 -> 5\); snapshot snap-pre-upgrade/);
    assert.deepEqual(upgradedHook, ['C:/fake/oldfmt/oldfmt.kicad_pro']);
  });

  it('an installed KiCad writes the format (guard rollback): "Use KiCad <v>" pins it and the call is retried', async () => {
    await start((b) => b[1]);
    const r = await server.pcbDrc('C:/fake/guardfmt/guardfmt.kicad_pro', { save_board: true });
    assert.equal(r.data.must_fix, 1);
    assert.equal(ui.modals.length, 1);
    assert.equal(
      ui.modals[0].message,
      'guardfmt is in KiCad 10.0 format and installed KiCad 10.0.6 still writes it. Upgrade the project files to KiCad 10.99 format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(ui.modals[0].buttons, ['Upgrade', 'Use KiCad 10.0.6', 'Cancel']);
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => `${c.tool}${c.args.action ? `:${c.args.action}` : ''}`), ['kw_pcb_drc', 'kw_project:retarget', 'kw_pcb_drc']);
    assert.equal(calls[1].args.target_kicad, '10.0.6');
    assert.equal(calls.filter((c) => c.args.action === 'upgrade').length, 0, 'no upgrade');
    assert.match(ui.infos[0], /guardfmt now runs on KiCad 10\.0\.6; its files keep their format/);
  });

  it('Open Live KiCad (kw_live launch) gets the same modal and continues after the upgrade', async () => {
    await start((b) => b[0]);
    const r = await server.live({ action: 'launch', project: 'C:/fake/oldfmt', editor: 'pcb', mode: 'view' });
    assert.equal(r.data.pid, 4242);
    assert.equal(ui.modals.length, 1);
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => c.tool), ['kw_live', 'kw_project', 'kw_live']);
  });

  it('Cancel: no upgrade, the error is rethrown marked handled (no second popup)', async () => {
    await start(() => undefined);
    await assert.rejects(server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError);
      assert.equal(err.code, 'E_FORMAT_UPGRADE_REQUIRED');
      assert.ok(isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.equal(ui.errors.length, 0);
    assert.deepEqual((await recorded()).map((c) => c.tool), ['kw_pcb_drc']);
  });

  it('Install KiCad <x>...: hands the install hint to the terminal and does not upgrade', async () => {
    await start((b) => b.find((x) => x.startsWith('Install KiCad')));
    await assert.rejects(server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true }), (err: unknown) => isHandledError(err));
    assert.deepEqual(ui.installs, [{ hint: 'winget install KiCad.KiCad --version 10.0.6', version: '10.0.6' }]);
    assert.deepEqual((await recorded()).map((c) => c.tool), ['kw_pcb_drc']);
  });

  it('a background (non-interactive) call never opens the modal and is not marked handled', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: false }, { interactive: false }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_FORMAT_UPGRADE_REQUIRED');
      assert.equal(isHandledError(err), false);
      return true;
    });
    assert.equal(ui.modals.length, 0);
  });

  it('concurrent calls on one project share one modal and both retry after the upgrade', async () => {
    await start((b) => b[0], 300);
    const [a, b] = await Promise.all([
      server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true }),
      server.live({ action: 'launch', project: 'C:/fake/oldfmt', editor: 'pcb', mode: 'view' }),
    ]);
    assert.equal(a.data.must_fix, 1);
    assert.equal(b.data.pid, 4242);
    assert.equal(ui.modals.length, 1, 'one question per project');
    assert.equal((await recorded()).filter((c) => c.tool === 'kw_project').length, 1, 'one upgrade');
  });

  it('a retry that still needs an upgrade is not asked about twice', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/oldstuck/oldstuck.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_FORMAT_UPGRADE_REQUIRED');
      return true;
    });
    assert.equal(ui.modals.length, 1);
  });

  it('a failed upgrade is shown once and rethrown handled', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/upgradefail/upgradefail.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_UPGRADE_FAILED');
      assert.ok(isHandledError(err));
      return true;
    });
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /upgrading upgradefail failed: E_UPGRADE_FAILED: kicad-cli pcb upgrade failed \(rc 3\) - restore the snapshot/);
    assert.equal(ui.infos.length, 0);
  });

  it('E_FORMAT_NEWER: an error naming the needed KiCad (with an install button), no modal', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/newerfmt/newerfmt.kicad_pro', {}), (err: unknown) => isHandledError(err));
    assert.equal(ui.modals.length, 0);
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /newerfmt is in KiCad 11\.0 format, newer than every installed KiCad \(10\.99\.0, 10\.0\.6\)\. Install KiCad 11\.0 or newer/);
    assert.deepEqual(ui.errors[0].buttons, ['Install KiCad 11.0\u2026']);
  });

  it('E_FORMAT_NEWER with an installed reader: "Use KiCad <v>" pins the project via kw_project retarget', async () => {
    await start((b) => b[0]);
    ui.errorAnswer = (b) => b[0];
    await assert.rejects(server.pcbDrc('C:/fake/newerpin/newerpin.kicad_pro', {}), (err: unknown) => isHandledError(err));
    assert.equal(ui.errors.length, 1);
    assert.equal(
      ui.errors[0].message,
      'kiwrap: newerpin is in KiCad 10.99 format, newer than KiCad 10.0.6. Installed KiCad 10.99.0 reads it: use that one.',
    );
    assert.deepEqual(ui.errors[0].buttons, ['Use KiCad 10.99.0']);
    // the button's action runs after the call already failed
    for (let i = 0; i < 50 && ui.infos.length === 0; i += 1) {
      await new Promise((r) => setTimeout(r, 50));
    }
    const retarget = (await recorded()).find((c) => c.tool === 'kw_project');
    assert.deepEqual(retarget?.args, { action: 'retarget', project: 'C:/fake/newerpin/newerpin.kicad_pro', target_kicad: '10.99.0' });
    assert.match(ui.infos[0], /newerpin now runs on KiCad 10\.99\.0/);
    assert.deepEqual(upgradedHook, ['C:/fake/newerpin/newerpin.kicad_pro']);
  });

  it('E_READONLY: an information message, no modal, no error popup', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/readonly/readonly.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_READONLY' && isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 0);
    assert.equal(ui.errors.length, 0);
    assert.equal(ui.infos.length, 1);
    assert.match(ui.infos[0], /^kiwrap is in read-only mode: kw_pcb_drc changed nothing .*restart kiwrap serve without --readonly/);
  });

  it('other teaching errors pass through untouched', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.schCheck('C:/fake/missing.kicad_pro'), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_PROJECT_NOT_FOUND');
      assert.equal(isHandledError(err), false);
      return true;
    });
    assert.equal(ui.modals.length + ui.errors.length + ui.infos.length, 0);
  });

  it('kw_project status carries the D21 fields; retarget pins without an upgrade', async () => {
    await start(() => undefined);
    const st = await server.projectStatus('C:/fake/oldfmt/oldfmt.kicad_pro');
    assert.equal(st.data.target_effective, '10.99.0');
    assert.equal(st.data.target_source, 'file_epoch_newer_readonly');
    assert.equal(st.data.epoch_match, false);
    assert.equal(st.data.warnings?.length, 1);
    const rt = await server.projectRetarget('C:/fake/oldfmt/oldfmt.kicad_pro', '10.0.6');
    assert.equal(rt.data.target_kicad, '10.0.6');
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => c.args.action), ['status', 'retarget']);
  });

  it('the proactive upgrade (FormatConsent.decide) asks, then upgrades with confirm=true', async () => {
    ui = new StubUi((b) => b[0]);
    const consent = new FormatConsent(ui);
    server = new KiwrapServer({ command: process.execPath, args: [FAKE], env: { ...(process.env as Record<string, string>) } }, { onToolError: consent.handle });
    await server.start();
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { project: 'C:/fake/oldfmt/oldfmt.kicad_pro', project_epoch: { pcb: 20260206 }, upgrade_to: '10.99.0' } });
    assert.equal(await consent.decide(info, (n, a, o) => server.client.call(n, a, o)), 'upgraded');
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => [c.tool, c.args.confirm]), [['kw_project', true]]);
  });
});
