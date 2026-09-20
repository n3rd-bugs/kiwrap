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
const CTX = Object.fromEntries([...typesSrc.matchAll(/^\s+(shield|shieldPalette|input|active): '(kiwrap\.live\.\w+)',$/gm)].map((m) => [m[1], m[2]]));
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
// `input` (keybindings.ts): the shield is armed only while an experimental input mode injects; on the
// product path (the live view) the key is false and VS Code's keyboard behaviour is untouched (D20)
const keybindings = [
  ...chords.chords.map((key) => ({ command: CMD.forwardKey, key, args: { key }, when: `${CTX.shield} && ${CTX.input} && ${activeClause} && ${focusClause}` })),
  ...chords.palette.map((key) => ({ command: CMD.forwardKey, key, args: { key }, when: `${CTX.shield} && ${CTX.input} && ${CTX.shieldPalette} && ${activeClause} && ${focusClause}` })),
];

const fragment = {
  commands: [
    // ext-core's kiwrap.openLive ("Open Live KiCad", in the menus) delegates here; this one takes the
    // full argument object ({project, editor, file, inputMode, target_kicad}) and stays palette-only
    { command: CMD.open, title: 'Live KiCad: Open session (project / editor)', category: 'kiwrap', icon: '$(circuit-board)' },
    { command: CMD.attach, title: 'Live KiCad: Attach to running KiCad (pid)', category: 'kiwrap' },
    { command: CMD.openSurface, title: 'Live KiCad: Show KiCad window in a tab (3D viewer, ...)', category: 'kiwrap' },
    { command: CMD.close, title: 'Live KiCad: Close session', category: 'kiwrap' },
    { command: CMD.status, title: 'Live KiCad: Status', category: 'kiwrap' },
    { command: CMD.raise, title: 'Live KiCad: Raise KiCad (bring the window to the front)', category: 'kiwrap' },
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
      'kiwrap.live.raiseOnDoubleClick': { type: 'boolean', default: true, description: 'Live view: a double-click on the picture raises the real KiCad window (the same as the tab\'s "Raise KiCad" button). A single click only focuses the tab.' },
      'kiwrap.live.inputMode': { type: 'string', enum: ['none', 'mirror', 'hybrid', 'events'], default: 'none', enumDescriptions: ['the live view (default, the supported mode): the tab shows the real KiCad editor live; to edit by hand, raise KiCad', 'EXPERIMENTAL, unsupported: drive the visible KiCad from the tab with SendInput', 'EXPERIMENTAL, unsupported: drive the visible KiCad from the tab with posted input and a borrowed cursor', 'EXPERIMENTAL, unsupported: cloaked KiCad driven with posted messages; needs a fork build carrying mr/display-input, falls back to the live view otherwise'], description: 'EXPERIMENTAL. Anything but "none" injects the tab\'s mouse and keyboard into KiCad; these modes are unsupported research paths (docs/DECISIONS.md D20) and may leave KiCad or the pointer in an odd state. The keybinding shield is armed only while an input mode is active.' },
      'kiwrap.live.shieldKeys': { type: 'boolean', default: true, description: 'While an EXPERIMENTAL input mode injects into a focused Live KiCad tab, swallow the VS Code keybindings a KiCad user would press (Ctrl+W, Ctrl+P, F5, ...). Has no effect in the live view (kiwrap.live.inputMode = none): VS Code\'s keys stay VS Code\'s.' },
      'kiwrap.live.keepCommandPalette': { type: 'boolean', default: true, description: 'Keep Ctrl+Shift+P / F1 for VS Code even while the shield is on.' },
      'kiwrap.live.pointerLock': { type: 'boolean', default: false, description: 'EXPERIMENTAL input modes: lock the pointer on click so drags never leave the tab. Esc releases it.' },
      'kiwrap.live.hud': { type: 'boolean', default: true, description: 'Show the HUD in the tab (surface, fps / latency, the "Raise KiCad" button).' },
      'kiwrap.live.fit': { type: 'string', enum: ['fit', 'native'], default: 'fit', description: 'fit = scale the KiCad frame down to the tab; native = 1 surface px per device px, the tab scrolls when the frame is larger.' },
      'kiwrap.live.fitWindowToTab': { type: 'boolean', default: true, description: 'EXPERIMENTAL input modes only: size the captured KiCad window to the tab (physical pixels) so the stream is 1:1 and the menus, toolbars and panels stay legible. The live view never touches the desktop window.' },
      'kiwrap.live.hiddenCloseDelayMs': { type: 'number', default: 1500, description: 'Grace period before a tab that is no longer visible closes its stream (it reconnects and resyncs when shown again).' },
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
