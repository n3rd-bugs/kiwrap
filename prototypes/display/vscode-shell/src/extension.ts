/*
 * kiwrap display shell - prototype VS Code extension.
 *
 * What it does (see README.md):
 *  - a WebviewPanel "KiCad view" that hosts either an <iframe> to a localhost page (noVNC / Xpra HTML5 /
 *    Broadway / our own stream) or an inline <canvas> + WebSocket frame client;
 *  - a CustomReadonlyEditorProvider for *.kicad_pcb / *.kicad_sch so "open the board = see KiCad";
 *  - an input probe page (media/probe.html) that logs every keydown/keyup/wheel/pointer event it
 *    receives, plus DPI/clipboard/pointer-lock/fullscreen checks, to a JSONL file for the driver;
 *  - a tiny HTTP server (demo iframe target, exercised through webview.portMapping) and a tiny
 *    WebSocket frame server (synthetic RGBA frames + input echo with timestamps) so the canvas client
 *    and the protocol sketch can be measured without a real backend;
 *  - a control channel: the driver appends JSON lines to <logDir>/cmd.jsonl and the extension executes
 *    them (executeCommand, clipboard, post to webview, open file), writing results to the log.
 *
 * Nothing here talks to KiCad. The backend contract is described in README.md.
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as http from 'http';
import * as path from 'path';
import { WebSocketServer, WebSocket } from 'ws';

const VIEW_TYPE = 'kiwrapShell.view';
const EDITOR_TYPE = 'kiwrapShell.boardEditor';

let out: vscode.OutputChannel;
let logFile = '';
let cmdFile = '';
let cmdOffset = 0;
let httpPort = 0;
let wsPort = 0;
let activePanel: vscode.WebviewPanel | undefined;
let extUri: vscode.Uri;
let instanceSeq = 0;
// Live KiCad surfaces (winstream helper): one webview per KiCad editor frame, keyed by surface kind.
const livePanels = new Map<string, vscode.Webview>();
type Page = 'probe' | 'view' | 'canvas' | 'live';
function surfaceForFile(fsPath: string): string {
  const ext = path.extname(fsPath).toLowerCase();
  return ext === '.kicad_pcb' ? 'pcb' : ext === '.kicad_sch' ? 'sch' : ext === '.kicad_pro' ? 'mgr' : 'pcb';
}

function cfg<T>(key: string, dflt: T): T {
  return vscode.workspace.getConfiguration('kiwrapShell').get<T>(key, dflt);
}

function log(kind: string, data: Record<string, unknown> = {}): void {
  const rec = { t: Date.now(), kind, ...data };
  const line = JSON.stringify(rec);
  out.appendLine(line);
  if (logFile) {
    try { fs.appendFileSync(logFile, line + '\n'); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------- demo HTTP server (iframe target)
function startHttp(mediaDir: string): Promise<number> {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const url = (req.url ?? '/').split('?')[0];
      const file = url === '/' || url === '/demo.html' ? 'demo.html' : url === '/probe.js' ? 'probe.js' : '';
      if (!file) { res.writeHead(404); res.end('not found'); return; }
      const body = fs.readFileSync(path.join(mediaDir, file));
      res.writeHead(200, { 'content-type': file.endsWith('.js') ? 'text/javascript' : 'text/html', 'cache-control': 'no-store' });
      res.end(body);
    });
    srv.listen(0, '127.0.0.1', () => resolve((srv.address() as { port: number }).port));
  });
}

// ---------------------------------------------------------------- synthetic frame server (canvas mode)
// Protocol sketch (binary frames, little-endian header):
//   server -> client  u8 type=1 (frame) | u32 seq | f64 t_server_ms | u16 w | u16 h | u8 fmt(0=RGBA8) | pixels
//   server -> client  text JSON {type:'echo', id, t_client, t_server}  (round trip probe for input)
//   client -> server  text JSON {type:'input', id, t_client, ev:{kind, ...}}  and {type:'resize', w, h, dpr}
function startWs(): Promise<number> {
  return new Promise((resolve) => {
    const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
    wss.on('listening', () => resolve((wss.address() as { port: number }).port));
    wss.on('connection', (sock: WebSocket) => {
      let w = 640, h = 400, seq = 0;
      let px = Buffer.alloc(w * h * 4);
      const header = 1 + 4 + 8 + 2 + 2 + 1;
      const timer = setInterval(() => {
        if (sock.readyState !== WebSocket.OPEN || sock.bufferedAmount > 4 * w * h * 4) { return; }
        seq++;
        // moving gradient + a vertical bar whose x encodes seq (visual latency check)
        const bar = (seq * 8) % w;
        for (let y = 0; y < h; y++) {
          for (let x = 0; x < w; x++) {
            const i = (y * w + x) * 4;
            const inBar = Math.abs(x - bar) < 6;
            px[i] = inBar ? 255 : (x + seq) & 255;
            px[i + 1] = inBar ? 255 : (y + seq * 2) & 255;
            px[i + 2] = inBar ? 0 : 96;
            px[i + 3] = 255;
          }
        }
        const buf = Buffer.alloc(header + px.length);
        let o = 0;
        buf.writeUInt8(1, o); o += 1;
        buf.writeUInt32LE(seq, o); o += 4;
        buf.writeDoubleLE(Date.now(), o); o += 8;
        buf.writeUInt16LE(w, o); o += 2;
        buf.writeUInt16LE(h, o); o += 2;
        buf.writeUInt8(0, o); o += 1;
        px.copy(buf, o);
        sock.send(buf);
      }, 1000 / 30);
      sock.on('message', (raw) => {
        try {
          const m = JSON.parse(raw.toString());
          if (m.type === 'resize') { w = Math.max(16, Math.min(1920, m.w | 0)); h = Math.max(16, Math.min(1200, m.h | 0)); px = Buffer.alloc(w * h * 4); }
          else if (m.type === 'input') { sock.send(JSON.stringify({ type: 'echo', id: m.id, t_client: m.t_client, t_server: Date.now(), kind: m.ev?.kind })); }
        } catch { /* ignore */ }
      });
      sock.on('close', () => clearInterval(timer));
    });
  });
}

