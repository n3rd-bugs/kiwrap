/**
 * The format-upgrade consent flow (D21, amended by D24; review item 1 / B1): the
 * E_FORMAT_UPGRADE_REQUIRED modal, [Upgrade] -> kw_project_upgrade(**upgrade_args) -> retry once,
 * Install KiCad <x>..., Use KiCad <v>, Cancel, the kw_project_upgrade failures (mismatch, read-only,
 * unsupported, a failed conversion), the proactive command (status -> modal -> kw_project_upgrade),
 * E_FORMAT_NEWER and E_READONLY; driven through McpClient's onToolError against the fake stdio
 * server with a stubbed window API.
 */
import * as assert from 'node:assert/strict';
import * as path from 'node:path';
import {
  E_FORMAT_UPGRADE_REQUIRED,
  FormatConsent,
  formatErrorInfo,
  formatLabel,
  hasMismatch,
  hintVersion,
  majorMinor,
  newerMessage,
  proactiveUpgradeInfo,
  projectKey,
  projectNameOf,
  readonlyMessage,
  upgradePrompt,
  upgradeSummary,
  upgradeTarget,
  type ConsentUi,
} from '../../src/consent';
import { MODAL_CONSENT, type CallOptions } from '../../src/mcpClient';
import { KiwrapServer } from '../../src/kiwrapServer';
import {
  asUpgradeArgs,
  KiwrapError,
  isHandledError,
  upgradeCallArgs,
  type InstallRow,
  type ProjectStatusData,
  type Receipt,
} from '../../src/types';

const FAKE = path.join(__dirname, 'fakeServer.js');

const UPGRADE_ARGS = {
  project: 'C:/work/kit/kit.kicad_pro',
  from_kicad: '10.0',
  to_kicad: '10.99.0',
  to_format: { sch: 20260830, pcb: 20260901 },
  files: ['kit.kicad_pcb', 'kit.kicad_sch', 'sub/child.kicad_sch'],
};

const UPGRADE_DATA = {
  files: [
    { path: 'C:/work/kit/kit.kicad_pcb', kind: 'pcb', stamp: 20260206 },
    { path: 'C:/work/kit/kit.kicad_sch', kind: 'sch', stamp: 20260206 },
    { path: 'C:/work/kit/sub/child.kicad_sch', kind: 'sch', stamp: 20260101 },
  ],
  project_epoch: { pcb: 20260206, sch: 20260206 },
  candidates: [{ version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } }],
  upgrade_to: '10.99.0',
  install_hint: 'winget install KiCad.KiCad --version 10.0.6',
  upgrade_args: UPGRADE_ARGS,
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

