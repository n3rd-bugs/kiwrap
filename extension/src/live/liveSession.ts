/*
 * LiveSessionManager: the one live KiCad session of this VS Code window.
 *
 *   open(project)  -> kw_live launch (sandboxed GUI KiCad, api-server on)      [ext-core MCP client]
 *                  -> spawn kiwrap-live.exe --pid <pid> ... --inject <mode>    [HelperProcess]
 *                  -> /control: surfaces list + pushes                          [ControlClient]
 *                  -> LiveView tabs bind to surfaces by kind                    [webviewHost.ts]
 *   close()        -> helper killed, kw_live detach (kills the KiCad)
 *
 * One KiCad per project and one session per window (D18 process hygiene): opening another project
 * closes the current session first; the project identity is its directory (`projectKey`: a
 * `.kicad_pro` path and its directory name the same session - reviewer finding 7). A session of
 * another editor kind (a pcbnew session and a request for the schematic) is never reused silently:
 * the user is asked whether to replace it (finding 8). The helper is restarted by HelperProcess
 * while KiCad is alive; the session ends when the KiCad pid dies (polled) or on close().
 */
import * as vscode from 'vscode';
import { HelperProcess, resolveHelper } from './helperProcess';
import { ControlClient, controlUrl } from './controlClient';
import { KwLive, resolveClient } from './kiwrapClient';
import { inputModeOf, launchModeFor, resolveInject, type InputModePreference } from './mode';
import { SurfacesModel, normalizeSurface, projectKey, surfaceKindFor, type LiveEditorKind } from './surfaces';
import { LIVE_SETTINGS_SECTION, viewModeForInject, type InjectMode, type KiwrapClient, type KwLiveSession, type LiveStatus, type ViewMode } from '../shared/types';

export type { InputModePreference } from './mode';
export { projectKey, surfaceKindFor, type LiveEditorKind } from './surfaces';

export interface LiveSessionState {
  /** The project as the caller named it (a directory or a .kicad_pro path). */
  project: string;
  /** Normalised identity of the project (its directory, resolved, lower-cased). */
  projectKey: string;
  /** The editor kind kw_live launched (`project` = the manager; unknown for a bare attach). */
  editor: LiveEditorKind | null;
  session: KwLiveSession | null;   // null when attached to a pid without kw_live
  pid: number;
  inject: InjectMode;
  helper: HelperProcess;
  control: ControlClient;
  port: number;
  token: string;
  version: string;
}

export interface AttachOptions {
  session?: KwLiveSession | null;
  editor?: LiveEditorKind | null;
  /** Attach the helper to a pid that is not a KiCad editor (tests). */
  allowAnyProcess?: boolean;
}

export class LiveSessionManager implements vscode.Disposable {
  private state: LiveSessionState | null = null;
  private livenessTimer: NodeJS.Timeout | null = null;
  private readonly _onDidChange = new vscode.EventEmitter<LiveSessionState | null>();
  readonly onDidChange = this._onDidChange.event;
  readonly surfaces = new SurfacesModel();
  private kwLive: KwLive = new KwLive(null);
  private clientResolved = false;
  /** Injectable for tests: the modal "replace the running session?" question. */
  confirmReplace: (message: string, action: string) => Thenable<boolean> = async (message, action) =>
    (await vscode.window.showWarningMessage(message, { modal: true }, action)) === action;

  constructor(private readonly context: vscode.ExtensionContext, private readonly out: vscode.OutputChannel, private readonly injectedClient?: KiwrapClient) {}

  get current(): LiveSessionState | null { return this.state; }
  get mode(): ViewMode { return this.state ? viewModeForInject(this.state.inject) : 'view'; }
  /** True while the helper injects input into KiCad (an experimental input mode): the only time the keybinding shield is wanted. */
  get inputActive(): boolean { return !!this.state && this.state.inject !== 'none'; }
  /** The kiwrap.live.inputMode setting, coerced (`none` = the live view, the product path). */
  inputModeSetting(): InputModePreference { return inputModeOf(this.cfg<string>('inputMode', 'none')); }

