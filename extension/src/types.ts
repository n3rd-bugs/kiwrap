/**
 * Wire shapes of the kiwrap MCP server (src/kiwrap/core/state/receipts.py: `ReceiptOut`,
 * `ErrorOut`) and the data payloads of the tools this extension calls. Kept free of `vscode`
 * imports so the unit tests (plain mocha) can use them.
 */

export interface ValidatorDelta {
  name: string;
  before: number | null;
  after: number | null;
  extra?: Record<string, unknown>;
}

export interface ImageInfo {
  width: number | null;
  height: number | null;
  bytes: number;
  mime_type: string;
}

/** `ReceiptOut` (structuredContent of every successful kw_* call). */
export interface Receipt<T = Record<string, unknown>> {
  ok: boolean;
  changed: { added: string[]; updated: string[]; removed: string[] };
  validators: ValidatorDelta[];
  provider_used: string;
  degraded: string[];
  snapshot: string | null;
  stage: string | null;
  next_suggested: string | null;
  warnings: string[];
  image_info: ImageInfo | null;
  data: T;
  /** Base64 PNG from the ImageContent block, when the tool returned one (renders, screenshots). */
  imageBase64?: string;
}

/** `ErrorOut` (structuredContent of an isError result) plus a client-side `code` for local failures. */
export interface KiwrapErrorShape {
  code: string;
  message: string;
  why?: string;
  fix?: string;
  candidates?: string[];
  example?: string | null;
  data?: Record<string, unknown>;
}

export class KiwrapError extends Error implements KiwrapErrorShape {
  code: string;
  why: string;
  fix: string;
  candidates: string[];
  example: string | null;
  data: Record<string, unknown>;
  /**
   * True once the extension has already told the user about this error (the format-upgrade modal,
   * the E_FORMAT_NEWER / E_READONLY messages in consent.ts): callers must not show a second popup.
   */
  handled = false;
  constructor(shape: KiwrapErrorShape) {
    super(`${shape.code}: ${shape.message}`);
    this.name = 'KiwrapError';
    this.code = shape.code;
    this.why = shape.why ?? '';
    this.fix = shape.fix ?? '';
    this.candidates = shape.candidates ?? [];
    this.example = shape.example ?? null;
    this.data = shape.data ?? {};
  }
  get shortMessage(): string {
    return this.message.split('\n')[0];
  }
}

/** Whether an error was already shown to the user (`KiwrapError.handled`); duck-typed so it survives a command boundary. */
export function isHandledError(err: unknown): boolean {
  return !!err && typeof err === 'object' && (err as { handled?: unknown }).handled === true;
}

/** A KiwrapError-shaped value (the class, or the same object seen through `executeCommand`). */
export function asKiwrapError(err: unknown): KiwrapError | null {
  if (err instanceof KiwrapError) {
    return err;
  }
  if (err && typeof err === 'object' && typeof (err as { code?: unknown }).code === 'string' && /^E_[A-Z_]+$/.test((err as { code: string }).code)) {
    return err as KiwrapError;
  }
  return null;
}

// ---- kw_env ------------------------------------------------------------------------------------

export interface InstallRow {
  version: string;
  major_minor: string;
  root: string;
  kicad_cli: string;
  share: string;
  source: string;
  python: string | null;
  capabilities: Record<string, unknown> | null;
  capabilities_error?: KiwrapErrorShape;
}

export interface EnvData {
  ok: boolean;
  kiwrap_version: string;
  platform: string;
  installs: InstallRow[];
  default_install: string | null;
  engines: Record<string, unknown>;
  data_backends: Record<string, boolean>;
  storage: Record<string, string | null>;
}

// ---- kw_project ----------------------------------------------------------------------------------

