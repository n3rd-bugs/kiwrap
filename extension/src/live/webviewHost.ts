/*
 * LiveView: one webview (custom editor tab or WebviewPanel) showing one KiCad surface. Owns the page
 * html, the host<->webview messages (config, surface, visibility, clipboard bridge, stats, test
 * hooks) and the binding "which surface does this tab show" (kind + optional file for editors, a
 * fixed surface id for panels).
 */
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import type { LiveSessionManager } from './liveSession';
import { KIND_LABEL } from './surfaces';
import { LIVE_SETTINGS_SECTION, type HostToWebview, type Surface, type SurfaceKind, type WebviewClientOptions, type WebviewStats, type WebviewToHost } from '../shared/types';

export type SurfaceSelector = { kind: SurfaceKind; file?: string } | { id: string };

export interface LiveViewHooks {
  /** Called when the page's Start button is pressed (the editor asks kiwrap.live.open for its file). */
  onStart?(view: LiveView): void;
  onDispose?(view: LiveView): void;
}

let instanceSeq = 0;

export function nonce(): string {
  let s = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) { s += chars[Math.floor(Math.random() * chars.length)]; }
  return s;
}

/** Page html with CSP allowing our module script, our stylesheet and a loopback WebSocket. */
export function buildHtml(webview: vscode.Webview, mediaRoot: vscode.Uri, title: string): string {
  const html = fs.readFileSync(path.join(mediaRoot.fsPath, 'live', 'live.html'), 'utf8');
  const n = nonce();
  const js = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, 'live', 'live.js')).toString();
  const css = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, 'live', 'live.css')).toString();
  const csp = `default-src 'none'; img-src ${webview.cspSource} data: blob:; style-src ${webview.cspSource}; font-src ${webview.cspSource}; ` +
    `script-src 'nonce-${n}' ${webview.cspSource}; connect-src ws://127.0.0.1:* ws://localhost:*; worker-src blob:;`;
  return html.replace(/__CSP__/g, csp).replace(/__NONCE__/g, n).replace(/__JS__/g, js).replace(/__CSS__/g, css).replace(/__TITLE__/g, escapeHtml(title));
}

function escapeHtml(s: string): string { return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string)); }

export function clientOptions(): WebviewClientOptions {
  const c = vscode.workspace.getConfiguration(LIVE_SETTINGS_SECTION);
  return { pointerLock: c.get<boolean>('pointerLock', false), hud: c.get<boolean>('hud', true), fit: c.get<'fit' | 'native'>('fit', 'fit'), hiddenCloseDelayMs: c.get<number>('hiddenCloseDelayMs', 1500), raiseOnDoubleClick: c.get<boolean>('raiseOnDoubleClick', true) };
}

export class LiveView implements vscode.Disposable {
  readonly instance = ++instanceSeq;
  private disposables: vscode.Disposable[] = [];
  private surface: Surface | null = null;
  private ready = false;
  private testWaiters = new Map<number, (r: unknown) => void>();
  private testSeq = 0;
  public lastStats: WebviewStats | null = null;
  public visible = true;
  public active = false;

