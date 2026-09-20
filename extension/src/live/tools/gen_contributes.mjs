#!/usr/bin/env node
// Generates extension/src/live/contributes.live.json (commands, customEditors, configuration, keybindings,
// menus of the Live KiCad module) from chords.ts and the ids in ../shared/types.ts. With --merge it
// folds the fragment into extension/package.json (owned by ext-core): keybindings bound to
// kiwrap.live.forwardKey are replaced, other entries are added or updated by id, nothing else is touched.
//   node extension/src/live/tools/gen_contributes.mjs [--merge] [--check]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const liveDir = path.resolve(here, '..');
const extDir = path.resolve(liveDir, '..', '..');
const typesSrc = fs.readFileSync(path.join(extDir, 'src', 'shared', 'types.ts'), 'utf8');
const id = (re) => { const m = typesSrc.match(re); if (!m) { throw new Error(`types.ts: ${re} not found`); } return m[1]; };

const EDITOR = id(/LIVE_EDITOR_VIEW_TYPE = '([^']+)'/);
const PANEL = id(/LIVE_PANEL_VIEW_TYPE = '([^']+)'/);
const CTX = Object.fromEntries([...typesSrc.matchAll(/^\s+(shield|shieldPalette|active): '(kiwrap\.live\.\w+)',$/gm)].map((m) => [m[1], m[2]]));
const CMD = Object.fromEntries([...typesSrc.matchAll(/^\s+(\w+): '(kiwrap\.live\.\w+)',$/gm)].filter((m) => !(m[1] in CTX)).map((m) => [m[1], m[2]]));
const chordsSrc = fs.readFileSync(path.join(liveDir, 'chords.ts'), 'utf8');
const arr = (name) => {
  const start = chordsSrc.indexOf(`export const ${name}: readonly string[] = [`);
  if (start < 0) { throw new Error(`chords.ts: ${name} not found`); }
  const body = chordsSrc.slice(chordsSrc.indexOf('[', start + `export const ${name}`.length) + 1, chordsSrc.indexOf('];', start));
  // single-quoted JS string literals; the only escape used is \\ (ctrl+\)
  return [...body.matchAll(/'((?:[^'\\]|\\.)*)'/g)].map((x) => x[1].replace(/\\(.)/g, '$1'));
};
const chords = { palette: arr('PALETTE_CHORDS'), chords: arr('SHIELD_CHORDS') };

const activeClause = `(activeCustomEditorId == '${EDITOR}' || activeWebviewPanelId == '${PANEL}')`;
// keybindings.ts FOCUS_NEGATIVES: the active-view keys stay set while the focus is in the sidebar /
// panel / terminal / an input box next to the live tab; the shield must not fire there
const kbSrc = fs.readFileSync(path.join(liveDir, 'keybindings.ts'), 'utf8');
const focusNegatives = [...(kbSrc.match(/FOCUS_NEGATIVES = \[([^\]]+)\]/)?.[1] ?? '').matchAll(/'([^']+)'/g)].map((m) => m[1]);
if (!focusNegatives.length) { throw new Error('keybindings.ts: FOCUS_NEGATIVES not found'); }
const focusClause = focusNegatives.map((k) => `!${k}`).join(' && ');
const keybindings = [
  ...chords.chords.map((key) => ({ command: CMD.forwardKey, key, args: { key }, when: `${CTX.shield} && ${activeClause} && ${focusClause}` })),
  ...chords.palette.map((key) => ({ command: CMD.forwardKey, key, args: { key }, when: `${CTX.shield} && ${CTX.shieldPalette} && ${activeClause} && ${focusClause}` })),
];

