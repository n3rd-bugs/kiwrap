/**
 * The tree's install / target model (installModel.ts): D21 semantics of kiwrap.kicadInstall, the
 * effective target and its source from kw_project status (faked), install roles, and the one-click
 * fix (a row's click reveals details; only an explicit "Select" names an install).
 */
import * as assert from 'node:assert/strict';
import {
  effectiveTarget,
  epochMatch,
  formatField,
  installDetails,
  installLabel,
  installRoles,
  installSpec,
  isProjectInstallSpec,
  MATCH_PROJECT_LABEL,
  matchesSpec,
  matchingInstalls,
  rolesText,
  selectionArg,
  sourceText,
  targetField,
} from '../../src/status/installModel';
import type { EnvData, InstallRow, ProjectStatusData } from '../../src/types';

function row(version: string, root: string, formats: { sch?: number; pcb?: number } = {}, source = 'registry'): InstallRow {
  const caps: Record<string, unknown> = { 'cli.has_drc': true };
  if (formats.sch !== undefined) {
    caps['formats.sch'] = formats.sch;
  }
  if (formats.pcb !== undefined) {
    caps['formats.pcb'] = formats.pcb;
  }
  return { version, major_minor: version.split('.').slice(0, 2).join('.'), root, kicad_cli: `${root}/bin/kicad-cli.exe`, share: `${root}/share/kicad`, source, python: null, capabilities: caps };
}

const NIGHTLY = row('10.99.0', 'C:/Program Files/KiCad/10.99', { sch: 20260830, pcb: 20260901 });
const BENCH = row('10.99.0', 'C:/kiwrap-builds/6dd893f7d1-7', { sch: 20260830, pcb: 20260901 }, 'explicit');
const STABLE = row('10.0.6', 'C:/Program Files/KiCad/10.0', { sch: 20260206, pcb: 20260206 });
const NINE = row('9.0.9', 'C:/Program Files/KiCad/9.0', { sch: 20250114, pcb: 20241229 });
const ROWS = [NIGHTLY, BENCH, STABLE, NINE];

function env(installs: InstallRow[] = ROWS): EnvData {
  return { ok: true, kiwrap_version: 'x', platform: 'windows', installs, default_install: installs[0]?.version ?? null, engines: {}, data_backends: {}, storage: {} };
}

function status(extra: Partial<ProjectStatusData> = {}): ProjectStatusData {
  return {
    ok: true, project: 'C:/w/kit/kit.kicad_pro', dir: 'C:/w/kit', name: 'kit', stage: 'empty', next_suggested: null,
    target_kicad: null, target_installed: null, hashes: {}, epochs: { sch: 20260206, pcb: 20260206 },
    files: { sch: ['kit.kicad_sch'], pcb: ['kit.kicad_pcb'] }, editor_locks: [], snapshot_head: null, external_edit: null, history: [],
    ...extra,
  };
}

describe('installModel: kiwrap.kicadInstall semantics (D21)', () => {
  it('empty and "project" mean: match the project file format', () => {
    assert.equal(isProjectInstallSpec(''), true);
    assert.equal(isProjectInstallSpec('  '), true);
    assert.equal(isProjectInstallSpec('Project'), true);
    assert.equal(isProjectInstallSpec(undefined), true);
    assert.equal(isProjectInstallSpec('10.0'), false);
    assert.equal(isProjectInstallSpec('C:/kiwrap-builds/x'), false);
    assert.equal(MATCH_PROJECT_LABEL, "match the project's file format (recommended)");
  });

  it('a click on an install row passes no string; the inline Select / context menu pass the node and select it', () => {
    // the row no longer has a click command; the menu actions receive the tree element
    assert.equal(selectionArg({ kind: 'install', value: '10.0.6', label: 'KiCad 10.0.6' }), '10.0.6');
    assert.equal(selectionArg({ kind: 'install', value: 'C:/kiwrap-builds/6dd893f7d1-7' }), 'C:/kiwrap-builds/6dd893f7d1-7');
    assert.equal(selectionArg('10.99'), '10.99');
    assert.equal(selectionArg(''), '', 'an explicit empty string = match the project');
    assert.equal(selectionArg({ kind: 'projectField', value: undefined }), undefined, 'other nodes open the picker');
    assert.equal(selectionArg(undefined), undefined);
  });
});

