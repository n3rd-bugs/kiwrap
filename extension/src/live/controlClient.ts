/*
 * /control WebSocket client (extension host -> helper). Text JSON both ways.
 * Uses the global WebSocket of the extension host's Node (>= 22) unless a constructor is injected
 * (tests pass `ws`). Requests carry an `id`; replies are matched by id when the helper echoes it and
 * by FIFO-per-type otherwise. Surface pushes are folded into a SurfacesModel by the caller.
 */
import type { ControlMessage, ControlRequest, Surface } from '../shared/types';

export interface WebSocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(type: 'open' | 'close' | 'error' | 'message', listener: (ev: any) => void): void;
}
export type WebSocketCtor = new (url: string) => WebSocketLike;

export function defaultWebSocketCtor(): WebSocketCtor {
  const g = globalThis as unknown as { WebSocket?: WebSocketCtor };
  if (!g.WebSocket) { throw new Error('global WebSocket is not available in this extension host (Node < 22?)'); }
  return g.WebSocket;
}

/** Failure text of a reply, or null when it is a success. */
export function replyError(m: ControlMessage): string | null {
  if (m.type === 'error') { return m.error ?? m.message ?? 'helper error'; }
  const o = m as { ok?: boolean; error?: string; reason?: string };
  if (o.ok === false) { return o.error ?? o.reason ?? `${m.type} failed`; }
  return null;
}

export function controlUrl(port: number, token: string): string {
  return `ws://127.0.0.1:${port}/control?token=${encodeURIComponent(token)}`;
}

interface Pending { id: number; type: string; resolve(m: ControlMessage): void; reject(e: Error): void; timer: NodeJS.Timeout }

export interface ControlEvents {
  onSurfaces?(list: Surface[]): void;
  onSurfaceCreated?(s: Surface): void;
  onSurfaceChanged?(s: Surface): void;
  onSurfaceDestroyed?(id: string): void;
  /** The helper's own hello on connect (inject mode as it runs, cloak expectation). */
  onHello?(hello: { inject?: string; cloak_expected?: boolean; version?: string }): void;
  /** The captured process exited (the helper keeps running until we kill it). */
  onProcessExit?(): void;
  onOpen?(): void;
  onClose?(code: number, reason: string): void;
  onLog?(line: string): void;
}

export class ControlClient {
  private ws: WebSocketLike | null = null;
  private seq = 0;
  private pending: Pending[] = [];
  private closed = false;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private attempts = 0;
  public connected = false;

  constructor(private readonly url: string, private readonly events: ControlEvents = {}, private readonly Ctor: WebSocketCtor = defaultWebSocketCtor()) {}