// ---------------------------------------------------------------- webview HTML
function nonce(): string { return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2); }

function pageHtml(webview: vscode.Webview, page: Page, extra: Record<string, string>): string {
  const mediaDir = vscode.Uri.joinPath(extUri, 'media');
  let html = fs.readFileSync(path.join(mediaDir.fsPath, page + '.html'), 'utf8');
  const n = nonce();
  const probeJs = webview.asWebviewUri(vscode.Uri.joinPath(mediaDir, 'probe.js')).toString();
  const iframeUrl = extra.url || `http://localhost:${httpPort}/demo.html`;
  const wsUrl = extra.wsUrl || `ws://127.0.0.1:${wsPort}/`;
  // CSP: allow our nonce scripts, the localhost iframe, and the local websocket.
  const csp = `default-src 'none'; img-src ${webview.cspSource} data: blob:; style-src ${webview.cspSource} 'unsafe-inline'; ` +
    `script-src 'nonce-${n}' ${webview.cspSource}; frame-src http://localhost:* http://127.0.0.1:* ${extra.frameSrc ?? ''}; ` +
    `connect-src ws://127.0.0.1:* ws://localhost:* ${extra.connectSrc ?? ''};`;
  return html
    .replace(/__CSP__/g, csp).replace(/__NONCE__/g, n).replace(/__PROBE_JS__/g, probeJs)
    .replace(/__IFRAME_URL__/g, iframeUrl).replace(/__WS_URL__/g, wsUrl)
    .replace(/__INSTANCE__/g, String(++instanceSeq)).replace(/__TITLE__/g, extra.title ?? page)
    .replace(/__SURFACE__/g, extra.surface ?? 'pcb').replace(/__LIVE_WS__/g, extra.liveWs ?? cfg('liveWs', 'ws://127.0.0.1:8765/ws'));
}

