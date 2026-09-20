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
export type LiveMode = 'auto' | 'hidden' | 'mirror';

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