export interface ProjectStatusData {
  ok: boolean;
  project: string;
  dir: string;
  name: string;
  stage: string;
  next_suggested: string | null;
  target_kicad: string | null;
  target_installed: boolean | null;
  hashes: Record<string, Record<string, string>>;
  epochs: { sch: number | null; pcb: number | null };
  files: { sch: string[]; pcb: string[] };
  editor_locks: string[];
  snapshot_head: string | null;
  external_edit: { files: string[]; from: string; to: string } | null;
  history: Record<string, unknown>[];
  /**
   * D21 / review item 1 (B1): the install kiwrap uses for this project, i.e. the pinned target, else
   * the newest point release whose formats equal the files' stamps, else the nearest newer epoch
   * (read-only for writes). Added by the server's `effective_target()`; absent on older servers.
   * `target_kicad_effective` is the review's spelling, read as a fallback.
   */
  target_effective?: string | null;
  target_kicad_effective?: string | null;
  /** Why that install: `pinned` | `file_epoch` | `file_epoch_newer_readonly` | `created`. */
  target_source?: TargetSource | null;
  /** Whether the effective target writes the files' own format (per kind when the server splits it). */
  epoch_match?: boolean | Record<string, boolean | null> | null;
  /** Format / target warnings kw_project status adds (a newer-epoch target, mixed sheet epochs, ...). */
  warnings?: string[];
  /**
   * The KiCad line the files' format belongs to ("10.0"; null when the files mix formats or no known
   * release writes theirs). kw_project status returns it (core/tools/project.py `target_status`); the
   * proactive upgrade modal names it (fixer finding 3: without it the D21 headline case, a 10.0 project
   * with only 10.99 installed, lost "KiCad 10.0" and the winget command).
   */
  project_kicad?: string | null;
  /** How to install that line (`winget install KiCad.KiCad --version 10.0.6`); null when a matching install exists. */
  install_hint?: string | null;
  /** Every design file's stamp, sub-sheets in subdirectories included (`sch/x.kicad_sch`). */
  file_epochs?: { path: string; kind: string; stamp: number | null }[];
  /** Every installed KiCad the server considered: `matches` = it writes the files' own format. */
  candidates?: { version: string; root?: string; formats?: Record<string, number>; reads?: boolean; matches?: boolean }[];
  /**
   * D24: present whenever a write would need a format upgrade - exactly the arguments of
   * `kw_project_upgrade` (the same `upgrade_args()` the E_FORMAT_UPGRADE_REQUIRED refusal carries as
   * `data.upgrade_args`). Absent when no write needs an upgrade, and on servers older than D24.
   */
  upgrade_args?: UpgradeArgs | null;
}

// ---- kw_project_upgrade (D24) ---------------------------------------------------------------------

/**
 * `upgrade_args(choice, project)` (D24): the single source of the `kw_project_upgrade` arguments.
 * The server rebuilds them under the project lock and refuses any difference with E_BAD_ARGS
 * (`data.mismatch`, `data.upgrade_args` = the true ones), so callers pass them verbatim.
 */
export interface UpgradeArgs {
  /** The project's `.kicad_pro` path. */
  project: string;
  /** The project's KiCad line ("10.0"), else `format sch=N,pcb=M`. */
  from_kicad: string;
  /** The install spec the target pins (a version, or an install root when installs sharing that version write different formats). */
  to_kicad: string;
  /** The formats `to_kicad` writes (informational: not a `kw_project_upgrade` argument). */
  to_format?: { sch?: number | null; pcb?: number | null };
  /** The design files that move to the newer format: sorted project-relative posix paths. */
  files: string[];
}

/** The arguments `kw_project_upgrade` takes: `to_format` is informational and not sent. */
export type UpgradeCallArgs = Pick<UpgradeArgs, 'project' | 'from_kicad' | 'to_kicad' | 'files'>;

/** The consent record of a `kw_project_upgrade` receipt (D24; it never claims a human click). */
export interface UpgradeConsentRecord {
  gate: 'anthropic/requiresUserInteraction' | 'elicitation' | 'kiwrap_vscode_modal' | 'none' | (string & {});
  via: 'client_permission_flow' | 'kiwrap_vscode_modal' | 'elicitation' | 'in_process' | 'unknown_client' | (string & {});
  client?: { name?: string | null; version?: string | null };
  entrypoint?: string | null;
}

/** The data of a successful `kw_project_upgrade` receipt (D24). */
export interface UpgradeData {
  files?: { path: string; kind: string; stamp_before: number | null; stamp_after: number | null; changed: boolean }[];
  replaced?: string[];
  /** `.kicad_pro`, `.kicad_prl`, `.kicad_dru`, lib tables and project libraries are never converted. */
  not_converted?: string[];
  /** ERC / DRC counts on the copy before and after the conversion. */
  checks?: { before?: Record<string, unknown> | null; after?: Record<string, unknown> | null };
  /** The snapshot taken first: `kw_snapshot restore <snapshot_before>` undoes the upgrade. */
  snapshot_before?: string | null;
  /** Stage gates dropped because an error count rose (`erc_clean`, `drc_clean`). */
  stage_dropped?: string[];
  target?: { kicad?: string | null; source?: string | null; epoch_match?: boolean | null };
  consent?: UpgradeConsentRecord;
  [key: string]: unknown;
}

export type TargetSource = 'pinned' | 'file_epoch' | 'file_epoch_newer_readonly' | 'created' | (string & {});

