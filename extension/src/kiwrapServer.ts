/**
 * The kiwrap server as seen by the extension: how it is launched (`uv run kiwrap serve` from the
 * kiwrap checkout, with `--storage-home` under the extension's global storage) and typed wrappers
 * over the kw_* tools the extension calls (kw_env, kw_project, kw_project_upgrade, kw_sch_check,
 * kw_pcb_drc, kw_pcb_check, kw_sch_render, kw_pcb_render, kw_live).
 *
 * No `vscode` import: the unit tests run it against test/unit/fakeServer.ts.
 */
import { McpClient, type CallOptions, type McpClientOptions, type ServerSpec } from './mcpClient';
import {
  asUpgradeArgs,
  upgradeCallArgs,
  type EnvData,
  type LiveArgs,
  type LiveSessionData,
  type PcbCheckData,
  type PcbDrcData,
  type ProjectStatusData,
  type Receipt,
  type RenderData,
  type SchCheckData,
  type UpgradeArgs,
  type UpgradeCallArgs,
  type UpgradeData,
} from './types';

export interface LaunchSettings {
  /** `uv` executable (kiwrap.uvPath). */
  uvPath: string;
  /** The kiwrap checkout / uv project that provides `kiwrap serve` (kiwrap.serverProject). */
  serverProject: string;
  /** kiwrap's state directory (`--storage-home`); empty = kiwrap's own default (KIWRAP_HOME or the
   *  platform default), so the extension's server and the agent's (.mcp.json) share one home:
   *  installs registry, sandbox roots and kw_live session records (docs/EXTENSION.md section 6). */
  storageHome: string;
  /** Extra environment for the server process. */
  env?: Record<string, string>;
}

/** `uv run --project <serverProject> kiwrap [--storage-home <home>] serve`, cwd = the kiwrap checkout. */
export function buildServerSpec(s: LaunchSettings): ServerSpec {
  const args = ['run', '--project', s.serverProject, 'kiwrap'];
  const env: Record<string, string> = { ...(process.env as Record<string, string>) };
  if (s.storageHome) {
    args.push('--storage-home', s.storageHome);
    env.KIWRAP_HOME = s.storageHome;
  }
  args.push('serve');
  return { command: s.uvPath || 'uv', args, cwd: s.serverProject, env: { ...env, ...(s.env ?? {}) } };
}

export interface SchCheckOptions {
  severity?: 'all' | 'error' | 'warning' | 'exclusions';
  netlist?: boolean;
}

export interface PcbDrcOptions {
  schematic_parity?: boolean;
  refill_zones?: boolean | null;
  save_board?: boolean | null;
  severity?: 'all' | 'error' | 'warning' | 'exclusions';
}

export interface PcbCheckOptions {
  checks?: ('overlaps' | 'off_board' | 'unrouted' | 'drc_summary')[];
  refs?: string[];
  nets?: string[];
  bbox_mm?: number[];
  cursor?: string;
  limit?: number;
  provider?: 'auto' | 'ipc11' | 'cli' | 'file';
}

export interface SchRenderOptions {
  sheet?: string;
  page?: number;
  max_px?: number;
}

export interface PcbRenderOptions {
  view?: '3d' | '2d';
  side?: 'top' | 'bottom';
  layers?: string[];
  max_px?: number;
}

export class KiwrapServer {
  readonly client: McpClient;

  constructor(spec: ServerSpec, opts: McpClientOptions = {}) {
    this.client = new McpClient(spec, opts);
  }

  start(): Promise<void> {
    return this.client.start();
  }

  stop(): Promise<void> {
    return this.client.stop();
  }

  restart(): Promise<void> {
    return this.client.restart();
  }

  get state() {
    return this.client.state;
  }

  get pid() {
    return this.client.pid;
  }

  hasTool(name: string): boolean {
    return this.client.hasTool(name);
  }

  // ---- typed wrappers ---------------------------------------------------------------------------

  env(refresh = false): Promise<Receipt<EnvData>> {
    return this.client.call<EnvData>('kw_env', { refresh }, { interactive: false });
  }

