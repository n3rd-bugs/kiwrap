/*
 * Shared contract between the Live KiCad module (extension/src/live/**) and the rest of the kiwrap
 * VS Code extension (ext-core). Pure types plus a few constants: no runtime dependencies, no `vscode`
 * import, so this file can be consumed from the extension host, the webview client build and tests.
 *
 * Three protocols meet here (docs/DISPLAY.md section 7, 12.6):
 *   1. helper protocol   kiwrap-live.exe  <->  extension host (/control) and webview (/stream)
 *   2. kw_live tool      extension host   <->  `uv run kiwrap serve` (through the ext-core MCP client)
 *   3. webview messages  extension host   <->  media/live/live.js (postMessage both ways)
 */

// ---------------------------------------------------------------------------------------------------
// 1. Helper protocol (kiwrap-live.exe)
// ---------------------------------------------------------------------------------------------------

/**
 * One JSON line printed by `kiwrap-live.exe --pid <pid> --listen 127.0.0.1:0` on stdout. The helper
 * mints the token itself (never passed on its command line, which any local process can read).
 */
export interface HelperHello {
  port: number;
  token: string;
  version: string;
}

/**
 * `--inject` argument of the helper: `none` = capture-only view (click raises the real window),
 * `events` = PostMessage-only for a cloaked KiCad carrying mr/display-input, `hybrid` = posted input with a
 * borrowed cursor on stock KiCad (DISPLAY.md 12.4, the recommended stock opt-in), `mirror` = SendInput
 * (kept for the contract; kiwrap.live.mirrorInjector picks which one the "mirror" preference uses).
 */
export type InjectMode = 'none' | 'mirror' | 'events' | 'hybrid';

/** Kind of a captured KiCad top-level frame (root of an owner chain). */
export type SurfaceKind = 'pcb' | 'sch' | '3d' | 'mgr' | 'fp' | 'sym' | 'other';

export const SURFACE_KINDS: readonly SurfaceKind[] = ['pcb', 'sch', '3d', 'mgr', 'fp', 'sym', 'other'];

