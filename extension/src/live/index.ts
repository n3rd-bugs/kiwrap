/*
 * Live KiCad module entry point. ext-core calls `activateLive(context, { client })` from its
 * activate(); the returned LiveApi is also what the integration test drives.
 *
 * Commands (all contributed in contributes.live.json):
 *   kiwrap.live.open        launch KiCad for the workspace project via kw_live, attach the helper,
 *                           open the file's custom editor (args: {project?, editor?, file?, mode?})
 *   kiwrap.live.attach      attach the helper to a running KiCad pid (args: {pid, project?, inject?})
 *   kiwrap.live.openSurface pick a captured window (3D viewer, ...) and show it in a panel
 *   kiwrap.live.close       kill the helper and detach/kill the KiCad session
 *   kiwrap.live.status      show the session status
 *   kiwrap.live.raise       bring the real KiCad window to the front (view-only mode)
 *   kiwrap.live.screenshot  PNG of a surface via the helper (returns base64; optional save)
 *   kiwrap.live.forwardKey  keybinding shield target (swallows the VS Code action)
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { LiveSessionManager, type ModePreference } from './liveSession';
import { LiveEditorProvider } from './liveEditor';
import { LivePanels } from './livePanel';
import { editorForFile, kindForFile, projectDirFor, tabShapeFor } from './surfaces';
import type { LiveView } from './webviewHost';
import { LIVE_COMMANDS, LIVE_CONTEXT_KEYS, LIVE_EDITOR_VIEW_TYPE, LIVE_SETTINGS_SECTION, type InjectMode, type KiwrapClient, type LiveStatus } from '../shared/types';

export interface LiveDeps {
  /** ext-core's MCP client (kw_* tools). Optional: see kiwrapClient.ts for the fallbacks. */
  client?: KiwrapClient;
  /** Output channel to share; a "kiwrap live" channel is created otherwise. */
  output?: vscode.OutputChannel;
  /** Directory holding media/ (defaults to the extension root). */
  mediaRoot?: vscode.Uri;
}

export interface LiveOpenArgs { project?: string; editor?: 'pcb' | 'sch' | 'project'; file?: string; mode?: ModePreference; target_kicad?: string }
/** `allowAnyProcess`: attach to a pid that is not a KiCad editor (tests only; the helper refuses otherwise). */
export interface LiveAttachArgs { pid: number; project?: string; inject?: InjectMode; allowAnyProcess?: boolean }

/** Image names the live module attaches to (reviewer finding 15: anything else is refused). */
export const KICAD_EDITOR_IMAGES = ['kicad.exe', 'pcbnew.exe', 'eeschema.exe'];

/** The image name of a running process (`tasklist`), '' when unknown. Windows only (D18). */
export function processImageName(pid: number): string {
  if (process.platform !== 'win32') { return ''; }
  try {
    const r = spawnSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8', windowsHide: true, timeout: 10_000 });
    const line = (r.stdout ?? '').split(/\r?\n/).find((l) => l.startsWith('"'));
    if (!line) { return ''; }
    const m = /^"([^"]+)","(\d+)"/.exec(line);
    return m && Number(m[2]) === pid ? m[1].toLowerCase() : '';
  } catch { return ''; }
}

export interface LiveApi {
  sessions: LiveSessionManager;
  editors: LiveEditorProvider;
  panels: LivePanels;
  status(): LiveStatus;
  open(args?: LiveOpenArgs): Promise<LiveStatus>;
  attach(args: LiveAttachArgs): Promise<LiveStatus>;
  close(): Promise<void>;
  views(): LiveView[];
  activeView(): LiveView | undefined;
}