function wireWebview(webview: vscode.Webview, label: string, disposables: vscode.Disposable[]): void {
  disposables.push(webview.onDidReceiveMessage((m: Record<string, unknown>) => {
    if (m && typeof m === 'object' && m.kind === 'batch' && Array.isArray(m.events)) {
      for (const ev of m.events as Record<string, unknown>[]) { log('wv', { src: label, ...ev }); }
    } else { log('wv', { src: label, ...(m as Record<string, unknown>) }); }
  }));
}

function makePanel(mode: Page, column: vscode.ViewColumn, surface = ''): vscode.WebviewPanel {
  const retain = cfg('retainContextWhenHidden', true);
  const title = mode === 'probe' ? 'KiCad input probe' : mode === 'live' ? `KiCad ${surface || 'pcb'} (live)` : 'KiCad view';
  const panel = vscode.window.createWebviewPanel(VIEW_TYPE, title, column, {
    enableScripts: true, retainContextWhenHidden: retain, enableFindWidget: false,
    localResourceRoots: [vscode.Uri.joinPath(extUri, 'media')],
    // portMapping lets the webview reach the extension host's local port as localhost:<same> even when the
    // extension host is remote (WSL / SSH / Codespaces). Locally it is a no-op but we declare it anyway.
    portMapping: httpPort ? [{ webviewPort: httpPort, extensionHostPort: httpPort }] : [],
  });
  panel.webview.html = pageHtml(panel.webview, mode, { url: cfg('url', ''), wsUrl: cfg('wsUrl', ''), surface: surface || 'pcb' });
  const disposables: vscode.Disposable[] = [];
  wireWebview(panel.webview, mode === 'live' ? 'live:' + surface : mode, disposables);
  if (mode === 'live') { livePanels.set(surface || 'pcb', panel.webview); panel.onDidDispose(() => { if (livePanels.get(surface || 'pcb') === panel.webview) { livePanels.delete(surface || 'pcb'); } }); }
  disposables.push(panel.onDidChangeViewState((e) => {
    log('panel', { mode, active: e.webviewPanel.active, visible: e.webviewPanel.visible, column: e.webviewPanel.viewColumn });
    if (e.webviewPanel.active) { activePanel = e.webviewPanel; }
  }));
  panel.onDidDispose(() => { disposables.forEach((d) => d.dispose()); if (activePanel === panel) { activePanel = undefined; } log('panel', { mode, disposed: true }); });
  activePanel = panel;
  log('panel', { mode, created: true, retainContextWhenHidden: retain, column });
  return panel;
}

// ---------------------------------------------------------------- custom editor: *.kicad_pcb / *.kicad_sch
class BoardDoc implements vscode.CustomDocument {
  constructor(public readonly uri: vscode.Uri) {}
  dispose(): void { /* nothing to release */ }
}

class BoardEditorProvider implements vscode.CustomReadonlyEditorProvider<BoardDoc> {
  openCustomDocument(uri: vscode.Uri): BoardDoc { return new BoardDoc(uri); }
  resolveCustomEditor(doc: BoardDoc, panel: vscode.WebviewPanel): void {
    const mode: Page = cfg('liveWs', '') ? 'live' : cfg('wsUrl', '') ? 'canvas' : 'view';
    const surface = surfaceForFile(doc.uri.fsPath);
    panel.webview.options = {
      enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(extUri, 'media')],
      portMapping: httpPort ? [{ webviewPort: httpPort, extensionHostPort: httpPort }] : [],
    };
    panel.webview.html = pageHtml(panel.webview, mode, { url: cfg('url', ''), wsUrl: cfg('wsUrl', ''), title: path.basename(doc.uri.fsPath), surface });
    const disposables: vscode.Disposable[] = [];
    wireWebview(panel.webview, 'editor:' + path.basename(doc.uri.fsPath), disposables);
    if (mode === 'live') { livePanels.set(surface, panel.webview); }
    // Backend contract sketch: tell the display backend which document this surface shows.
    panel.webview.postMessage({ type: 'open', file: doc.uri.fsPath });
    disposables.push(panel.onDidChangeViewState((e) => log('editor', { file: doc.uri.fsPath, active: e.webviewPanel.active, visible: e.webviewPanel.visible })));
    panel.onDidDispose(() => { disposables.forEach((d) => d.dispose()); if (livePanels.get(surface) === panel.webview) { livePanels.delete(surface); } });
    activePanel = panel;
    log('editor', { file: doc.uri.fsPath, resolved: true, mode, surface });
  }
}