describe('installModel: labels, specs, formats', () => {
  it('labels and specs tell bench builds from the stock install', () => {
    assert.equal(installLabel(NIGHTLY, ROWS), 'KiCad 10.99.0 (10.99)');
    assert.equal(installLabel(BENCH, ROWS), 'KiCad 10.99.0 (6dd893f7d1-7)');
    assert.equal(installLabel(STABLE, ROWS), 'KiCad 10.0.6');
    assert.equal(installSpec(STABLE, ROWS), '10.0.6');
    assert.equal(installSpec(BENCH, ROWS), 'C:/kiwrap-builds/6dd893f7d1-7');
    assert.ok(matchesSpec(STABLE, '10'));
    assert.ok(matchesSpec(STABLE, '10.0'));
    assert.ok(!matchesSpec(STABLE, '10.9'));
    assert.ok(matchesSpec(BENCH, 'c:\\kiwrap-builds\\6dd893f7d1-7\\'));
  });

  it('matchingInstalls finds the installs writing the files exact format', () => {
    assert.deepEqual(matchingInstalls(ROWS, { sch: 20260206, pcb: 20260206 }), [STABLE]);
    assert.deepEqual(matchingInstalls(ROWS, { sch: 20260830, pcb: null }), [NIGHTLY, BENCH]);
    assert.deepEqual(matchingInstalls(ROWS, { sch: 20230121, pcb: 20221018 }), []);
    assert.deepEqual(matchingInstalls(ROWS, { sch: null, pcb: null }), []);
  });

  it('install details list root, cli, source, formats and capabilities', () => {
    const d = Object.fromEntries(installDetails(STABLE));
    assert.equal(d.root, 'C:/Program Files/KiCad/10.0');
    assert.equal(d['kicad-cli'], 'C:/Program Files/KiCad/10.0/bin/kicad-cli.exe');
    assert.equal(d['writes formats'], 'sch 20260206, pcb 20260206');
    assert.equal(d.capabilities, 'cli.has_drc');
  });
});

describe('installModel: the effective target from kw_project status (review item 1)', () => {
  it('shows target_effective with its source in words', () => {
    const t = targetField(status({ target_effective: '10.0.6', target_source: 'file_epoch', epoch_match: true }), undefined);
    assert.equal(t.description, "10.0.6 (matches the files' format)");
    assert.equal(t.icon, undefined);
    assert.match(t.tooltip, /follows the project/);
    assert.equal(sourceText('pinned'), 'pinned');
    assert.equal(sourceText('created'), 'created by kiwrap');
  });

  it('a newer-epoch target is flagged read-only with a warning icon', () => {
    const t = targetField(status({ target_effective: '10.99.0', target_source: 'file_epoch_newer_readonly', epoch_match: false }), undefined);
    assert.equal(t.description, '10.99.0 (newer than the files: read-only until upgraded)');
    assert.equal(t.icon, 'warning');
  });

  it("reads the review's spelling and older servers without the fields", () => {
    assert.equal(effectiveTarget(status({ target_kicad_effective: '9.0.9' })), '9.0.9');
    assert.equal(targetField(status({ target_kicad: '10.0.6' }), undefined).description, '10.0.6 (pinned)');
    assert.equal(targetField(status(), undefined).description, 'matched to the file format by the server');
    assert.equal(targetField(undefined, undefined).description, 'status pending');
    assert.match(targetField(status(), '10.99').tooltip, /kiwrap\.kicadInstall = 10\.99: the extension's live launches use it/);
  });

  it('epoch_match may be a boolean or per kind', () => {
    assert.equal(epochMatch(status({ epoch_match: true })), true);
    assert.equal(epochMatch(status({ epoch_match: { sch: true, pcb: false } })), false);
    assert.equal(epochMatch(status({ epoch_match: { sch: null } })), null);
    assert.equal(epochMatch(status()), null);
  });

  it('the file format field says whether an installed KiCad writes it', () => {
    const ok = formatField(status({ target_effective: '10.0.6', epoch_match: true }), env());
    assert.equal(ok?.description, 'sch 20260206, pcb 20260206 - KiCad 10.0.6 writes it');
    assert.equal(ok?.icon, 'pass');
    // only 10.99 installed: the review's scenario (kit-dev-coldfire re-saved by 10.0.6)
    const bad = formatField(status({ target_effective: '10.99.0', epoch_match: false }), env([NIGHTLY]));
    assert.equal(bad?.description, 'sch 20260206, pcb 20260206 - no installed KiCad writes it');
    assert.equal(bad?.icon, 'warning');
    assert.match(bad?.tooltip ?? '', /kiwrap asks first/);
    // an older server (no epoch_match): the extension's own match against kw_env formats
    assert.equal(formatField(status(), env([NIGHTLY]))?.icon, 'warning');
    assert.equal(formatField(status(), env())?.description, 'sch 20260206, pcb 20260206 - KiCad 10.0.6 writes it');
    assert.equal(formatField(status({ epochs: { sch: null, pcb: null } }), env()), undefined);
  });
});

describe('installModel: roles (selected / project / newest)', () => {
  it('follows the project when nothing is selected', () => {
    const roles = installRoles(ROWS, { effective: '10.0.6', newest: '10.99.0' });
    assert.equal(rolesText(roles.get(STABLE)), 'project');
    assert.equal(rolesText(roles.get(NIGHTLY)), 'newest', 'exactly one newest row (finding 18)');
    assert.equal(rolesText(roles.get(BENCH)), '');
  });

  it('an explicit selection marks its row; a root selects the bench build only', () => {
    const roles = installRoles(ROWS, { explicit: 'C:/kiwrap-builds/6dd893f7d1-7', effective: '10.0.6', newest: '10.99.0' });
    assert.equal(rolesText(roles.get(BENCH)), 'selected');
    assert.equal(rolesText(roles.get(NIGHTLY)), 'newest');
    const same = installRoles(ROWS, { explicit: '10.0', effective: '10.0.6', newest: '10.99.0' });
    assert.equal(rolesText(same.get(STABLE)), 'selected project');
  });
});
