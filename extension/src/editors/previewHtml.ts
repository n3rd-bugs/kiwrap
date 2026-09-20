/**
 * The preview webview page: an <img> inside a pannable/zoomable viewport, a toolbar (refresh,
 * fit, zoom, board side / view, sheet page, "Open Live KiCad") and a status line. The extension
 * posts `{type:'image', dataUri, info}` / `{type:'status', text}` / `{type:'error', text}`;
 * the page posts `{type:'refresh'|'live'|'option', ...}` back.
 */
import type * as vscode from 'vscode';

export interface PreviewPageOptions {
  kind: 'sch' | 'pcb';
  title: string;
  nonce: string;
}

export function previewHtml(webview: vscode.Webview, opts: PreviewPageOptions): string {
  const csp = `default-src 'none'; img-src ${webview.cspSource} data:; style-src 'nonce-${opts.nonce}'; script-src 'nonce-${opts.nonce}';`;
  const pcbControls =
    opts.kind === 'pcb'
      ? `<select id="view" title="view"><option value="3d">3D</option><option value="2d">2D layers</option></select>
         <select id="side" title="side"><option value="top">top</option><option value="bottom">bottom</option></select>`
      : `<label class="pg">page <input id="page" type="number" min="1" value="1" title="sheet page"></label>`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(opts.title)}</title>
<style nonce="${opts.nonce}">
  :root { color-scheme: light dark; }
  html, body { height: 100%; margin: 0; overflow: hidden; background: var(--vscode-editor-background); color: var(--vscode-foreground); font: 12px var(--vscode-font-family); }
  #bar { display: flex; gap: 6px; align-items: center; padding: 4px 8px; border-bottom: 1px solid var(--vscode-panel-border); background: var(--vscode-editorGroupHeader-tabsBackground); }
  #bar button, #bar select, #bar input { font: inherit; color: var(--vscode-button-foreground); background: var(--vscode-button-background); border: 1px solid transparent; border-radius: 2px; padding: 2px 8px; cursor: pointer; }
  #bar select, #bar input { color: var(--vscode-input-foreground); background: var(--vscode-input-background); border-color: var(--vscode-input-border, transparent); }
  #bar input { width: 3em; }
  #bar .sp { flex: 1; }
  #bar .live { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  #status { opacity: .8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45%; }
  #vp { position: absolute; top: 31px; left: 0; right: 0; bottom: 0; overflow: hidden; cursor: grab; background: repeating-conic-gradient(var(--vscode-editorWidget-background) 0 25%, transparent 0 50%) 0 0 / 20px 20px; }
  #vp.drag { cursor: grabbing; }
  #img { position: absolute; left: 0; top: 0; transform-origin: 0 0; image-rendering: auto; user-select: none; -webkit-user-drag: none; }
  #msg { position: absolute; inset: 31px 0 0 0; display: flex; align-items: center; justify-content: center; text-align: center; padding: 24px; white-space: pre-wrap; }
  #msg.err { color: var(--vscode-errorForeground); }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div id="bar">
  <button id="refresh" title="Re-render (kw_${opts.kind}_render)">$ Refresh</button>
  <button id="fit" title="Fit to view">Fit</button>
  <button id="zin" title="Zoom in">+</button>
  <button id="zout" title="Zoom out">-</button>
  ${pcbControls}
  <span class="sp"></span>
  <span id="status">rendering...</span>
  <button id="live" class="live" title="Open this file in a sandboxed live KiCad (kiwrap.live.open)">Open Live KiCad</button>
</div>
<div id="vp"><img id="img" class="hidden" alt="preview"></div>
<div id="msg">Rendering with kicad-cli...</div>
<script nonce="${opts.nonce}">
(function () {
  const vscode = acquireVsCodeApi();
  const vp = document.getElementById('vp');
  const img = document.getElementById('img');
  const msg = document.getElementById('msg');
  const status = document.getElementById('status');
  let scale = 1, tx = 0, ty = 0, natW = 0, natH = 0;
  let dragging = null;
  document.getElementById('refresh').textContent = '\\u21bb Refresh';

  function apply() { img.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')'; }
  function fit() {
    if (!natW || !natH) return;
    const w = vp.clientWidth, h = vp.clientHeight;
    scale = Math.min(w / natW, h / natH) * 0.98;
    tx = (w - natW * scale) / 2; ty = (h - natH * scale) / 2;
    apply();
  }
  function zoomAt(factor, cx, cy) {
    const ns = Math.min(Math.max(scale * factor, 0.05), 40);
    tx = cx - (cx - tx) * (ns / scale); ty = cy - (cy - ty) * (ns / scale);
    scale = ns; apply();
  }
  vp.addEventListener('wheel', (e) => { e.preventDefault(); zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.offsetX, e.offsetY); }, { passive: false });
  vp.addEventListener('pointerdown', (e) => { dragging = { x: e.clientX - tx, y: e.clientY - ty }; vp.classList.add('drag'); vp.setPointerCapture(e.pointerId); });
  vp.addEventListener('pointermove', (e) => { if (!dragging) return; tx = e.clientX - dragging.x; ty = e.clientY - dragging.y; apply(); });
  vp.addEventListener('pointerup', () => { dragging = null; vp.classList.remove('drag'); });
  vp.addEventListener('dblclick', fit);
  window.addEventListener('resize', fit);
  document.getElementById('fit').addEventListener('click', fit);
  document.getElementById('zin').addEventListener('click', () => zoomAt(1.25, vp.clientWidth / 2, vp.clientHeight / 2));
  document.getElementById('zout').addEventListener('click', () => zoomAt(0.8, vp.clientWidth / 2, vp.clientHeight / 2));
  document.getElementById('refresh').addEventListener('click', () => vscode.postMessage({ type: 'refresh' }));
  document.getElementById('live').addEventListener('click', () => vscode.postMessage({ type: 'live' }));
  for (const id of ['view', 'side', 'page']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => vscode.postMessage({ type: 'option', key: id, value: el.value }));
  }
  window.addEventListener('message', (ev) => {
    const m = ev.data;
    if (m.type === 'status') { status.textContent = m.text; }
    if (m.type === 'error') { status.textContent = 'error'; msg.textContent = m.text; msg.className = 'err'; msg.classList.remove('hidden'); }
    if (m.type === 'image') {
      msg.classList.add('hidden');
      img.onload = () => { natW = img.naturalWidth; natH = img.naturalHeight; img.classList.remove('hidden'); if (m.keepView && scale) { apply(); } else { fit(); } };
      img.src = m.dataUri;
      status.textContent = m.info || '';
    }
  });
  vscode.postMessage({ type: 'ready' });
})();
</script>
</body>
</html>`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string);
}