// ---------------------------------------------------------------- control channel for the test driver
async function runControl(m: Record<string, unknown>): Promise<unknown> {
  switch (m.cmd) {
    case 'marker': return m.text;
    case 'executeCommand': return await vscode.commands.executeCommand(String(m.command), ...(Array.isArray(m.args) ? m.args : []));
    case 'post': activePanel?.webview.postMessage(m.msg); return activePanel ? 'posted' : 'no-panel';
    case 'clipboardWrite': await vscode.env.clipboard.writeText(String(m.text)); return 'ok';
    case 'clipboardRead': return await vscode.env.clipboard.readText();
    case 'openFile': await vscode.commands.executeCommand('vscode.open', vscode.Uri.file(String(m.path)), m.column ?? vscode.ViewColumn.Active); return 'ok';
    case 'openText': { const d = await vscode.workspace.openTextDocument(vscode.Uri.file(String(m.path))); await vscode.window.showTextDocument(d, { viewColumn: (m.column as vscode.ViewColumn) ?? vscode.ViewColumn.Beside, preserveFocus: !!m.preserveFocus }); return 'ok'; }
    case 'openPanel': { const p = makePanel((m.mode as Page) ?? 'probe', (m.column as vscode.ViewColumn) ?? vscode.ViewColumn.Active, String(m.surface ?? '')); return 'panel ' + p.title; }
    case 'postSurface': { const wv = livePanels.get(String(m.surface)); wv?.postMessage(m.msg); return wv ? 'posted ' + m.surface : 'no live panel ' + m.surface; }
    case 'livePanels': return [...livePanels.keys()];
    case 'reveal': activePanel?.reveal(undefined, !!m.preserveFocus); return 'ok';
    case 'state': return { activeEditor: vscode.window.activeTextEditor?.document.fileName, tabs: vscode.window.tabGroups.all.map((g) => ({ col: g.viewColumn, active: g.isActive, tabs: g.tabs.map((t) => ({ label: t.label, active: t.isActive })) })), zoom: vscode.workspace.getConfiguration('window').get('zoomLevel'), keyboardDispatch: vscode.workspace.getConfiguration('keyboard').get('dispatch') };
    case 'setConfig': await vscode.workspace.getConfiguration().update(String(m.key), m.value, vscode.ConfigurationTarget.Global); return 'ok';
    case 'quit': await vscode.commands.executeCommand('workbench.action.quit'); return 'ok';
    default: return 'unknown cmd';
  }
}

function pollControl(): void {
  if (!cmdFile) { return; }
  try {
    if (!fs.existsSync(cmdFile)) { return; }
    const size = fs.statSync(cmdFile).size;
    if (size <= cmdOffset) { return; }
    const fd = fs.openSync(cmdFile, 'r');
    const buf = Buffer.alloc(size - cmdOffset);
    fs.readSync(fd, buf, 0, buf.length, cmdOffset);
    fs.closeSync(fd);
    const text = buf.toString('utf8');
    const lastNl = text.lastIndexOf('\n');
    if (lastNl < 0) { return; }
    cmdOffset += Buffer.byteLength(text.slice(0, lastNl + 1));
    for (const line of text.slice(0, lastNl).split('\n')) {
      if (!line.trim()) { continue; }
      let m: Record<string, unknown>;
      try { m = JSON.parse(line); } catch { log('ctl', { error: 'bad json', line }); continue; }
      runControl(m).then((r) => log('ctl', { id: m.id, cmd: m.cmd, result: r }), (e) => log('ctl', { id: m.id, cmd: m.cmd, error: String(e) }));
    }
  } catch (e) { log('ctl', { error: String(e) }); }
}

