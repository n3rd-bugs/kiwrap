/**
 * The extension's model: the server handle, what kw_env / kw_project last reported, the last
 * validator numbers and the live session state. The tree, the status bar and the diagnostics
 * manager subscribe to `onDidChange`.
 */
import { NOT_FOUND_FIX } from './serverLocator';
import * as vscode from 'vscode';
import { KiwrapServer } from './kiwrapServer';
import type { ClientState, ServerSpec } from './mcpClient';
import { findProjects, type KicadProject } from './project';
import { kicadInstallSetting } from './settings';
import { KiwrapError, type EnvData, type LiveSessionData, type ProjectStatusData } from './types';

export interface ValidatorSummary {
  at: Date;
  ok: boolean;
  /** ERC: errors / warnings; DRC: must_fix / review / cosmetic / unconnected. */
  numbers: Record<string, number>;
  provider: string;
  stage: string | null;
  kicadVersion: string | null;
}

export type LiveState =
  | { kind: 'unavailable'; reason: string }
  | { kind: 'idle' }
  | { kind: 'launching' }
  | { kind: 'running'; session: LiveSessionData }
  | { kind: 'error'; message: string };

export class KiwrapSession implements vscode.Disposable {
  readonly output: vscode.OutputChannel;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  server: KiwrapServer | undefined;
  serverState: ClientState = 'stopped';
  serverDetail = '';
  serverError: string | undefined;
  env: EnvData | undefined;
  envError: string | undefined;
  project: KicadProject | undefined;
  projectStatus: ProjectStatusData | undefined;
  lastErc: ValidatorSummary | undefined;
  lastDrc: ValidatorSummary | undefined;
  live: LiveState = { kind: 'idle' };
  busy = new Set<string>();

  constructor(private readonly context: vscode.ExtensionContext) {
    this.output = vscode.window.createOutputChannel('KiCad (kiwrap)');
    context.subscriptions.push(this.output);
  }

  fire(): void {
    this.changed.fire();
  }

  log(line: string): void {
    const t = new Date().toISOString().slice(11, 19);
    this.output.appendLine(`[${t}] ${line}`);
  }

  /** Create (or re-create) the server handle from a launch spec; does not start it. */
  attachServer(spec: ServerSpec | undefined, error?: string): void {
    if (this.server) {
      void this.server.stop();
    }
    this.serverError = error;
    if (!spec) {
      this.server = undefined;
      this.serverState = 'stopped';
      this.fire();
      return;
    }
    this.log(`server: ${spec.command} ${spec.args.join(' ')} (cwd ${spec.cwd ?? '.'})`);
    this.server = new KiwrapServer(spec, {
      onStderr: (line) => this.output.appendLine(`  ${line}`),
      onState: (state, detail) => {
        this.serverState = state;
        this.serverDetail = detail ?? '';
        this.log(`server ${state}${detail ? `: ${detail}` : ''}`);
        if (state !== 'ready') {
          this.env = undefined;
        }
        this.fire();
      },
      clientInfo: { name: 'kiwrap-vscode', version: this.context.extension.packageJSON.version ?? '0.0.0' },
    });
    this.fire();
  }

  /** The server, started; throws KiwrapError when nothing is configured. */
  async ready(): Promise<KiwrapServer> {
    if (!this.server) {
      throw new KiwrapError({
        code: 'E_CLIENT',
        message: this.serverError ?? 'the kiwrap server is not configured',
        fix: NOT_FOUND_FIX,
      });
    }
    await this.server.start();
    return this.server;
  }

  async refreshEnv(refresh = false): Promise<EnvData | undefined> {
    try {
      const server = await this.ready();
      const r = await server.env(refresh);
      this.env = r.data;
      this.envError = undefined;
      if (this.env.installs.length === 0) {
        this.envError = 'no KiCad installation found';
      }
    } catch (err) {
      this.envError = err instanceof KiwrapError ? err.shortMessage : String(err);
      this.log(`kw_env failed: ${this.envError}`);
    }
    this.fire();
    return this.env;
  }

  /** The workspace's project: the first `.kicad_pro` of the first workspace folder (or a chosen one). */
  pickWorkspaceProject(): KicadProject | undefined {
    if (this.project) {
      return this.project;
    }
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      const found = findProjects(folder.uri.fsPath);
      if (found.length) {
        this.project = found[0];
        return this.project;
      }
    }
    return undefined;
  }

  setProject(p: KicadProject | undefined): void {
    this.project = p;
    this.projectStatus = undefined;
    this.lastErc = undefined;
    this.lastDrc = undefined;
    this.fire();
  }

  async refreshProject(): Promise<ProjectStatusData | undefined> {
    const p = this.pickWorkspaceProject();
    if (!p) {
      this.projectStatus = undefined;
      this.fire();
      return undefined;
    }
    try {
      const server = await this.ready();
      const r = await server.projectStatus(p.pro);
      this.projectStatus = r.data;
    } catch (err) {
      this.log(`kw_project status failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    this.fire();
    return this.projectStatus;
  }

  /** The install the extension targets: the setting, else kw_env's default. */
  selectedInstall(): string | undefined {
    return kicadInstallSetting() ?? this.env?.default_install ?? undefined;
  }

  setBusy(key: string, on: boolean): void {
    if (on) {
      this.busy.add(key);
    } else {
      this.busy.delete(key);
    }
    this.fire();
  }

  dispose(): void {
    void this.server?.stop();
    this.changed.dispose();
  }
}