  private cfg<T>(key: string, dflt: T): T { return vscode.workspace.getConfiguration(LIVE_SETTINGS_SECTION).get<T>(key, dflt); }
  private log(msg: string): void { this.out.appendLine(`[live] ${new Date().toISOString()} ${msg}`); }

  private async ensureClient(): Promise<KwLive> {
    if (!this.clientResolved) { this.kwLive = new KwLive(await resolveClient(this.injectedClient)); this.clientResolved = true; }
    return this.kwLive;
  }

  /** Stream endpoint for the webviews (they connect to /stream themselves). */
  streamInfo(): { url: string; token: string } | null {
    return this.state ? { url: `ws://127.0.0.1:${this.state.port}`, token: this.state.token } : null;
  }

  status(): LiveStatus {
    const s = this.state;
    return {
      sessionId: s?.session?.session_id ?? null, project: s?.project ?? null, pid: s?.pid ?? null,
      helper: s ? { pid: s.helper.pid ?? 0, port: s.port, version: s.version, restarts: s.helper.restarts } : null,
      mode: this.mode, surfaces: this.surfaces.all, alive: !!s && pidAlive(s.pid),
      editor: s?.editor ?? null,
    };
  }

  /**
   * Whether the running session serves `editor` for `project`: the same project (by directory)
   * and either the same editor kind, a manager session (it can open any editor), or a surface of
   * the wanted kind already captured.
   */
  private reusable(project: string, editor: LiveEditorKind): boolean {
    const s = this.state;
    if (!s || s.projectKey !== projectKey(project) || !pidAlive(s.pid)) { return false; }
    if (editor === 'project' || s.editor === null || s.editor === editor || s.editor === 'project') { return true; }
    return this.surfaces.find(surfaceKindFor(editor)) !== undefined;
  }

  /**
   * Resolve the helper binary before anything is launched (a missing helper used to cost a KiCad
   * launch and kill before the error appeared - reviewer finding 20); warns once per call when a
   * configured kiwrap.live.helperPath is not a file and the bundled one is used.
   */
  private helperPath(): string {
    const r = resolveHelper(this.cfg<string>('helperPath', ''), this.context.extensionUri.fsPath);
    if (r.ignoredSetting) {
      const msg = `kiwrap.live.helperPath is set to "${r.ignoredSetting}" but that is not a file; ${r.path ? `using ${r.path}` : 'no bundled kiwrap-live.exe either'}`;
      this.log(msg);
      void vscode.window.showWarningMessage(`Live KiCad: ${msg}`);
    }
    if (!r.path) { throw new Error('kiwrap-live.exe not found: build helper/ or set kiwrap.live.helperPath'); }
    return r.path;
  }

  /**
   * Launch KiCad for a project through kw_live and attach the helper. `targetKicad` is the install
   * to launch (the kiwrap.kicadInstall setting); the project's target KiCad otherwise. The product
   * path (`pref` / kiwrap.live.inputMode = `none`) launches `view`: a visible KiCad window, never
   * activated, streamed into the tab, nothing injected. Only the experimental `events` mode asks
   * kw_live for a `hidden` (cloaked) KiCad.
   */
  async open(project: string, editor: LiveEditorKind, pref?: InputModePreference, targetKicad?: string): Promise<LiveSessionState> {
    const preference = pref ? inputModeOf(pref) : this.inputModeSetting();
    if (this.reusable(project, editor)) { return this.state as LiveSessionState; }
    if (this.state && pidAlive(this.state.pid)) {
      const s = this.state;
      const same = s.projectKey === projectKey(project);
      const label = (k: LiveEditorKind | null) => (k === 'pcb' ? 'PCB editor' : k === 'sch' ? 'schematic editor' : k === 'project' ? 'project manager' : 'KiCad');
      const message = same
        ? `Live KiCad: the running session for this project is a ${label(s.editor)} (pid ${s.pid}) and has no ${label(editor)} window. A standalone editor cannot open the other one: close it and start a ${label(editor)} session instead?`
        : `Live KiCad: a session is running for ${s.project} (pid ${s.pid}). Close it and start one for ${project}?`;
      const action = same ? `Close ${label(s.editor)}, open ${label(editor)}` : 'Close and open';
      if (!(await this.confirmReplace(message, action))) {
        throw new Error(`kept the running ${label(s.editor)} session (pid ${s.pid}); close it first to open the ${label(editor)}`);
      }
      await this.close();
    } else if (this.state) {
      await this.close();
    }
    const helperPath = this.helperPath();
    const kw = await this.ensureClient();
    const launchMode = launchModeFor(preference);
    this.log(`kw_live launch project=${project} editor=${editor} mode=${launchMode}${preference !== 'none' ? ` inputMode=${preference} (experimental)` : ''}${targetKicad ? ` target_kicad=${targetKicad}` : ''}`);
    const session = await kw.launch(project, editor, launchMode, targetKicad);
    this.log(`kw_live launched session=${session.session_id} pid=${session.pid} install=${typeof session.install === 'string' ? session.install : JSON.stringify(session.install)} mode_used=${session.mode_used ?? '?'} cloaked=${String(session.cloaked)}`);
    const resolved = resolveInject(preference, session);
    const inject = resolved.inject;
    if (resolved.warning) { this.log(resolved.warning); void vscode.window.showWarningMessage(`Live KiCad: ${resolved.warning}`); }
    try {
      return await this.attach(session.pid, project, inject, { session, editor, helperPath });
    } catch (e) {
      await kw.detach(project, session.session_id).catch(() => undefined);
      throw e;
    }
  }