/**
 * A well-formed `upgrade_args` object (D24), or null. Tolerant of an older or partial server: the
 * four `kw_project_upgrade` arguments must be present; `to_format` is kept when it is an object.
 * An empty `files` list is null too: the server refuses it as "nothing to convert", so the modal
 * must not offer an [Upgrade] that can only fail (review finding 8).
 */
export function asUpgradeArgs(value: unknown): UpgradeArgs | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const v = value as Record<string, unknown>;
  if (
    typeof v.project !== 'string' ||
    typeof v.from_kicad !== 'string' ||
    typeof v.to_kicad !== 'string' ||
    !Array.isArray(v.files) ||
    v.files.length === 0 ||
    !v.files.every((f) => typeof f === 'string')
  ) {
    return null;
  }
  const out: UpgradeArgs = { project: v.project, from_kicad: v.from_kicad, to_kicad: v.to_kicad, files: [...(v.files as string[])] };
  if (v.to_format && typeof v.to_format === 'object') {
    out.to_format = v.to_format as UpgradeArgs['to_format'];
  }
  return out;
}

/** Exactly the four `kw_project_upgrade` arguments, verbatim (D24: the server compares them to its own). */
export function upgradeCallArgs(args: UpgradeArgs | UpgradeCallArgs): Record<string, unknown> {
  return { project: args.project, from_kicad: args.from_kicad, to_kicad: args.to_kicad, files: [...args.files] };
}

// ---- kw_sch_check / kw_pcb_drc -------------------------------------------------------------------

export interface PosMm {
  x_mm: number;
  y_mm: number;
}

export interface ViolationItem {
  uuid: string;
  description: string;
  pos: PosMm | null;
}

export interface Violation {
  type: string;
  severity: string;
  description: string;
  sheet: string | null;
  excluded: boolean;
  pos: PosMm | null;
  items: ViolationItem[];
  /** kw_pcb_drc only: must_fix | review | cosmetic */
  class?: string;
  note?: string;
}

export interface SchCheckData {
  ok: boolean;
  kicad_version: string | null;
  errors: number;
  warnings: number;
  violations: Violation[];
  violations_truncated: number;
  netlist: Record<string, unknown> | null;
  intent_diff: Record<string, unknown> | null;
}

export interface PcbDrcData {
  ok: boolean;
  kicad_version: string | null;
  errors: number;
  warnings: number;
  must_fix: number;
  review: number;
  cosmetic: number;
  unconnected: number;
  parity_violations: number;
  capped: string[];
  violations: Violation[];
  violations_truncated: number;
  options: { schematic_parity: boolean; refill_zones: boolean; save_board: boolean };
}

export interface PcbCheckData {
  ok: boolean;
  board: string;
  checks: string[];
  filters: Record<string, unknown>;
  summary: Record<string, unknown>;
  items?: Record<string, unknown>[];
  next_cursor?: string | null;
  [key: string]: unknown;
}

// ---- renders -------------------------------------------------------------------------------------

export interface RenderData {
  ok: boolean;
  mode: '3d' | '2d' | 'svg' | 'pdf';
  width?: number | null;
  height?: number | null;
  bytes?: number;
  png: string | null;
  files?: string[];
  pdf?: string;
  layers?: string[];
  side?: string;
  sheet?: string;
  page?: number;
}

// ---- kw_live (contract 2 of the Phase 2 brief; the Python tool is built concurrently) ------------

export type LiveAction = 'launch' | 'attach' | 'status' | 'screenshot' | 'detach' | 'raise';
export type LiveEditor = 'pcb' | 'sch' | 'project';
/** kw_live launch mode: `view` (the product path: a visible, never activated KiCad streamed into the tab) or the experimental `hidden`. */
export type LiveMode = 'view' | 'hidden';

export interface LiveArgs {
  action: LiveAction;
  project?: string;
  editor?: LiveEditor;
  mode?: LiveMode;
  session_id?: string;
  max_px?: number;
  surface?: string;
  /** launch: the install to use (default: the project's target KiCad). */
  target_kicad?: string;
  /** attach: the helper endpoint the extension started ({port, token, version}). */
  helper?: { port: number; token: string; version: string };
  /** launch: how long to wait for the API (s). */
  ready_timeout_s?: number;
}

export interface LiveSessionData {
  ok: boolean;
  session_id: string;
  pid: number;
  install: string;
  api_socket: string | null;
  token: string | null;
  editors: string[];
  alive?: boolean;
  mode?: string;
  mode_used?: string;
  cloaked?: boolean;
  project?: string;
  [key: string]: unknown;
}