export function activateLive(context: vscode.ExtensionContext, deps: LiveDeps = {}): LiveApi {
  const out = deps.output ?? vscode.window.createOutputChannel('kiwrap live');
  if (!deps.output) { context.subscriptions.push(out); }
  const mediaRoot = deps.mediaRoot ?? vscode.Uri.joinPath(context.extensionUri, 'media');
  const cfg = () => vscode.workspace.getConfiguration(LIVE_SETTINGS_SECTION);

  const sessions = new LiveSessionManager(context, out, deps.client);
  const { provider: editors, disposable: editorReg } = LiveEditorProvider.register(sessions, mediaRoot, out, cfg().get<boolean>('retainContextWhenHidden', true));
  const panels = new LivePanels(sessions, mediaRoot, out);
  context.subscriptions.push(sessions, editorReg, panels);

  const views = (): LiveView[] => [...editors.views, ...panels.views];
  const activeView = (): LiveView | undefined => views().find((v) => v.panel.active);

  // Context keys for the keybinding shield.
  const applyContext = async (): Promise<void> => {
    await vscode.commands.executeCommand('setContext', LIVE_CONTEXT_KEYS.shield, cfg().get<boolean>('shieldKeys', true));
    await vscode.commands.executeCommand('setContext', LIVE_CONTEXT_KEYS.shieldPalette, !cfg().get<boolean>('keepCommandPalette', true));
  };
  void applyContext();
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration(LIVE_SETTINGS_SECTION)) { void applyContext(); } }),
    sessions.onDidChange((s) => { void vscode.commands.executeCommand('setContext', LIVE_CONTEXT_KEYS.active, !!s); }),
  );

  async function resolveProject(args: LiveOpenArgs): Promise<{ project: string; editor: 'pcb' | 'sch' | 'project'; file?: string }> {
    const file = args.file ?? (activeView()?.selector as { file?: string } | undefined)?.file ?? vscode.window.activeTextEditor?.document.uri.fsPath;
    const project = args.project ?? (file && kindForFile(file) ? projectDirFor(file, (d) => fs.readdirSync(d)) : undefined) ?? (await pickProject());
    if (!project) { throw new Error('no KiCad project: open a .kicad_pcb/.kicad_sch or a folder with a .kicad_pro'); }
    const editor = args.editor ?? (file && kindForFile(file) ? editorForFile(file) : 'project');
    return { project, editor, file: file && kindForFile(file) ? file : undefined };
  }

  async function pickProject(): Promise<string | undefined> {
    const pros = await vscode.workspace.findFiles('**/*.kicad_pro', '**/node_modules/**', 20);
    if (pros.length === 1) { return path.dirname(pros[0].fsPath); }
    if (!pros.length) { return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath; }
    const pick = await vscode.window.showQuickPick(pros.map((u) => ({ label: path.basename(u.fsPath), description: path.dirname(u.fsPath), dir: path.dirname(u.fsPath) })), { placeHolder: 'KiCad project to open live' });
    return pick?.dir;
  }

  async function showFile(file: string | undefined): Promise<void> {
    if (!file) { return; }
    const kind = kindForFile(file);
    if (kind && tabShapeFor(kind) === 'editor') { await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(file), LIVE_EDITOR_VIEW_TYPE); }
  }

  const api: LiveApi = {
    sessions, editors, panels, views, activeView,
    status: () => sessions.status(),
    async open(args = {}) {
      const { project, editor, file } = await resolveProject(args);
      try {
        await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Live KiCad: starting KiCad...' }, async () => { await sessions.open(project, editor, args.mode, args.target_kicad); });
      } catch (e) {
        // the file's live tab still opens: its card explains the state (a kept session of the
        // other editor kind, a missing helper) and carries the Start button
        await showFile(file);
        throw e;
      }
      await showFile(file);
      return sessions.status();
    },
    async attach(args) {
      if (!Number.isInteger(args.pid) || args.pid <= 0) { throw new Error('attach needs a pid'); }
      if (!args.allowAnyProcess) {
        // the helper streams and drives every window of the pid: only KiCad editors are attached
        const image = processImageName(args.pid);
        if (!KICAD_EDITOR_IMAGES.includes(image)) {
          throw new Error(`pid ${args.pid} is ${image || 'not a running process'}, not a KiCad editor (${KICAD_EDITOR_IMAGES.join(', ')}); only KiCad windows are streamed into VS Code`);
        }
      }
      const pref = cfg().get<ModePreference>('mode', 'auto');
      const inject: InjectMode = args.inject ?? (pref === 'mirror' ? 'hybrid' : pref === 'hidden' ? 'events' : 'none');
      await sessions.attach(args.pid, args.project ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd(), inject, { session: null, allowAnyProcess: !!args.allowAnyProcess });
      return sessions.status();
    },
    close: () => sessions.close(),
  };

  const wrap = <T>(name: string, fn: (...a: any[]) => Promise<T> | T) => vscode.commands.registerCommand(name, async (...a: any[]) => {
    try { return await fn(...a); } catch (e) { const msg = e instanceof Error ? e.message : String(e); out.appendLine(`[live] ${name} failed: ${msg}`); void vscode.window.showErrorMessage(`Live KiCad: ${msg}`); return undefined; }
  });

  context.subscriptions.push(
    wrap(LIVE_COMMANDS.open, (args?: LiveOpenArgs) => api.open(args ?? {})),
    wrap(LIVE_COMMANDS.attach, async (args?: LiveAttachArgs) => {
      if (!args?.pid) {
        const s = await vscode.window.showInputBox({ prompt: 'PID of the running KiCad (kicad.exe / pcbnew.exe / eeschema.exe)', validateInput: (v) => (/^\d+$/.test(v) ? undefined : 'digits only') });
        if (!s) { return undefined; }
        args = { pid: Number(s) };
      }
      return api.attach(args);
    }),
    wrap(LIVE_COMMANDS.openSurface, () => panels.pickAndOpen()),
    wrap(LIVE_COMMANDS.close, () => api.close()),
    wrap(LIVE_COMMANDS.status, async () => {
      const s = api.status();
      const text = s.pid ? `Live KiCad: pid ${s.pid}, mode ${s.mode}, helper v${s.helper?.version} on :${s.helper?.port} (restarts ${s.helper?.restarts}), surfaces: ${s.surfaces.map((x) => `${x.kind}${x.cloaked ? '(cloaked)' : ''}`).join(', ') || 'none'}` : 'Live KiCad: no session';
      out.appendLine(`[live] ${JSON.stringify(s)}`);
      void vscode.window.showInformationMessage(text);
      return s;
    }),
    wrap(LIVE_COMMANDS.raise, () => sessions.raise(activeView()?.boundSurface?.id)),
    wrap(LIVE_COMMANDS.screenshot, async (args?: { surface?: string; save?: string; max_px?: number }) => {
      const id = args?.surface ?? activeView()?.boundSurface?.id ?? sessions.surfaces.all[0]?.id;
      if (!id) { throw new Error('no surface to screenshot'); }
      const shot = await sessions.screenshot(id, args?.max_px ?? 1024);
      if (args?.save) { fs.writeFileSync(args.save, shot.png); }
      return { surface: id, w: shot.w, h: shot.h, bytes: shot.png.length, png_base64: shot.png.toString('base64'), saved: args?.save };
    }),
    vscode.commands.registerCommand(LIVE_COMMANDS.forwardKey, (args?: { key?: string }) => {
      // The DOM already saw the chord; this command exists so VS Code's own binding does not run.
      activeView()?.post({ type: 'forwardedKey', key: args?.key ?? '?' });
    }),
  );

  out.appendLine(`[live] activated (vscode ${vscode.version}, node ${process.versions.node}, client ${deps.client ? 'injected' : 'command/none'})`);
  return api;
}

export { LiveSessionManager } from './liveSession';
export { LiveView } from './webviewHost';
export { generateKeybindings } from './keybindings';