  /** Tell kw_live about the helper (screenshots from any kiwrap server then go through it). */
  private async registerHelper(state: LiveSessionState): Promise<void> {
    if (!state.session) { return; }
    try {
      await this.kwLive.attach(state.project, state.session.session_id, { port: state.port, token: state.token, version: state.version, pid: state.helper.pid ?? undefined });
      this.log(`kw_live attach: helper :${state.port} registered for ${state.session.session_id}`);
    } catch (e) { this.log(`kw_live attach failed (screenshots fall back to PrintWindow): ${String(e)}`); }
  }

  /**
   * Attach the helper to an already running KiCad (no kw_live involvement when `opts.session` is
   * null). The helper mints the session token and reports it in its hello line; nothing secret is
   * on its command line. On the product path (`inject: 'none'`) the helper only captures: no
   * `--cloak-expected`, no `--fit-to-view`, the desktop window is left alone. The captured frame
   * is sized to the tab (`--fit-to-view`) only in an experimental input mode (the tab is then the
   * UI) when kiwrap.live.fitWindowToTab is on; `--cloak-expected` only with `events`.
   */
  async attach(pid: number, project: string, inject: InjectMode, opts: AttachOptions & { helperPath?: string } = {}): Promise<LiveSessionState> {
    if (this.state) { await this.close(); }
    const helperPath = opts.helperPath ?? this.helperPath();
    const fitToView = inject !== 'none' && this.cfg<boolean>('fitWindowToTab', true);
    const helper = new HelperProcess({ helperPath, pid, inject, cloakExpected: inject === 'events', fitToView, allowAnyProcess: !!opts.allowAnyProcess, nodePath: this.cfg<string>('nodePath', '') || undefined }, {
      onLog: (l) => this.log(`helper ${l}`),
      onExit: (code, signal, willRestart) => {
        this.log(`helper exited code=${code} signal=${signal} restart=${willRestart}`);
        if (!willRestart && this.state?.helper === helper) { void this.endSession('helper exited'); }
      },
      onHello: (h) => { if (this.state?.helper === helper && (this.state.port !== h.port || this.state.token !== h.token)) { void this.rebindControl(h.port, h.token); } },
    });
    this.log(`spawning ${helperPath} --pid ${pid} --inject ${inject}${fitToView ? ' --fit-to-view' : ''}`);
    const hello = await helper.start();
    const control = new ControlClient(controlUrl(hello.port, hello.token), this.controlEvents());
    const state: LiveSessionState = { project, projectKey: projectKey(project), editor: opts.editor ?? null, session: opts.session ?? null, pid, inject, helper, control, port: hello.port, token: hello.token, version: hello.version };
    this.state = state;
    try {
      await control.connect();
      this.surfaces.reset((await control.surfaces()).map(normalizeSurface).filter((s): s is NonNullable<typeof s> => !!s));
    } catch (e) {
      helper.dispose(); control.dispose(); this.state = null;
      throw e;
    }
    this.startLiveness();
    this._onDidChange.fire(state);
    this.log(`attached pid=${pid} helper v${hello.version} port=${hello.port} surfaces=${this.surfaces.all.map((s) => s.kind).join(',') || 'none'}`);
    void this.registerHelper(state);
    return state;
  }