  /** Connect; resolves on open, rejects on the first failure (later drops reconnect silently). */
  connect(timeoutMs = 5000): Promise<void> {
    this.closed = false;
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const ws = new this.Ctor(this.url);
      this.ws = ws;
      const timer = setTimeout(() => { if (!settled) { settled = true; reject(new Error('control socket open timeout')); try { ws.close(); } catch { /* ignore */ } } }, timeoutMs);
      ws.addEventListener('open', () => { this.connected = true; this.attempts = 0; if (!settled) { settled = true; clearTimeout(timer); resolve(); } this.events.onOpen?.(); });
      ws.addEventListener('message', (ev: { data: unknown }) => { this.onMessage(typeof ev.data === 'string' ? ev.data : String(ev.data)); });
      ws.addEventListener('error', (ev: { message?: string }) => { this.events.onLog?.(`control socket error ${ev?.message ?? ''}`); });
      ws.addEventListener('close', (ev: { code: number; reason: string }) => {
        this.connected = false;
        if (this.ws === ws) { this.ws = null; }
        for (const p of this.pending.splice(0)) { clearTimeout(p.timer); p.reject(new Error('control socket closed')); }
        if (!settled) { settled = true; clearTimeout(timer); reject(new Error(`control socket closed (${ev.code} ${ev.reason})`)); }
        this.events.onClose?.(ev.code, ev.reason);
        if (!this.closed) { this.scheduleReconnect(); }
      });
    });
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer || this.closed) { return; }
    const delay = Math.min(5000, 250 * 2 ** Math.min(this.attempts++, 5));
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; if (!this.closed) { this.connect().catch(() => { /* onClose reschedules */ }); } }, delay);
  }

  private onMessage(text: string): void {
    let m: ControlMessage;
    try { m = JSON.parse(text) as ControlMessage; } catch { this.events.onLog?.(`control: bad JSON ${text.slice(0, 120)}`); return; }
    if (!m || typeof m !== 'object' || typeof (m as { type?: unknown }).type !== 'string') { return; }
    // Replies: match by id, else FIFO for that type (or an 'ok'/'error' for any pending request).
    const id = (m as { id?: number }).id;
    let idx = -1;
    if (typeof id === 'number') { idx = this.pending.findIndex((p) => p.id === id); }
    if (idx < 0 && (m.type === 'ok' || m.type === 'error')) { idx = this.pending.findIndex((p) => p.type === ((m as { for?: string }).for ?? p.type)); }
    if (idx < 0 && (m.type === 'surfaces' || m.type === 'screenshot')) { idx = this.pending.findIndex((p) => p.type === m.type); }
    if (idx >= 0) { const [p] = this.pending.splice(idx, 1); clearTimeout(p.timer); p.resolve(m); }
    switch (m.type) {
      case 'hello': this.events.onHello?.(m); if (Array.isArray(m.surfaces)) { this.events.onSurfaces?.(m.surfaces); } break;
      case 'event':
        if (m.event === 'process_exit') { this.events.onProcessExit?.(); }
        else if (m.event === 'destroy') { const id = m.id ?? m.surface?.id; if (id) { this.events.onSurfaceDestroyed?.(id); } }
        else if (m.surface) { if (m.event === 'create') { this.events.onSurfaceCreated?.(m.surface); } else { this.events.onSurfaceChanged?.(m.surface); } }
        break;
      case 'surfaces': this.events.onSurfaces?.(Array.isArray(m.surfaces) ? m.surfaces : []); break;
      case 'surface_created': this.events.onSurfaceCreated?.(m.surface); break;
      case 'surface_changed': this.events.onSurfaceChanged?.(m.surface); break;
      case 'surface_destroyed': this.events.onSurfaceDestroyed?.(typeof m.surface === 'string' ? m.surface : m.surface.id); break;
      case 'surface':
        if (m.event === 'create') { this.events.onSurfaceCreated?.(m.surface); }
        else if (m.event === 'destroy') { this.events.onSurfaceDestroyed?.(m.surface.id); }
        else { this.events.onSurfaceChanged?.(m.surface); }
        break;
      default: break;
    }
  }

  request(req: ControlRequest, timeoutMs = 10_000): Promise<ControlMessage> {
    const ws = this.ws;
    if (!ws || ws.readyState !== 1) { return Promise.reject(new Error('control socket not connected')); }
    const id = ++this.seq;
    return new Promise<ControlMessage>((resolve, reject) => {
      const timer = setTimeout(() => { this.pending = this.pending.filter((p) => p.id !== id); reject(new Error(`control request ${req.type} timed out`)); }, timeoutMs);
      this.pending.push({ id, type: req.type, resolve: (m) => { const err = replyError(m); if (err) { reject(new Error(err)); } else { resolve(m); } }, reject, timer });
      ws.send(JSON.stringify({ ...req, id }));
    });
  }

  async surfaces(): Promise<Surface[]> {
    const m = await this.request({ type: 'surfaces' });
    return m.type === 'surfaces' ? m.surfaces : [];
  }

  async screenshot(surface: string, maxPx = 1024): Promise<{ png: string; w?: number; h?: number }> {
    const m = await this.request({ type: 'screenshot', surface, max_px: maxPx }, 15_000);
    if (m.type !== 'screenshot') { throw new Error(`unexpected reply ${m.type}`); }
    if (!m.png) { throw new Error(m.jpeg ? 'helper returned jpeg, png expected' : 'screenshot reply has no png'); }
    return { png: m.png, w: m.w, h: m.h };
  }

  async raise(surface: string): Promise<void> { await this.request({ type: 'raise', surface }, 5000).catch((e: Error) => { if (!/timed out/.test(e.message)) { throw e; } }); }

  /** Ask for a full frame on a surface's streams (kiwrap-live `keyframe`; older helpers answer with an error we ignore). */
  async keyframe(surface: string): Promise<void> { await this.request({ type: 'keyframe', surface }, 3000).catch(() => undefined); }

  /**
   * WM_CLOSE the top owned dialog of a surface (kiwrap-live `close_dialog`): the way to dismiss a
   * DIALOG_SHIM whose grid editor eats Esc in hidden mode (docs/research/display/win-input-patch.md 5).
   */
  async closeDialog(surface: string): Promise<boolean> {
    const m = await this.request({ type: 'close_dialog', surface }, 5000);
    return (m as { ok?: boolean }).ok !== false;
  }

  dispose(): void {
    this.closed = true;
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
    const ws = this.ws; this.ws = null;
    if (ws) { try { ws.close(1000, 'dispose'); } catch { /* ignore */ } }
    for (const p of this.pending.splice(0)) { clearTimeout(p.timer); p.reject(new Error('disposed')); }
  }
}