/** Physical-pixel rectangle on the virtual desktop (normalised; the helper sends `[l, t, r, b]`). */
export interface RectPx {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A rect as the helper sends it: `[l, t, r, b]` (rect_px) or `[x, y, w, h]` (dirty). */
export type RectArray = [number, number, number, number];

/** An owned dialog / chooser composited into its surface by the helper. */
export interface SurfaceDialog {
  hwnd: number;
  title: string;
  rect_px: RectPx;
}

/** A surface as reported on /control (normalised by surfaces.ts; the wire form carries `rect_px: [l,t,r,b]`). */
export interface Surface {
  id: string;
  kind: SurfaceKind;
  title: string;
  hwnd: number;
  rect_px: RectPx;
  /** Effective DPI of the frame's monitor (96 = 100 %). */
  dpi: number;
  /** True when the window is DWM-cloaked (hidden mode on a build carrying mr/display-input). */
  cloaked: boolean;
  dialogs: SurfaceDialog[];
  /** Optional helper extras: the editor frame's own rect, foreground / enabled state. */
  frame_px?: RectPx;
  foreground?: boolean;
  enabled?: boolean;
}

/** Requests the extension host sends on /control. `id` is optional correlation; echoed back when present. */
export type ControlRequest =
  | { type: 'surfaces'; id?: number }
  | { type: 'screenshot'; surface: string; max_px?: number; id?: number }
  | { type: 'raise'; surface: string; id?: number }
  | { type: 'keyframe'; surface: string; id?: number }
  | { type: 'close_dialog'; surface: string; id?: number };

/**
 * Messages the helper sends on /control. kiwrap-live sends `hello` (with the current surfaces and the
 * inject mode) on connect and pushes `{type:'event', event:'create'|'destroy'|'title'|'rect'|'process_exit'}`;
 * the client also accepts the older single-surface shapes. Replies echo `id`; failures are either
 * `{type:'error', error}` or the reply with `ok:false` (+ `error` / `reason`).
 */
export type ControlMessage =
  | { type: 'hello'; version?: string; pid?: number; inject?: InjectMode; cloak_expected?: boolean; surfaces?: Surface[] }
  | { type: 'event'; event: 'create' | 'destroy' | 'title' | 'rect' | 'process_exit'; surface?: Surface; id?: string }
  | { type: 'surfaces'; surfaces: Surface[]; id?: number }
  | { type: 'surface_created'; surface: Surface }
  | { type: 'surface_changed'; surface: Surface }
  | { type: 'surface_destroyed'; surface: Pick<Surface, 'id'> | string }
  | { type: 'surface'; event: 'create' | 'change' | 'destroy'; surface: Surface }
  | { type: 'screenshot'; surface: string; ok?: boolean; png?: string; jpeg?: string; w?: number; h?: number; error?: string; id?: number }
  | { type: 'raise'; surface: string; ok: boolean; reason?: string; id?: number }
  | { type: 'keyframe'; surface: string; id?: number }
  | { type: 'ok'; id?: number; for?: string }
  | { type: 'error'; id?: number; message?: string; error?: string; for?: string };

/** Binary stream frame header: 8 bytes, little-endian `[u32 frame_id][u16 w][u16 h]`, then JPEG or WebP. */
export const STREAM_HEADER_BYTES = 8;

export interface StreamFrameHeader {
  frameId: number;
  w: number;
  h: number;
  codec: 'jpeg' | 'webp' | 'unknown';
  mime: string;
  payloadOffset: number;
}

/** Text frame on /stream preceding a size change; `dirty` rects (`[x,y,w,h]` on the wire) are in surface px. */
export interface StreamMeta {
  type: 'meta';
  w: number;
  h: number;
  dpi_scale: number;
  dirty?: Array<RectPx | RectArray>;
  frame_id?: number;
  surface?: string;
  /** Extension: the helper may tell the client which inject mode it runs (mirrors the --inject flag). */
  inject?: InjectMode;
  /** Extension: text copied inside KiCad, relayed to vscode.env.clipboard by the extension host. */
  clipboard?: string;
}

/** Modifier state carried by every input event (the helper also accepts a bitmask ctrl=1 shift=2 alt=4 meta=8 or a name list; the object is unambiguous). */
export interface Modifiers { ctrl: boolean; shift: boolean; alt: boolean; meta: boolean }

/**
 * Input events the webview sends on /stream. Coordinates are surface pixels (physical px of the
 * captured frame, origin top-left of the surface's main frame), already rounded to integers.
 * - mouse: `down` present => button transition (`button` = DOM button 0/1/2/3/4); absent => move.
 *   `buttons` is the DOM `MouseEvent.buttons` bitmask after the transition.
 * - wheel: `dy` positive = wheel rolled away from the user (Windows WHEEL_DELTA sign), in pixels
 *   (lines are converted, 40 px per line); `dx` likewise for horizontal wheels.
 * - key: `key` and `code` are the DOM KeyboardEvent values; `down` false = key up.
 * - focus: `down` true = the view gained keyboard focus, false = it lost it (release everything).
 * - keyframe: please send a full frame (resync of a tab that was hidden while the stream stayed open).
 * - resize (extension, may be ignored by the helper): the view's size in physical px; a hidden-mode
 *   helper may resize the cloaked window to match so the stream is 1:1.
 * - clipboard (extension): text pasted into the view; a helper on the same machine can ignore it because
 *   the OS clipboard is shared.
 */
export type StreamInputEvent =
  | { type: 'mouse'; x: number; y: number; buttons: number; modifiers: Modifiers; button?: number; down?: boolean }
  | { type: 'wheel'; x: number; y: number; dx: number; dy: number; modifiers: Modifiers }
  | { type: 'key'; key: string; code: string; modifiers: Modifiers; down: boolean; repeat?: boolean }
  | { type: 'focus'; down: boolean }
  | { type: 'keyframe' }
  | { type: 'resize'; w: number; h: number; dpi_scale: number }
  | { type: 'clipboard'; text: string };

// ---------------------------------------------------------------------------------------------------
// 2. kw_live (MCP tool) as seen from the extension
// ---------------------------------------------------------------------------------------------------

export type LiveEditorKind = 'pcb' | 'sch' | 'project';
export type LiveLaunchMode = 'auto' | 'hidden' | 'mirror';

export interface KwLiveLaunchArgs {
  action: 'launch';
  project: string;
  editor: LiveEditorKind;
  mode: LiveLaunchMode;
  /** The install to launch (kiwrap.kicadInstall); default: the project's target KiCad. */
  target_kicad?: string;
}

/** What the helper printed on stdout, handed to `kw_live attach` so the server's screenshots go through it. */
export interface KwLiveHelperArg { port: number; token: string; version?: string; host?: string; pid?: number }

export type KwLiveArgs =
  | KwLiveLaunchArgs
  | { action: 'status' | 'detach' | 'raise'; project: string; session_id?: string }
  | { action: 'attach'; project: string; session_id?: string; helper: KwLiveHelperArg }
  | { action: 'screenshot'; project: string; session_id?: string; max_px?: number };

/** `kw_live` launch/status result (structuredContent). Fields beyond the contract are optional. */
export interface KwLiveSession {
  session_id: string;
  pid: number;
  /** Install descriptor: at least a name/path; capabilities when the tool includes them. */
  install: string | { name?: string; path?: string; version?: string; capabilities?: Record<string, boolean> };
  api_socket: string;
  token: string;
  editors: string[];
  /** Optional: the effective input mode the launcher applied (`hidden` => KICAD_INPUT_FROM_EVENTS=cloak). */
  mode?: LiveLaunchMode | 'view';
  /** kiwrap's kw_live: the mode it resolved (`hidden` = cloaked editor, events injection possible). */
  mode_used?: string;
  /** kiwrap's kw_live: DWMWA_CLOAKED read back from the frame after readiness. */
  cloaked?: boolean;
  /** kiwrap's kw_live: the launched install carries the display-input patch. */
  hidden_available?: boolean;
  alive?: boolean;
  project?: string;
}

/**
 * The slice of the ext-core MCP client the live module needs. ext-core passes an implementation to
 * `activateLive(context, { client })`; when absent the module falls back to the command
 * `kiwrap.core.callTool` (name, args) and, failing that, to an explicit "not available" error.
 */
export interface KiwrapClient {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
}

/** Name of the command the live module uses to reach ext-core's client when no object was injected. */
export const CORE_CALL_TOOL_COMMAND = 'kiwrap.core.callTool';

// ---------------------------------------------------------------------------------------------------
// 3. Live module public surface (commands, view types, settings)
// ---------------------------------------------------------------------------------------------------

export const LIVE_EDITOR_VIEW_TYPE = 'kiwrap.live';
export const LIVE_PANEL_VIEW_TYPE = 'kiwrap.live.panel';

export const LIVE_COMMANDS = {
  open: 'kiwrap.live.open',
  openSurface: 'kiwrap.live.openSurface',
  close: 'kiwrap.live.close',
  status: 'kiwrap.live.status',
  raise: 'kiwrap.live.raise',
  forwardKey: 'kiwrap.live.forwardKey',
  screenshot: 'kiwrap.live.screenshot',
  attach: 'kiwrap.live.attach',
} as const;

/** Context keys set by the module (used by the generated keybinding `when` clauses). */
export const LIVE_CONTEXT_KEYS = {
  shield: 'kiwrap.live.shield',
  shieldPalette: 'kiwrap.live.shieldPalette',
  active: 'kiwrap.live.active',
} as const;

export const LIVE_SETTINGS_SECTION = 'kiwrap.live';

/** What the webview shows in its HUD and how it treats input. */
export type ViewMode = 'view' | 'mirror' | 'hidden';

export function viewModeForInject(inject: InjectMode): ViewMode {
  return inject === 'events' ? 'hidden' : inject === 'mirror' || inject === 'hybrid' ? 'mirror' : 'view';
}

export interface LiveStatus {
  sessionId: string | null;
  project: string | null;
  pid: number | null;
  helper: { pid: number; port: number; version: string; restarts: number } | null;
  mode: ViewMode;
  surfaces: Surface[];
  alive: boolean;
  /** The editor kind kw_live launched (null for a bare attach or no session). */
  editor?: LiveEditorKind | null;
}

// ---------------------------------------------------------------------------------------------------
// 4. Webview <-> extension host messages
// ---------------------------------------------------------------------------------------------------

export interface WebviewClientOptions {
  pointerLock: boolean;
  hud: boolean;
  fit: 'fit' | 'native';
  /** Milliseconds to wait before closing the stream when the tab is hidden. */
  hiddenCloseDelayMs: number;
}

export type HostToWebview =
  | {
      type: 'config';
      stream: { url: string; token: string } | null;
      surface: Surface | null;
      mode: ViewMode;
      options: WebviewClientOptions;
      /** Why there is no stream yet, shown on the placeholder card. */
      reason?: string;
      /** Whether a session can be started from this view (shows the start button). */
      canStart?: boolean;
    }
  | { type: 'surface'; surface: Surface | null }
  | { type: 'visible'; visible: boolean; active: boolean }
  | { type: 'clipboard.text'; text: string; id?: number }
  | { type: 'forwardedKey'; key: string }
  | { type: 'test.dispatch'; event: Record<string, unknown> }
  | { type: 'test.query'; id: number };

export interface WebviewStats {
  fps: number;
  kbps: number;
  frames: number;
  decodeMs: number;
  inputToFrameMs: number | null;
  w: number;
  h: number;
  scale: number;
  dpr: number;
  connected: boolean;
  focused: boolean;
  lastFrameId: number;
}

export type WebviewToHost =
  | { type: 'ready' }
  | { type: 'start' }
  | { type: 'raise' }
  | { type: 'stats'; stats: WebviewStats }
  | { type: 'clipboard.read'; id: number }
  | { type: 'clipboard.write'; text: string }
  | { type: 'log'; level: 'info' | 'warn' | 'error'; message: string; data?: unknown }
  | { type: 'test.result'; id: number; result: unknown };