  /** After a helper restart the port (and token) change: reconnect /control and tell the tabs. */
  private async rebindControl(port: number, token: string): Promise<void> {
    const s = this.state; if (!s) { return; }
    s.control.dispose();
    s.port = port; s.token = token;
    s.control = new ControlClient(controlUrl(port, token), this.controlEvents());
    try { await s.control.connect(); this.surfaces.reset((await s.control.surfaces()).map(normalizeSurface).filter((x): x is NonNullable<typeof x> => !!x)); } catch (e) { this.log(`rebind failed ${String(e)}`); }
    this._onDidChange.fire(s);
    void this.registerHelper(s);
  }

  private controlEvents() {
    return {
      onSurfaces: (list: unknown[]) => this.surfaces.reset(list.map(normalizeSurface).filter((s): s is NonNullable<typeof s> => !!s)),
      onSurfaceCreated: (s: unknown) => { const n = normalizeSurface(s); if (n) { this.surfaces.upsert(n); } },
      onSurfaceChanged: (s: unknown) => { const n = normalizeSurface(s); if (n) { this.surfaces.upsert(n); } },
      onSurfaceDestroyed: (id: string) => this.surfaces.remove(id),
      onProcessExit: () => { void this.endSession('KiCad exited (helper event)'); },
      onHello: (h: { inject?: string; version?: string }) => { this.log(`helper hello inject=${h.inject ?? '?'} v${h.version ?? '?'}`); },
      onLog: (l: string) => this.log(l),
      onClose: (code: number, reason: string) => this.log(`control closed ${code} ${reason}`),
    };
  }

  private startLiveness(): void {
    this.stopLiveness();
    this.livenessTimer = setInterval(() => { if (this.state && !pidAlive(this.state.pid)) { void this.endSession('KiCad exited'); } }, 3000);
  }
  private stopLiveness(): void { if (this.livenessTimer) { clearInterval(this.livenessTimer); this.livenessTimer = null; } }

  private async endSession(reason: string): Promise<void> {
    const s = this.state; if (!s) { return; }
    this.log(`session ended: ${reason}`);
    this.state = null; this.stopLiveness();
    s.helper.dispose(); s.control.dispose();
    this.surfaces.clear();
    this._onDidChange.fire(null);
    if (s.session) { await this.kwLive.detach(s.project, s.session.session_id).catch((e) => this.log(`detach failed ${String(e)}`)); }
  }

  async close(): Promise<void> { await this.endSession('closed'); }

  async raise(surfaceId?: string): Promise<void> {
    const s = this.state; if (!s) { return; }
    const id = surfaceId ?? this.surfaces.all[0]?.id;
    if (id) { await s.control.raise(id); }
    else if (s.session) { await this.kwLive.raise(s.project, s.session.session_id); }
  }

  /** Close the top owned dialog of a surface through the helper (experimental cloaked mode: Esc may not reach it). */
  async closeDialog(surfaceId: string): Promise<boolean> {
    const s = this.state; if (!s) { throw new Error('no live session'); }
    return s.control.closeDialog(surfaceId);
  }

  async screenshot(surfaceId: string, maxPx = 1024): Promise<{ png: Buffer; w?: number; h?: number }> {
    const s = this.state; if (!s) { throw new Error('no live session'); }
    const r = await s.control.screenshot(surfaceId, maxPx);
    return { png: Buffer.from(r.png, 'base64'), w: r.w, h: r.h };
  }

  dispose(): void {
    this.stopLiveness();
    const s = this.state; this.state = null;
    if (s) { s.helper.dispose(); s.control.dispose(); if (s.session) { void this.kwLive.detach(s.project, s.session.session_id).catch(() => undefined); } }
    this._onDidChange.dispose();
  }
}

export function pidAlive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch (e) { return (e as NodeJS.ErrnoException).code === 'EPERM'; }
}