// ---------------------------------------------------------------- activation
export async function activate(context: vscode.ExtensionContext): Promise<void> {
  extUri = context.extensionUri;
  out = vscode.window.createOutputChannel('kiwrap shell');
  logFile = cfg('logFile', '');
  if (logFile) { cmdFile = path.join(path.dirname(logFile), 'cmd.jsonl'); try { cmdOffset = fs.existsSync(cmdFile) ? fs.statSync(cmdFile).size : 0; } catch { cmdOffset = 0; } }
  httpPort = await startHttp(path.join(extUri.fsPath, 'media'));
  wsPort = await startWs();
  const rescue = cfg('rescueKeys', true);
  await vscode.commands.executeCommand('setContext', 'kiwrapShell.rescueKeys', rescue);
  log('activated', { pid: process.pid, vscode: vscode.version, httpPort, wsPort, logFile, rescueKeys: rescue, uiKind: vscode.env.uiKind, remote: vscode.env.remoteName ?? null, appHost: vscode.env.appHost });

  context.subscriptions.push(
    vscode.commands.registerCommand('kiwrapShell.openProbe', () => makePanel('probe', vscode.ViewColumn.Active)),
    vscode.commands.registerCommand('kiwrapShell.openView', () => makePanel('view', vscode.ViewColumn.Active)),
    vscode.commands.registerCommand('kiwrapShell.openCanvas', () => makePanel('canvas', vscode.ViewColumn.Active)),
    vscode.commands.registerCommand('kiwrapShell.openLive', (surface?: string) => makePanel('live', vscode.ViewColumn.Active, surface ?? '3d')),
    vscode.commands.registerCommand('kiwrapShell.marker', (text?: string) => log('marker', { text: text ?? '' })),
    vscode.commands.registerCommand('kiwrapShell.focusView', () => activePanel?.reveal(undefined, false)),
    // Keys that VS Code would otherwise consume while the view is active. The webview already received the
    // raw DOM event (VS Code cannot prevent that), so the command only needs to swallow the workbench action.
    // If a backend needs the chord re-sent (e.g. an <iframe> that never saw it), forward it via postMessage.
    vscode.commands.registerCommand('kiwrapShell.forwardKey', (args?: { key?: string }) => {
      log('rescued', { key: args?.key ?? '?', activePanel: activePanel?.title ?? null });
      activePanel?.webview.postMessage({ type: 'forwardedKey', key: args?.key ?? '?' });
    }),
    vscode.commands.registerCommand('kiwrapShell.probeCommand', (m: Record<string, unknown>) => runControl(m)),
    vscode.window.registerCustomEditorProvider(EDITOR_TYPE, new BoardEditorProvider(), {
      webviewOptions: { retainContextWhenHidden: cfg('retainContextWhenHidden', true) }, supportsMultipleEditorsPerDocument: false,
    }),
    vscode.window.onDidChangeWindowState((s) => log('window', { focused: s.focused, active: s.active })),
    vscode.window.onDidChangeActiveTextEditor((e) => log('activeTextEditor', { file: e?.document.fileName ?? null })),
    vscode.workspace.onDidChangeConfiguration(async (e) => {
      if (e.affectsConfiguration('kiwrapShell.rescueKeys')) { await vscode.commands.executeCommand('setContext', 'kiwrapShell.rescueKeys', cfg('rescueKeys', true)); log('config', { rescueKeys: cfg('rescueKeys', true) }); }
    }),
  );
  const timer = setInterval(pollControl, 150);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  const auto = cfg<string>('autoOpen', '');
  if (auto === 'probe' || auto === 'view' || auto === 'canvas') { makePanel(auto, vscode.ViewColumn.Active); }
}

export function deactivate(): void { log('deactivated'); }
