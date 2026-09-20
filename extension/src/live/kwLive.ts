/*
 * kw_live as seen from the extension, free of `vscode` imports (unit-tested with mocha).
 * Tool results may come back as an MCP CallToolResult (structuredContent / content[0].text JSON),
 * as ext-core's Receipt (payload under `data`) or as the plain structured object; `unwrapToolResult`
 * normalises all of them.
 */
import type { KiwrapClient, KwLiveArgs, KwLiveHelperArg, KwLiveSession, LiveLaunchMode } from '../shared/types';

export function unwrapToolResult(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== 'object') { throw new Error(`kw_live returned ${typeof raw}`); }
  const o = raw as Record<string, unknown>;
  if (o.isError) {
    const msg = Array.isArray(o.content) ? (o.content as Array<{ text?: string }>).map((c) => c.text ?? '').join('\n') : JSON.stringify(o);
    throw new Error(`kw_live error: ${msg}`);
  }
  if (o.structuredContent && typeof o.structuredContent === 'object') { return unwrapToolResult(o.structuredContent); }
  // ext-core's McpClient.call() returns a Receipt: the tool payload sits under `data`.
  if ('ok' in o && 'data' in o && o.data && typeof o.data === 'object' && 'validators' in o) { return o.data as Record<string, unknown>; }
  if (Array.isArray(o.content)) {
    const text = (o.content as Array<{ type?: string; text?: string }>).find((c) => c.type === 'text' && typeof c.text === 'string')?.text;
    if (text) { try { return JSON.parse(text) as Record<string, unknown>; } catch { return { text }; } }
  }
  return o;
}

export class KwLive {
  constructor(private readonly client: KiwrapClient | null) {}

  get available(): boolean { return this.client !== null; }

  private async call(args: KwLiveArgs): Promise<Record<string, unknown>> {
    if (!this.client) { throw new Error('kiwrap core client is not available (ext-core did not register kiwrap.core.callTool); use "Attach to running KiCad" instead'); }
    return unwrapToolResult(await this.client.callTool('kw_live', args as unknown as Record<string, unknown>));
  }

  async launch(project: string, editor: 'pcb' | 'sch' | 'project', mode: LiveLaunchMode, targetKicad?: string): Promise<KwLiveSession> {
    return asSession(await this.call(targetKicad ? { action: 'launch', project, editor, mode, target_kicad: targetKicad } : { action: 'launch', project, editor, mode }));
  }
  async status(project: string, sessionId?: string): Promise<KwLiveSession | null> {
    const r = await this.call({ action: 'status', project, session_id: sessionId });
    return r && typeof r.pid === 'number' ? asSession(r) : null;
  }
  /** Register the helper the extension started, so `kw_live screenshot` (from any server) uses it. */
  async attach(project: string, sessionId: string | undefined, helper: KwLiveHelperArg): Promise<void> {
    await this.call({ action: 'attach', project, session_id: sessionId, helper });
  }
  async detach(project: string, sessionId?: string): Promise<void> { await this.call({ action: 'detach', project, session_id: sessionId }); }
  async raise(project: string, sessionId?: string): Promise<void> { await this.call({ action: 'raise', project, session_id: sessionId }); }
  async screenshot(project: string, sessionId?: string, maxPx = 1024): Promise<Record<string, unknown>> { return this.call({ action: 'screenshot', project, session_id: sessionId, max_px: maxPx }); }
}

export function asSession(o: Record<string, unknown>): KwLiveSession {
  const pid = Number(o.pid);
  if (!Number.isInteger(pid) || pid <= 0) { throw new Error(`kw_live result has no pid: ${JSON.stringify(o).slice(0, 300)}`); }
  return {
    session_id: String(o.session_id ?? ''), pid,
    install: (o.install as KwLiveSession['install']) ?? '', api_socket: String(o.api_socket ?? ''), token: String(o.token ?? ''),
    editors: Array.isArray(o.editors) ? (o.editors as string[]) : [],
    mode: o.mode as KwLiveSession['mode'], alive: o.alive as boolean | undefined, project: o.project as string | undefined,
    mode_used: typeof o.mode_used === 'string' ? o.mode_used : undefined,
    cloaked: typeof o.cloaked === 'boolean' ? o.cloaked : undefined,
    hidden_available: typeof o.hidden_available === 'boolean' ? o.hidden_available : undefined,
  };
}

/**
 * True when the launched KiCad can be driven with posted messages (docs/DISPLAY.md 12.5, the
 * experimental `events` input mode): kiwrap's kw_live resolved `mode_used: 'hidden'`
 * (KICAD_INPUT_FROM_EVENTS=cloak exported, DWMWA_CLOAKED read back) or the frame reports `cloaked`.
 * A session kw_live resolved to `view` (or the older `mirror`) is never events-capable, whatever
 * the build carries: the variable is only exported for hidden sessions. Without a resolved mode
 * (older servers, tests) the launch request and the install's capability decide.
 */
export function sessionSupportsEvents(s: KwLiveSession): boolean {
  if (s.mode_used === 'hidden' || s.cloaked === true) { return true; }
  if (s.mode_used === 'view' || s.mode_used === 'mirror') { return false; }
  if (s.mode === 'hidden') { return true; }
  if (s.install && typeof s.install === 'object' && s.install.capabilities) { return s.install.capabilities['gui.input_from_events'] === true; }
  return false;
}
