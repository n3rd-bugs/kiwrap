/*
 * Keybinding shield (docs/research/display/vscode-shell.md 3(b), vscode-shell-keys.md): while a Live
 * KiCad tab is the active custom editor / webview panel AND its helper injects input (an experimental
 * input mode, kiwrap.live.inputMode != none), every chord VS Code binds by default is rebound to
 * `kiwrap.live.forwardKey`, whose only job is to swallow the workbench action. The webview already
 * received the DOM keydown (VS Code cannot prevent that), so nothing has to be re-sent.
 *
 * Three context keys: `kiwrap.live.input` (set only while the session injects input; on the product
 * path - the live view, D20 - it is false and the shield never fires, VS Code's keys stay VS Code's),
 * `kiwrap.live.shield` (setting kiwrap.live.shieldKeys) and `kiwrap.live.shieldPalette` (true only
 * when kiwrap.live.keepCommandPalette is false), so Ctrl+Shift+P / F1 can stay VS Code's.
 * The active-view keys stay set while the keyboard focus sits in the sidebar, a panel, the terminal or
 * an input box next to the live tab (reviewer finding 6: every shielded chord was swallowed there),
 * so the clause also requires that none of those has the focus (FOCUS_NEGATIVES).
 * The generated block lives in contributes.live.json (tools/gen_contributes.mjs) for package.json.
 */
import { PALETTE_CHORDS, SHIELD_CHORDS } from './chords';
import { LIVE_COMMANDS, LIVE_CONTEXT_KEYS, LIVE_EDITOR_VIEW_TYPE, LIVE_PANEL_VIEW_TYPE } from '../shared/types';

export interface Keybinding { command: string; key: string; args: { key: string }; when: string }

export { PALETTE_CHORDS, SHIELD_CHORDS };

export const ACTIVE_VIEW_CLAUSE = `(activeCustomEditorId == '${LIVE_EDITOR_VIEW_TYPE}' || activeWebviewPanelId == '${LIVE_PANEL_VIEW_TYPE}')`;

/** VS Code focus context keys that mean "the keyboard is NOT in the live tab" although it is the active editor. */
export const FOCUS_NEGATIVES = ['sideBarFocus', 'auxiliaryBarFocus', 'panelFocus', 'terminalFocus', 'inputFocus', 'notificationFocus'] as const;

export const FOCUS_CLAUSE = FOCUS_NEGATIVES.map((k) => `!${k}`).join(' && ');

export function whenClause(palette: boolean): string {
  return palette
    ? `${LIVE_CONTEXT_KEYS.shield} && ${LIVE_CONTEXT_KEYS.input} && ${LIVE_CONTEXT_KEYS.shieldPalette} && ${ACTIVE_VIEW_CLAUSE} && ${FOCUS_CLAUSE}`
    : `${LIVE_CONTEXT_KEYS.shield} && ${LIVE_CONTEXT_KEYS.input} && ${ACTIVE_VIEW_CLAUSE} && ${FOCUS_CLAUSE}`;
}

export function generateKeybindings(): Keybinding[] {
  const mk = (key: string, palette: boolean): Keybinding => ({ command: LIVE_COMMANDS.forwardKey, key, args: { key }, when: whenClause(palette) });
  return [...SHIELD_CHORDS.map((k) => mk(k, false)), ...PALETTE_CHORDS.map((k) => mk(k, true))];
}