describe('consent: error data and texts (D21, D24)', () => {
  it('normalises the E_FORMAT_UPGRADE_REQUIRED data, upgrade_args included, and names the release', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: UPGRADE_DATA }, { project: 'C:/work/kit' });
    assert.equal(info.project, 'C:/work/kit/kit.kicad_pro', 'upgrade_args.project names the .kicad_pro');
    assert.equal(info.projectName, 'kit');
    assert.equal(info.files.length, 3);
    assert.deepEqual(info.projectEpoch, { pcb: 20260206, sch: 20260206 });
    assert.equal(info.upgradeTo, '10.99.0');
    assert.deepEqual(info.upgradeArgs, UPGRADE_ARGS);
    assert.equal(info.installVersion, '10.0.6');
    assert.equal(info.formatKicad, '10.0');
    assert.equal(formatLabel(info), 'KiCad 10.0');
  });

  it('the modal reads as the owner asked and lists from, to and exactly the upgrade_args files', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: UPGRADE_DATA }, { project: 'C:/work/kit' });
    const p = upgradePrompt(info);
    assert.equal(
      p.message,
      'kit is in KiCad 10.0 format and no installed KiCad 10.0 can write it. Upgrade the project files to KiCad 10.99 format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad 10.0\u2026', 'Cancel']);
    const lines = p.detail.split('\n');
    assert.equal(lines[0], 'From: KiCad 10.0');
    assert.equal(lines[1], 'To: KiCad 10.99.0 (sch 20260830, pcb 20260901)');
    assert.equal(lines[2], 'Files that will be converted (3):');
    assert.deepEqual(lines.slice(3, 6), [
      '  kit.kicad_pcb (pcb, format 20260206)',
      '  kit.kicad_sch (sch, format 20260206)',
      '  sub/child.kicad_sch (sch, format 20260101)',
    ]);
    assert.match(p.detail, /Never converted: \.kicad_pro, \.kicad_prl, \.kicad_dru/);
    assert.match(p.detail, /Installed: KiCad 10\.99\.0 \(sch 20260830, pcb 20260901\)/);
    assert.match(p.detail, /snapshot/);
    assert.match(p.detail, /winget install KiCad\.KiCad --version 10\.0\.6/);
    assert.doesNotMatch(p.detail, /confirm/);
  });

  it('without upgrade_args (an older server) the modal offers no [Upgrade]', () => {
    const { upgrade_args: _drop, ...legacy } = UPGRADE_DATA;
    void _drop;
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: legacy }, { project: 'C:/work/kit/kit.kicad_pro' });
    assert.equal(info.upgradeArgs, null);
    const p = upgradePrompt(info);
    assert.deepEqual(p.buttons, ['Install KiCad 10.0\u2026', 'Cancel']);
    assert.match(p.detail, /kit\.kicad_pcb \(pcb, format 20260206\)/);
    assert.match(p.detail, /no upgrade_args/);
  });

  it('falls back to the format stamp when the release is unknown, and prefers an explicit project_kicad', () => {
    const bare = formatErrorInfo(
      {
        code: E_FORMAT_UPGRADE_REQUIRED,
        data: {
          project_epoch: { pcb: 20250114 },
          upgrade_args: { project: 'C:/a/b.kicad_pro', from_kicad: 'format sch=20250114,pcb=20250114', to_kicad: '10.99.0', files: ['b.kicad_pcb'] },
        },
      },
      { project: 'C:/a/b.kicad_pro' },
    );
    assert.equal(bare.formatKicad, null, 'a format from_kicad names no release');
    const p = upgradePrompt(bare);
    assert.match(p.message, /^b is in KiCad file format 20250114 format and no installed KiCad can write it\. Upgrade the project files to KiCad 10\.99 format\?/);
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad\u2026', 'Cancel']);
    assert.equal(p.detail.split('\n')[0], 'From: KiCad file format sch 20250114, pcb 20250114');
    const named = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { ...UPGRADE_DATA, project_kicad: '9.0.9' } });
    assert.equal(named.formatKicad, '9.0');
    const mixed = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { files: [{ path: 'x.kicad_sch', kind: 'sch', stamp: 20250114 }, { path: 'x.kicad_pcb', kind: 'pcb', stamp: 20241229 }] } });
    assert.deepEqual(mixed.projectEpoch, { sch: 20250114, pcb: 20241229 });
    assert.equal(formatLabel(mixed), 'KiCad file format sch 20250114, pcb 20241229');
  });

  it('a to_kicad install root (installs sharing a version) is named by the candidate writing to_format', () => {
    const root = 'C:/kiwrap-builds/10.99-638d03a55e';
    const info = formatErrorInfo({
      code: E_FORMAT_UPGRADE_REQUIRED,
      data: {
        ...UPGRADE_DATA,
        upgrade_to: null,
        candidates: [
          { version: '10.99.0', formats: { sch: 20260801, pcb: 20260801 } },
          { version: '10.99.1', formats: { sch: 20260830, pcb: 20260901 } },
        ],
        upgrade_args: { ...UPGRADE_ARGS, to_kicad: root },
      },
    });
    assert.equal(upgradeTarget(info), '10.99.1');
    assert.equal(upgradePrompt(info).detail.split('\n')[1], `To: KiCad 10.99.1 at ${root} (sch 20260830, pcb 20260901)`);
  });

  it('the proactive prompt names an installed KiCad that still writes the format', () => {
    const upgradeArgs = { project: 'C:/w/ecc83/ecc83-pp.kicad_pro', from_kicad: '9.0', to_kicad: '10.99.0', files: ['ecc83-pp.kicad_sch'] };
    const info = formatErrorInfo(
      { code: E_FORMAT_UPGRADE_REQUIRED, data: { project_epoch: { sch: 20250114, pcb: 20241229 }, project_kicad: '9.0.9', upgrade_to: '10.99.0', upgrade_args: upgradeArgs } },
      { project: 'C:/w/ecc83/ecc83-pp.kicad_pro' },
    );
    info.writer = '9.0.9';
    assert.equal(
      upgradePrompt(info).message,
      'ecc83-pp is in KiCad 9.0 format and installed KiCad 9.0.9 still writes it. Upgrade the project files to KiCad 10.99 format? KiCad 9.0 will no longer open them.',
    );
    // without upgrade_args (review finding 8: the target converts nothing) the modal offers no upgrade
    const noArgs = { ...info, upgradeArgs: null };
    assert.equal(
      upgradePrompt(noArgs).message,
      'ecc83-pp is in KiCad 9.0 format and installed KiCad 9.0.9 still writes it; a write by KiCad 10.99.0 would have converted it, so nothing was saved.',
    );
    assert.deepEqual(upgradePrompt(noArgs).buttons, ['Use KiCad 9.0.9', 'Cancel']);
  });

  it('proactive info from kw_project status, no installed writer (fixer finding 3): KiCad 10.0, the winget command, upgrade_args', () => {
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
      upgrade_args: {
        project: 'C:/w/pic_programmer/pic_programmer.kicad_pro', from_kicad: '10.0', to_kicad: '10.99.0',
        to_format: { sch: 20260830, pcb: 20260901 },
        files: ['pic_programmer.kicad_pcb', 'pic_programmer.kicad_sch', 'pic_sockets.kicad_sch'],
      },
    } as ProjectStatusData;
    const { info, writers } = proactiveUpgradeInfo(st.project, st, [nightly]);
    assert.deepEqual(writers, []);
    assert.equal(info.code, 'status', 'no error is faked for the proactive command');
    assert.equal(info.writer, null);
    assert.equal(info.formatKicad, '10.0');
    assert.equal(info.installHint, 'winget install KiCad.KiCad --version 10.0.6');
    assert.deepEqual(info.upgradeArgs, st.upgrade_args);
    const p = upgradePrompt(info);
    assert.equal(
      p.message,
      'pic_programmer is in KiCad 10.0 format and no installed KiCad 10.0 can write it. Upgrade the project files to KiCad 10.99 format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(p.buttons, ['Upgrade', 'Install KiCad 10.0\u2026', 'Cancel']);
    assert.match(p.detail, /winget install KiCad\.KiCad --version 10\.0\.6/);
    assert.match(p.detail, /^ {2}pic_sockets\.kicad_sch \(sch, format 20260101\)$/m);
    // an older server without the fields still names the format by its stamps, but cannot upgrade
    const bare = proactiveUpgradeInfo(st.project, { ...st, project_kicad: undefined, install_hint: undefined, file_epochs: undefined, candidates: undefined, upgrade_args: undefined }, [nightly]);
    assert.equal(bare.info.formatKicad, null);
    assert.equal(bare.info.upgradeArgs, null);
    assert.match(upgradePrompt(bare.info).message, /^pic_programmer is in KiCad file format sch 20260101, pcb 20260206 format/);
  });

  it("proactive info: the server's generation match names an installed writer the exact stamps miss", () => {
    // 10.0 demo sheets are stamped 20260101; 10.0.6 writes 20260306 - the server still says it matches
    const stable: InstallRow = {
      version: '10.0.6', major_minor: '10.0', root: 'C:/Program Files/KiCad/10.0', kicad_cli: 'C:/Program Files/KiCad/10.0/bin/kicad-cli.exe',
      share: 'C:/Program Files/KiCad/10.0/share/kicad', source: 'registry', python: null,
      capabilities: { 'formats.sch': 20260306, 'formats.pcb': 20260206 },
    };
    const st = {
      ok: true, project: 'C:/w/pic/pic.kicad_pro', dir: 'C:/w/pic', name: 'pic', stage: 'created', next_suggested: null,
      target_kicad: '10.99.0', target_installed: true, hashes: {}, epochs: { sch: 20260101, pcb: 20260206 },
      files: { sch: ['pic.kicad_sch'], pcb: ['pic.kicad_pcb'] }, editor_locks: [], snapshot_head: null, external_edit: null, history: [],
      project_kicad: '10.0', install_hint: null,
      candidates: [{ version: '10.0.6', matches: true }],
      upgrade_args: { project: 'C:/w/pic/pic.kicad_pro', from_kicad: '10.0', to_kicad: '10.99.0', files: ['pic.kicad_pcb', 'pic.kicad_sch'] },
    } as ProjectStatusData;
    const { info, writers } = proactiveUpgradeInfo(st.project, st, [stable]);
    assert.deepEqual(writers, ['10.0.6']);
    assert.equal(info.writer, '10.0.6');
    assert.deepEqual(upgradePrompt(info).buttons, ['Upgrade', 'Use KiCad 10.0.6', 'Cancel']);
  });

  it('upgrade_args helpers: well-formed only; the call sends exactly the four arguments', () => {
    assert.deepEqual(asUpgradeArgs(UPGRADE_ARGS), UPGRADE_ARGS);
    assert.equal(asUpgradeArgs(null), null);
    assert.equal(asUpgradeArgs({ ...UPGRADE_ARGS, files: 'kit.kicad_pcb' }), null);
    assert.equal(asUpgradeArgs({ ...UPGRADE_ARGS, from_kicad: undefined }), null);
    // review finding 8: empty files is "nothing to convert" on the server - never an [Upgrade]
    assert.equal(asUpgradeArgs({ ...UPGRADE_ARGS, files: [] }), null);
    const empty = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: { ...UPGRADE_DATA, upgrade_args: { ...UPGRADE_ARGS, files: [] } } });
    assert.equal(empty.upgradeArgs, null);
    assert.ok(!upgradePrompt(empty).buttons.includes('Upgrade'));
    assert.deepEqual(upgradeCallArgs(UPGRADE_ARGS), {
      project: 'C:/work/kit/kit.kicad_pro',
      from_kicad: '10.0',
      to_kicad: '10.99.0',
      files: ['kit.kicad_pcb', 'kit.kicad_sch', 'sub/child.kicad_sch'],
    });
  });

  it('the upgrade summary names the files, ERC/DRC before -> after, dropped stages and the undo', () => {
    const info = formatErrorInfo({ code: E_FORMAT_UPGRADE_REQUIRED, data: UPGRADE_DATA });
    const r: Receipt<Record<string, unknown>> = {
      ok: true, changed: { added: [], updated: [], removed: [] }, validators: [], provider_used: '', degraded: [], snapshot: 'snap-x',
      stage: null, next_suggested: 'kw_sch_check', warnings: [], image_info: null,
      data: {
        files: [
          { path: 'kit.kicad_pcb', kind: 'pcb', stamp_before: 20260206, stamp_after: 20260901, changed: true },
          { path: 'kit.kicad_sch', kind: 'sch', stamp_before: 20260830, stamp_after: 20260830, changed: false },
        ],
        checks: { before: { erc: 2, drc: { errors: 5 } }, after: { erc: 3, drc: { errors: 5 } } },
        snapshot_before: 'snap-before',
        stage_dropped: ['erc_clean'],
        target: { kicad: '10.99.0', source: 'pinned', epoch_match: true },
      },
    };
    assert.equal(
      upgradeSummary(info, r),
      'kiwrap: upgraded kit to KiCad 10.99 format (1 file; erc 2 -> 3, drc 5 -> 5); stage erc_clean dropped; kw_snapshot restore snap-before undoes it.',
    );
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

type Call = { tool: string; args: Record<string, unknown> };

describe('consent: modal flow through McpClient (fake server, stubbed window)', function () {
  this.timeout(30000);
  let server: KiwrapServer;
  let ui: StubUi;
  let changedHook: string[];

  const start = async (answer: (buttons: string[]) => string | undefined, modalDelayMs = 0, fakeArgs: string[] = []) => {
    ui = new StubUi(answer, modalDelayMs);
    changedHook = [];
    const consent = new FormatConsent(ui, { onProjectChanged: (p) => changedHook.push(p) });
    server = new KiwrapServer({ command: process.execPath, args: [FAKE, ...fakeArgs], env: { ...(process.env as Record<string, string>) } }, { onToolError: consent.handle });
    await server.start();
    return consent;
  };
  const recorded = async (): Promise<Call[]> => {
    const r = await server.client.call<{ calls: Call[] }>('fake_calls');
    return r.data.calls;
  };
  const names = (calls: Call[]) => calls.map((c) => `${c.tool}${c.args.action ? `:${c.args.action}` : ''}`);
  const oldfmtArgs = (name: string) => ({
    project: `C:/fake/${name}/${name}.kicad_pro`,
    from_kicad: '10.0',
    to_kicad: '10.99.0',
    files: [`${name}.kicad_pcb`, `${name}.kicad_sch`, 'sub/child.kicad_sch'],
  });

  afterEach(async () => {
    await server?.stop();
  });

  it('Upgrade: kw_project_upgrade with exactly error.data.upgrade_args, then the original DRC is retried once', async () => {
    await start((b) => b[0]);
    const r = await server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true });
    assert.equal(r.data.must_fix, 1, 'the retried DRC result comes back');
    assert.equal(ui.modals.length, 1);
    assert.match(ui.modals[0].message, /^oldfmt is in KiCad 10\.0 format and no installed KiCad 10\.0 can write it\. Upgrade the project files to KiCad 10\.99 format\? KiCad 10\.0 will no longer open them\.$/);
    assert.match(ui.modals[0].detail, /^From: KiCad 10\.0\nTo: KiCad 10\.99\.0 \(sch 20260830, pcb 20260901\)\nFiles that will be converted \(3\):\n {2}oldfmt\.kicad_pcb \(pcb, format 20260206\)/);
    const calls = await recorded();
    assert.deepEqual(names(calls), ['kw_pcb_drc', 'kw_project_upgrade', 'kw_pcb_drc']);
    assert.deepEqual(calls[1].args, oldfmtArgs('oldfmt'));
    assert.ok(calls.every((c) => !('confirm' in c.args)), 'no call carries confirm (D24)');
    assert.equal(ui.infos.length, 1);
    assert.equal(ui.infos[0], 'kiwrap: upgraded oldfmt to KiCad 10.99 format (3 files; erc 2 -> 2, drc 5 -> 5); kw_snapshot restore snap-pre-upgrade undoes it.');
    assert.ok(ui.logs.some((l) => /kw_project_upgrade: 10\.0 can no longer open/.test(l)), 'the receipt warning is logged');
    assert.deepEqual(changedHook, ['C:/fake/oldfmt/oldfmt.kicad_pro']);
    assert.equal(ui.errors.length, 0);
  });

  it('an error without upgrade_args (older server): kw_project status supplies them before the modal', async () => {
    await start((b) => b[0]);
    const r = await server.pcbDrc('C:/fake/legacyfmt/legacyfmt.kicad_pro', { save_board: true });
    assert.equal(r.data.must_fix, 1);
    const calls = await recorded();
    assert.deepEqual(names(calls), ['kw_pcb_drc', 'kw_project:status', 'kw_project_upgrade', 'kw_pcb_drc']);
    assert.deepEqual(calls[2].args, oldfmtArgs('legacyfmt'));
    assert.equal(ui.modals.length, 1);
    assert.deepEqual(ui.modals[0].buttons[0], 'Upgrade');
    assert.match(ui.modals[0].detail, /^ {2}sub\/child\.kicad_sch/m);
  });

  it('an installed KiCad writes the format (guard rollback): no [Upgrade], "Use KiCad <v>" pins it and the call is retried', async () => {
    // review finding 8: the real server's guard refusal carries upgrade_args null here (the target
    // converts nothing) and status has none either, so an [Upgrade] could only fail
    await start((b) => b.find((x) => x.startsWith('Use KiCad')));
    const r = await server.pcbDrc('C:/fake/guardfmt/guardfmt.kicad_pro', { save_board: true });
    assert.equal(r.data.must_fix, 1);
    assert.equal(ui.modals.length, 1);
    assert.equal(
      ui.modals[0].message,
      'guardfmt is in KiCad 10.0 format and installed KiCad 10.0.6 still writes it; a write by KiCad 10.99.0 would have converted it, so nothing was saved.',
    );
    assert.deepEqual(ui.modals[0].buttons, ['Use KiCad 10.0.6', 'Cancel']);
    assert.doesNotMatch(ui.modals[0].detail, /no upgrade_args/, 'not blamed on an old server');
    assert.match(ui.modals[0].detail, /^Keep the files as they are and run the project on KiCad 10\.0\.6/m);
    const calls = await recorded();
    assert.deepEqual(names(calls), ['kw_pcb_drc', 'kw_project:status', 'kw_project:retarget', 'kw_pcb_drc']);
    assert.equal(calls[2].args.target_kicad, '10.0.6');
    assert.equal(calls.filter((c) => c.tool === 'kw_project_upgrade').length, 0, 'no upgrade');
    assert.match(ui.infos[0], /guardfmt now runs on KiCad 10\.0\.6; its files keep their format/);
  });

  it('Open Live KiCad (kw_live launch) gets the same modal and continues after the upgrade', async () => {
    await start((b) => b[0]);
    const r = await server.live({ action: 'launch', project: 'C:/fake/oldfmt', editor: 'pcb', mode: 'view' });
    assert.equal(r.data.pid, 4242);
    assert.equal(ui.modals.length, 1);
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => c.tool), ['kw_live', 'kw_project_upgrade', 'kw_live']);
    assert.deepEqual(calls[1].args, oldfmtArgs('oldfmt'), 'a directory argument still upgrades the .kicad_pro upgrade_args names');
  });

  it('kw_live launch with the selected install (target_kicad) on a project in its own KiCad\'s format: pin, upgrade, retry (review findings 2/3)', async () => {
    await start((b) => b[0]);
    const pro = 'C:/fake/livepref/livepref.kicad_pro';
    const r = await server.live({ action: 'launch', project: pro, editor: 'pcb', mode: 'view', target_kicad: '10.99.0' });
    assert.equal(r.data.pid, 4242, 'the retried launch comes back');
    assert.equal(ui.modals.length, 1);
    assert.equal(
      ui.modals[0].message,
      'livepref runs on KiCad 10.0.6; the selected KiCad 10.99.0 would convert its KiCad 10.0-format files. Pin the project to KiCad 10.99.0 and upgrade the files to its format? KiCad 10.0 will no longer open them.',
    );
    assert.deepEqual(ui.modals[0].buttons, ['Upgrade', 'Cancel'], 'no "Use KiCad 10.0.6": the retry asks for 10.99.0 again');
    assert.match(ui.modals[0].detail, /^From: KiCad 10\.0\nTo: KiCad 10\.99\.0 \(sch 20260830, pcb 20260901\)\nFiles that will be converted \(3\):/);
    assert.match(ui.modals[0].detail, /first pins the project to KiCad 10\.99\.0 \(kw_project retarget, no file changes\)/);
    const calls = await recorded();
    assert.deepEqual(names(calls), ['kw_live:launch', 'kw_project:retarget', 'kw_project_upgrade', 'kw_live:launch']);
    assert.deepEqual(calls[1].args, { action: 'retarget', project: pro, target_kicad: '10.99.0' });
    assert.deepEqual(calls[2].args, oldfmtArgs('livepref'), 'exactly retarget_first.upgrade_args, verbatim');
    assert.equal(ui.errors.length, 0);
    assert.match(ui.infos[0], /^kiwrap: upgraded livepref to KiCad 10\.99 format/);
  });

  it('a failed upgrade after the retarget-first pin puts the pin back and never retries', async () => {
    await start((b) => b[0]);
    const pro = 'C:/fake/liveprefail/liveprefail.kicad_pro';
    await assert.rejects(server.live({ action: 'launch', project: pro, editor: 'pcb', mode: 'view', target_kicad: '10.99.0' }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_UPGRADE_FAILED' && isHandledError(err));
      return true;
    });
    const calls = await recorded();
    assert.deepEqual(names(calls), ['kw_live:launch', 'kw_project:retarget', 'kw_project_upgrade', 'kw_project:retarget']);
    assert.equal(calls[3].args.target_kicad, 'auto', 'the project had no pin: back to auto');
    assert.equal(ui.modals.length, 1);
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /upgrading liveprefail failed: E_UPGRADE_FAILED/);
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
    assert.deepEqual((await recorded()).map((c) => c.tool), ['kw_pcb_drc']);
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
    assert.equal((await recorded()).filter((c) => c.tool === 'kw_project_upgrade').length, 1, 'one upgrade');
  });

  it('a retry that still needs an upgrade is not asked about twice', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/oldstuck/oldstuck.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_FORMAT_UPGRADE_REQUIRED');
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade', 'kw_pcb_drc']);
  });

  it('a failed upgrade is shown once, rethrown handled, and the original call is not retried', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/upgradefail/upgradefail.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_UPGRADE_FAILED');
      assert.ok(isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /upgrading upgradefail failed: E_UPGRADE_FAILED: kicad-cli pcb upgrade failed \(rc 3\) - restore the snapshot/);
    assert.equal(ui.infos.length, 0);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade']);
  });

  it('a failed upgrade never reopens the modal, even when kw_project_upgrade itself answers E_FORMAT_UPGRADE_REQUIRED', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/upgradeloop/upgradeloop.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_FORMAT_UPGRADE_REQUIRED' && isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 1, 'one modal');
    assert.equal(ui.errors.length, 1);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade']);
  });

  it('an E_BAD_ARGS mismatch says the project changed, once, with no second modal and no retry', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/changedfmt/changedfmt.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_BAD_ARGS' && isHandledError(err));
      assert.ok('mismatch' in err.data);
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.deepEqual(ui.errors.map((e) => e.message), ['kiwrap: the project changedfmt changed; run the command again. Nothing was converted.']);
    assert.equal(ui.infos.length, 0);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade']);
    assert.deepEqual(changedHook, ['C:/fake/changedfmt/changedfmt.kicad_pro'], 'the status is refreshed');
  });

  it('"nothing to convert" (mismatch null) is not "the project changed": the failure and its retarget fix are shown (review finding 9)', async () => {
    const consent = await start((b) => b[0]);
    const info = formatErrorInfo(
      { code: E_FORMAT_UPGRADE_REQUIRED, data: { ...UPGRADE_DATA, upgrade_args: { ...UPGRADE_ARGS, project: 'C:/fake/nothingfmt/nothingfmt.kicad_pro' } } },
      { project: 'C:/fake/nothingfmt/nothingfmt.kicad_pro' },
    );
    await assert.rejects(consent.decide(info, (n, a, o) => server.client.call(n, a, o)), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_BAD_ARGS' && isHandledError(err));
      assert.equal(err.data.mismatch, null);
      return true;
    });
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /^kiwrap: upgrading nothingfmt failed: E_BAD_ARGS: nothing to convert: .* - no upgrade is needed\. .*kw_project\(action='retarget'/);
    assert.doesNotMatch(ui.errors[0].message, /changed; run the command again/);
    assert.equal(hasMismatch({ mismatch: null }), false);
    assert.equal(hasMismatch({ mismatch: {} }), false);
    assert.equal(hasMismatch({ mismatch: { files: { missing: ['a.kicad_sch'], extra: [] } } }), true);
  });

  it('E_UNSUPPORTED from kw_project_upgrade is shown once with its fix, no retry', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/unsupfmt/unsupfmt.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_UNSUPPORTED' && isHandledError(err));
      return true;
    });
    assert.equal(ui.errors.length, 1);
    assert.match(ui.errors[0].message, /upgrading unsupfmt failed: E_UNSUPPORTED: .*cli\.has_sch_pcb_upgrade - install a KiCad/);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade']);
  });

  it('E_READONLY from kw_project_upgrade shows the read-only message once, no retry', async () => {
    await start((b) => b[0]);
    await assert.rejects(server.pcbDrc('C:/fake/roupgrade/roupgrade.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_READONLY' && isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.equal(ui.errors.length, 0);
    assert.deepEqual(ui.infos, ['kiwrap is in read-only mode: kw_project_upgrade changed nothing (kw_project_upgrade needs a writable server). restart kiwrap serve without --readonly']);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc', 'kw_project_upgrade']);
  });

  it('a server without kw_project_upgrade (read-only): the unknown tool shows the read-only message once, no retry', async () => {
    await start((b) => b[0], 0, ['--readonly']);
    assert.equal(server.hasTool('kw_project_upgrade'), false);
    await assert.rejects(server.pcbDrc('C:/fake/oldfmt/oldfmt.kicad_pro', { save_board: true }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_NO_TOOL' && isHandledError(err));
      return true;
    });
    assert.equal(ui.modals.length, 1);
    assert.equal(ui.errors.length, 0);
    assert.equal(ui.infos.length, 1);
    assert.match(ui.infos[0], /^kiwrap is in read-only mode: kw_project_upgrade changed nothing \(this kiwrap server does not offer kw_project_upgrade\)\. A read-only server/);
    assert.deepEqual(names(await recorded()), ['kw_pcb_drc'], 'the unpublished tool is never sent');
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
    assert.deepEqual(changedHook, ['C:/fake/newerpin/newerpin.kicad_pro']);
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

  it('kw_project status carries the D21 fields and upgrade_args; retarget pins without an upgrade', async () => {
    await start(() => undefined);
    const st = await server.projectStatus('C:/fake/oldfmt/oldfmt.kicad_pro');
    assert.equal(st.data.target_effective, '10.99.0');
    assert.equal(st.data.target_source, 'file_epoch_newer_readonly');
    assert.equal(st.data.epoch_match, false);
    assert.equal(st.data.warnings?.length, 1);
    assert.deepEqual(asUpgradeArgs(st.data.upgrade_args) && upgradeCallArgs(asUpgradeArgs(st.data.upgrade_args)!), oldfmtArgs('oldfmt'));
    assert.deepEqual(await server.upgradeArgs('C:/fake/oldfmt/oldfmt.kicad_pro'), st.data.upgrade_args);
    assert.equal(await server.upgradeArgs('C:/fake/current/current.kicad_pro'), null, 'no upgrade_args when no write needs one');
    const rt = await server.projectRetarget('C:/fake/oldfmt/oldfmt.kicad_pro', '10.0.6');
    assert.equal(rt.data.target_kicad, '10.0.6');
    const calls = await recorded();
    assert.deepEqual(calls.map((c) => c.args.action), ['status', 'status', 'status', 'retarget']);
  });

  it('KiwrapServer.projectUpgrade sends exactly the four upgrade_args (to_format dropped)', async () => {
    await start(() => undefined);
    const args = asUpgradeArgs((await server.projectStatus('C:/fake/oldfmt/oldfmt.kicad_pro')).data.upgrade_args)!;
    assert.ok(args.to_format, 'status carries to_format');
    const r = await server.projectUpgrade(args, { interactive: false, consent: MODAL_CONSENT });
    assert.equal(r.data.target?.kicad, '10.99.0');
    assert.equal(r.data.snapshot_before, 'snap-pre-upgrade');
    assert.equal(r.data.consent?.via, 'kiwrap_vscode_modal');
    const calls = await recorded();
    assert.deepEqual(calls[1], { tool: 'kw_project_upgrade', args: oldfmtArgs('oldfmt') });
  });

  it('kw_project_upgrade without the modal token (kiwrap.core.callTool, the exported client) sends nothing (review finding 6)', async () => {
    await start((b) => b[0]);
    const args = upgradeCallArgs(oldfmtArgs('oldfmt'));
    for (const send of [
      () => server.client.call('kw_project_upgrade', args), // what kiwrap.core.callTool forwards
      () => server.projectUpgrade(oldfmtArgs('oldfmt')),
      () => server.client.call('kw_project_upgrade', args, { consent: Symbol('kiwrap.modalConsent') as unknown as typeof MODAL_CONSENT }),
    ]) {
      await assert.rejects(send(), (err: unknown) => {
        assert.ok(err instanceof KiwrapError && err.code === 'E_CLIENT');
        assert.match(err.message, /only after the user clicks \[Upgrade\] in kiwrap's consent modal; nothing was sent/);
        return true;
      });
    }
    assert.deepEqual(await recorded(), [], 'the fake server never saw the call');
    assert.equal(ui.modals.length, 0, 'the refusal is not a teaching error: no modal');
  });

  describe('the proactive command (FormatConsent.proactive)', () => {
    /** The command's call function, recording the per-call options. */
    const recordingCall = (log: { name: string; opts?: CallOptions }[]) => (n: string, a: Record<string, unknown>, o?: CallOptions) => {
      log.push({ name: n === 'kw_project' ? `kw_project:${String(a.action)}` : n, opts: o });
      return server.client.call(n, a, o);
    };

    it('calls kw_project status (interactive:false), then the modal, then kw_project_upgrade(upgrade_args)', async () => {
      const consent = await start((b) => b[0]);
      const log: { name: string; opts?: CallOptions }[] = [];
      const outcome = await consent.proactive('C:/fake/oldfmt/oldfmt.kicad_pro', [], recordingCall(log), server.hasTool('kw_project_upgrade'));
      assert.equal(outcome, 'upgraded');
      assert.deepEqual(log.map((l) => l.name), ['kw_project:status', 'kw_project_upgrade']);
      assert.equal(log[0].opts?.interactive, false, 'the status read never opens a modal');
      assert.equal(ui.modals.length, 1);
      assert.match(ui.modals[0].detail, /^From: KiCad 10\.0\nTo: KiCad 10\.99\.0/);
      const calls = await recorded();
      assert.deepEqual(names(calls), ['kw_project:status', 'kw_project_upgrade']);
      assert.deepEqual(calls[1].args, oldfmtArgs('oldfmt'));
      assert.match(ui.infos[0], /^kiwrap: upgraded oldfmt to KiCad 10\.99 format/);
    });

    it('Cancel converts nothing', async () => {
      const consent = await start(() => undefined);
      const outcome = await consent.proactive('C:/fake/oldfmt/oldfmt.kicad_pro', [], (n, a, o) => server.client.call(n, a, o), true);
      assert.equal(outcome, 'declined');
      assert.deepEqual(names(await recorded()), ['kw_project:status']);
    });

    it('a project in its target\'s format: no modal, "nothing to upgrade"', async () => {
      const consent = await start((b) => b[0]);
      const outcome = await consent.proactive('C:/fake/current/current.kicad_pro', [], (n, a, o) => server.client.call(n, a, o), true);
      assert.equal(outcome, 'up_to_date');
      assert.equal(ui.modals.length, 0);
      assert.match(ui.infos[0], /^kiwrap: current is already in the format KiCad 10\.99\.0 writes; nothing to upgrade\./);
      assert.deepEqual(names(await recorded()), ['kw_project:status']);
    });

    it('a server without kw_project_upgrade: the read-only message instead of a modal', async () => {
      const consent = await start((b) => b[0], 0, ['--readonly']);
      const outcome = await consent.proactive('C:/fake/oldfmt/oldfmt.kicad_pro', [], (n, a, o) => server.client.call(n, a, o), server.hasTool('kw_project_upgrade'));
      assert.equal(outcome, 'unavailable');
      assert.equal(ui.modals.length, 0);
      assert.equal(ui.infos.length, 1);
      assert.match(ui.infos[0], /^kiwrap is in read-only mode: kw_project_upgrade changed nothing/);
      assert.deepEqual(names(await recorded()), ['kw_project:status']);
    });

    it('decide() with status-built info asks, then upgrades (no confirm anywhere)', async () => {
      const consent = await start((b) => b[0]);
      const st = await server.projectStatus('C:/fake/oldfmt/oldfmt.kicad_pro');
      const { info } = proactiveUpgradeInfo('C:/fake/oldfmt/oldfmt.kicad_pro', st.data, []);
      assert.equal(await consent.decide(info, (n, a, o) => server.client.call(n, a, o)), 'upgraded');
      const calls = await recorded();
      assert.deepEqual(names(calls), ['kw_project:status', 'kw_project_upgrade']);
      assert.ok(calls.every((c) => !('confirm' in c.args)));
    });
  });
});