const fragment = {
  commands: [
    // ext-core's kiwrap.openLive ("Open Live KiCad", in the menus) delegates here; this one takes the
    // full argument object ({project, editor, file, mode, target_kicad}) and stays palette-only
    { command: CMD.open, title: 'Live KiCad: Open session (project / editor / mode)', category: 'kiwrap', icon: '$(circuit-board)' },
    { command: CMD.attach, title: 'Live KiCad: Attach to running KiCad (pid)', category: 'kiwrap' },
    { command: CMD.openSurface, title: 'Live KiCad: Show KiCad window in a tab (3D viewer, ...)', category: 'kiwrap' },
    { command: CMD.close, title: 'Live KiCad: Close session', category: 'kiwrap' },
    { command: CMD.status, title: 'Live KiCad: Status', category: 'kiwrap' },
    { command: CMD.raise, title: 'Live KiCad: Raise KiCad window', category: 'kiwrap' },
    { command: CMD.screenshot, title: 'Live KiCad: Screenshot surface', category: 'kiwrap' },
    { command: CMD.forwardKey, title: 'Live KiCad: Forward key (internal)', category: 'kiwrap' },
  ],
  customEditors: [
    {
      viewType: EDITOR, displayName: 'Live KiCad',
      selector: [{ filenamePattern: '*.kicad_pcb' }, { filenamePattern: '*.kicad_sch' }],
      priority: 'option',
    },
  ],
  menus: {
    commandPalette: [{ command: CMD.forwardKey, when: 'false' }],
    'editor/title': [{ command: CMD.openSurface, when: `activeCustomEditorId == '${EDITOR}' || activeWebviewPanelId == '${PANEL}'`, group: 'navigation@9' }],
  },
  configuration: {
    title: 'kiwrap Live KiCad',
    properties: {
      // executables are machine-scoped: a repository's .vscode/settings.json must not be able to point them at its own binaries (finding 14)
      'kiwrap.live.helperPath': { type: 'string', default: '', scope: 'machine', description: 'Path of kiwrap-live.exe. Empty = <extension>/bin/kiwrap-live.exe, then helper/target/{release,debug}/kiwrap-live.exe next to the extension folder (dev). A .js/.mjs path is run with node (tests). Machine scope: user settings only.' },
      'kiwrap.live.nodePath': { type: 'string', default: '', scope: 'machine', description: 'Node executable used for a .js helper (tests). Empty = the extension host itself (ELECTRON_RUN_AS_NODE). Machine scope: user settings only.' },
      'kiwrap.live.mode': { type: 'string', enum: ['auto', 'hidden', 'mirror', 'view'], default: 'auto', enumDescriptions: ['hidden when the launched install carries mr/display-input, otherwise view-only', 'hidden KiCad (cloaked, input by window messages); needs a build with mr/display-input', 'KiCad visible on the desktop, mirrored and driven from the tab (stock KiCad, opt-in)', 'capture only; a click raises the real window'], description: 'How the Live KiCad tab treats input (docs/DISPLAY.md 12.6).' },
      'kiwrap.live.mirrorInjector': { type: 'string', enum: ['hybrid', 'mirror'], default: 'hybrid', enumDescriptions: ['posted input with a borrowed cursor (VS Code keeps the foreground; docs/DISPLAY.md 12.4)', 'SendInput (KiCad must be visible and foreground; the first spike\'s injector)'], description: 'Which helper injector the "mirror" mode uses on stock KiCad.' },
      'kiwrap.live.shieldKeys': { type: 'boolean', default: true, description: 'While a Live KiCad tab is active, swallow the VS Code keybindings a KiCad user would press (Ctrl+W, Ctrl+P, F5, ...).' },
      'kiwrap.live.keepCommandPalette': { type: 'boolean', default: true, description: 'Keep Ctrl+Shift+P / F1 for VS Code even while the shield is on.' },
      'kiwrap.live.pointerLock': { type: 'boolean', default: false, description: 'Lock the pointer on click (mirror / hidden modes) so drags never leave the tab. Esc releases it.' },
      'kiwrap.live.hud': { type: 'boolean', default: true, description: 'Show the fps / latency / mode HUD in the tab.' },
      'kiwrap.live.fit': { type: 'string', enum: ['fit', 'native'], default: 'fit', description: 'fit = scale the KiCad frame down to the tab; native = 1 surface px per device px, the tab scrolls when the frame is larger.' },
      'kiwrap.live.fitWindowToTab': { type: 'boolean', default: true, description: 'In hidden and mirror modes, size the captured KiCad window to the tab (physical pixels) so the stream is 1:1 and the menus, toolbars and panels stay legible. View-only sessions leave the desktop window alone.' },
      'kiwrap.live.hiddenCloseDelayMs': { type: 'number', default: 1500, description: 'Grace period before a hidden tab closes its stream (it reconnects and resyncs when shown again).' },
      'kiwrap.live.autoOpen3d': { type: 'boolean', default: false, description: 'Open a tab automatically when KiCad shows a 3D viewer.' },
      'kiwrap.live.retainContextWhenHidden': { type: 'boolean', default: true, description: 'Keep Live KiCad webviews alive while hidden (faster tab switches, more memory).' },
    },
  },
  keybindings,
};

const outPath = path.join(liveDir, 'contributes.live.json');
const args = process.argv.slice(2);
const text = JSON.stringify(fragment, null, 2) + '\n';
if (args.includes('--check')) {
  const cur = fs.existsSync(outPath) ? fs.readFileSync(outPath, 'utf8') : '';
  if (cur !== text) { console.error('contributes.live.json is stale: run gen_contributes.mjs'); process.exit(1); }
  console.log('contributes.live.json up to date');
} else {
  fs.writeFileSync(outPath, text);
  console.log(`wrote ${outPath}: ${fragment.commands.length} commands, ${keybindings.length} keybindings`);
}

if (args.includes('--merge')) {
  const pkgPath = path.join(extDir, 'package.json');
  if (!fs.existsSync(pkgPath)) { console.error(`no ${pkgPath} to merge into`); process.exit(2); }
  const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
  pkg.contributes ??= {};
  const c = pkg.contributes;
  const upsert = (list, key, items) => { const out = Array.isArray(list) ? list.filter((x) => !items.some((y) => y[key] === x[key])) : []; return [...out, ...items]; };
  c.commands = upsert(c.commands, 'command', fragment.commands);
  c.customEditors = upsert(c.customEditors, 'viewType', fragment.customEditors);
  c.keybindings = [...(Array.isArray(c.keybindings) ? c.keybindings.filter((k) => k.command !== CMD.forwardKey) : []), ...keybindings];
  c.menus ??= {};
  for (const [menu, items] of Object.entries(fragment.menus)) { c.menus[menu] = upsert(c.menus[menu], 'command', items); }
  const confs = Array.isArray(c.configuration) ? c.configuration : c.configuration ? [c.configuration] : [];
  const mine = confs.find((x) => x.title === fragment.configuration.title);
  if (mine) { mine.properties = fragment.configuration.properties; } else { confs.push(fragment.configuration); }
  c.configuration = confs.length === 1 ? confs[0] : confs;
  const acts = new Set(Array.isArray(pkg.activationEvents) ? pkg.activationEvents : []);
  acts.add(`onCustomEditor:${EDITOR}`); acts.add(`onWebviewPanel:${PANEL}`);
  for (const ctx of Object.values(CTX)) { acts.delete(`onCommand:${ctx}`); }
  for (const cmd of Object.values(CMD)) { acts.add(`onCommand:${cmd}`); }
  pkg.activationEvents = [...acts];
  fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n');
  console.log(`merged into ${pkgPath}`);
}