  constructor(
    readonly panel: vscode.WebviewPanel,
    readonly selector: SurfaceSelector,
    private readonly sessions: LiveSessionManager,
    mediaRoot: vscode.Uri,
    private readonly out: vscode.OutputChannel,
    private readonly hooks: LiveViewHooks = {},
    readonly canStart = false,
  ) {
    panel.webview.options = { enableScripts: true, localResourceRoots: [mediaRoot] };
    panel.webview.html = buildHtml(panel.webview, mediaRoot, this.title());
    this.disposables.push(
      panel.webview.onDidReceiveMessage((m: WebviewToHost) => this.onMessage(m)),
      panel.onDidChangeViewState((e) => { this.visible = e.webviewPanel.visible; this.active = e.webviewPanel.active; this.post({ type: 'visible', visible: this.visible, active: this.active }); }),
      sessions.onDidChange(() => { this.surface = this.pick(); this.sendConfig(); }),   // mode / stream endpoint may change with the same surface id
      sessions.surfaces.onChange(() => this.rebind()),
      vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration(LIVE_SETTINGS_SECTION)) { this.sendConfig(); } }),
    );
    panel.onDidDispose(() => this.dispose());
  }

  title(): string {
    if ('id' in this.selector) { return this.surface ? this.surface.title : `KiCad ${this.selector.id}`; }
    return this.selector.file ? path.basename(this.selector.file) : KIND_LABEL[this.selector.kind];
  }

  get boundSurface(): Surface | null { return this.surface; }

  /** Pick the surface this tab shows from the current model. */
  private pick(): Surface | null {
    const m = this.sessions.surfaces;
    if ('id' in this.selector) { return m.get(this.selector.id) ?? null; }
    return m.find(this.selector.kind, this.selector.file) ?? null;
  }

  private rebind(): void {
    const next = this.pick();
    const changed = (next?.id ?? null) !== (this.surface?.id ?? null) || (next && this.surface && (next.title !== this.surface.title || next.cloaked !== this.surface.cloaked));
    this.surface = next;
    if (changed || !next) { this.sendConfig(); }
  }

  private reason(): string {
    const cur = this.sessions.current;
    if (!cur) { return this.canStart ? 'Start a sandboxed KiCad for this project to see it here.' : 'No live KiCad session. Run "kiwrap: Open Live KiCad".'; }
    if (!this.surface) {
      const want = 'id' in this.selector ? this.selector.id : KIND_LABEL[this.selector.kind];
      const running = cur.editor === 'pcb' ? 'a standalone PCB editor' : cur.editor === 'sch' ? 'a standalone Schematic editor' : cur.editor === 'project' ? 'the KiCad project manager' : 'KiCad';
      if ('kind' in this.selector && (cur.editor === 'pcb' || cur.editor === 'sch') && this.selector.kind !== cur.editor) {
        // a standalone editor cannot open the other one (finding 8): say what Start will do
        return `${running} is running (pid ${cur.pid}) and has no ${want} window; a standalone editor cannot open it.` +
          (this.canStart ? ` Start closes that session and opens a ${want} session for this project (you will be asked).` : ' Close the session and open this file live instead.');
      }
      return `${running} is running (pid ${cur.pid}) but has no ${want} window yet. Open it in KiCad or wait for it to appear.`;
    }
    return '';
  }

  sendConfig(): void {
    if (!this.ready) { return; }
    const stream = this.sessions.streamInfo();
    const msg: HostToWebview = { type: 'config', stream: stream && this.surface ? stream : null, surface: this.surface, mode: this.sessions.mode, options: clientOptions(), reason: this.reason(), canStart: this.canStart };
    this.post(msg);
  }

  post(m: HostToWebview): void { void this.panel.webview.postMessage(m); }

  private async onMessage(m: WebviewToHost): Promise<void> {
    switch (m.type) {
      case 'ready': this.ready = true; this.surface = this.pick(); this.sendConfig(); this.post({ type: 'visible', visible: this.panel.visible, active: this.panel.active }); break;
      case 'start': this.hooks.onStart?.(this); break;
      case 'raise': await this.sessions.raise(this.surface?.id).catch((e) => this.out.appendLine(`[live] raise failed ${String(e)}`)); break;
      case 'stats': this.lastStats = m.stats; break;
      case 'clipboard.read': { let text = ''; try { text = await vscode.env.clipboard.readText(); } catch { text = ''; } this.post({ type: 'clipboard.text', text, id: m.id }); break; }
      case 'clipboard.write': try { await vscode.env.clipboard.writeText(m.text); } catch { /* clipboard unavailable */ } break;
      case 'log': this.out.appendLine(`[live:${this.title()}] ${m.level} ${m.message}${m.data ? ' ' + JSON.stringify(m.data) : ''}`); break;
      case 'test.result': { const w = this.testWaiters.get(m.id); if (w) { this.testWaiters.delete(m.id); w(m.result); } break; }
      default: break;
    }
  }

  /** Test hooks: dispatch a synthetic DOM event inside the page and read the page state back. */
  testDispatch(event: Record<string, unknown>): void { this.post({ type: 'test.dispatch', event }); }
  testQuery(timeoutMs = 5000): Promise<unknown> {
    const id = ++this.testSeq;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.testWaiters.delete(id); reject(new Error('test.query timeout')); }, timeoutMs);
      this.testWaiters.set(id, (r) => { clearTimeout(t); resolve(r); });
      this.post({ type: 'test.query', id });
    });
  }

  dispose(): void {
    for (const d of this.disposables.splice(0)) { d.dispose(); }
    this.hooks.onDispose?.(this);
  }
}