  /** kw_project status: a background read for the tree, never a modal. */
  projectStatus(project: string): Promise<Receipt<ProjectStatusData>> {
    return this.client.call<ProjectStatusData>('kw_project', { action: 'status', project }, { interactive: false });
  }

  /**
   * kw_project retarget (review item 1 fix layer 1, contract (c)): pins the project's target KiCad
   * in `.kiwrap/state.json` without touching the design files.
   */
  projectRetarget(project: string, targetKicad: string, call?: CallOptions): Promise<Receipt<Record<string, unknown>>> {
    return this.client.call('kw_project', { action: 'retarget', project, target_kicad: targetKicad }, call);
  }

  /**
   * `kw_project_upgrade` (D24): the only way kiwrap converts design files to a newer KiCad format.
   * Called only after the user clicked [Upgrade] in the extension's modal (this client identifies as
   * `kiwrap-vscode`, so the server records consent gate/via `kiwrap_vscode_modal`): `call.consent`
   * must be `MODAL_CONSENT`, else `McpClient.call` refuses without sending anything (review
   * finding 6). The arguments are the server's own `upgrade_args` passed verbatim; `to_format` is
   * informational and dropped.
   */
  projectUpgrade(args: UpgradeArgs | UpgradeCallArgs, call?: CallOptions): Promise<Receipt<UpgradeData>> {
    return this.client.call<UpgradeData>('kw_project_upgrade', upgradeCallArgs(args), call);
  }

  /**
   * The `kw_project_upgrade` arguments of a project from `kw_project status` (D24: status returns
   * `upgrade_args` whenever a write would need an upgrade). A background read: never a modal.
   * null when no write needs an upgrade, or on a server older than D24.
   */
  async upgradeArgs(project: string): Promise<UpgradeArgs | null> {
    const st = await this.client.call<ProjectStatusData>(
      'kw_project',
      { action: 'status', project },
      { interactive: false, handleErrors: false },
    );
    return asUpgradeArgs(st.data.upgrade_args);
  }

  projectCreate(dir: string, name: string, targetKicad?: string, boardSizeMm?: [number, number]) {
    const args: Record<string, unknown> = { action: 'create', dir, name };
    if (targetKicad) {
      args.target_kicad = targetKicad;
    }
    if (boardSizeMm) {
      args.board_size_mm = boardSizeMm;
    }
    return this.client.call<Record<string, unknown>>('kw_project', args);
  }

  schCheck(project: string, opts: SchCheckOptions = {}, call?: CallOptions): Promise<Receipt<SchCheckData>> {
    return this.client.call<SchCheckData>('kw_sch_check', { project, ...compact(opts) }, call);
  }

  pcbDrc(project: string, opts: PcbDrcOptions = {}, call?: CallOptions): Promise<Receipt<PcbDrcData>> {
    return this.client.call<PcbDrcData>('kw_pcb_drc', { project, ...compact(opts) }, call);
  }

  pcbCheck(project: string, opts: PcbCheckOptions = {}, call?: CallOptions): Promise<Receipt<PcbCheckData>> {
    return this.client.call<PcbCheckData>('kw_pcb_check', { project, ...compact(opts) }, call);
  }

  schRender(project: string, opts: SchRenderOptions = {}, call?: CallOptions): Promise<Receipt<RenderData>> {
    return this.client.call<RenderData>('kw_sch_render', { project, ...compact(opts) }, call);
  }

  pcbRender(project: string, opts: PcbRenderOptions = {}, call?: CallOptions): Promise<Receipt<RenderData>> {
    return this.client.call<RenderData>('kw_pcb_render', { project, ...compact(opts) }, call);
  }

  /** kw_live (Phase 2 contract 2). Throws E_NO_TOOL while the server does not publish it. */
  live(args: LiveArgs, call?: CallOptions): Promise<Receipt<LiveSessionData>> {
    return this.client.call<LiveSessionData>('kw_live', compact(args), call);
  }

  get liveAvailable(): boolean {
    return this.client.hasTool('kw_live');
  }
}

/** Drop undefined values so the server sees its own defaults. */
function compact(obj: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    if (v !== undefined) {
      out[k] = v;
    }
  }
  return out;
}
