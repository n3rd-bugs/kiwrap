// kiwrap prototype: KiCad window inside a VS Code webview via noVNC.
//
// The extension does NOT start KiCad or the VNC stack; the spike scripts do (see README). It only hosts the
// noVNC page in a webview (iframe) and, optionally, registers a read-only custom editor for *.kicad_pcb so
// that opening a board file shows the live KiCad window in the editor area.
//
// Findings this file encodes (docs/research/display/vnc-novnc.md):
//  - keys typed into the cross-origin iframe go to KiCad through noVNC, EXCEPT the chords VS Code/Electron own at
//    window level: F1, Ctrl+P, Ctrl+Shift+P and Alt were measured to open VS Code UI instead (see README).
//  - portMapping is a no-op locally and only rewrites http fetches to localhost in remote sessions; the RFB
//    WebSocket does not go through it, so a remote/WSL setup needs a forwarded port instead.
import * as vscode from "vscode";

const VIEW_TYPE = "kiwrapVnc.pcbEditor";

function pageUrl(): string {
  const cfg = vscode.workspace.getConfiguration("kiwrapVnc");
  return cfg.get<string>("url", "http://127.0.0.1:6080/kicad.html?nostatus=1&fit=dpr");
}

function port(): number {
  return vscode.workspace.getConfiguration("kiwrapVnc").get<number>("port", 6080);
}

function html(url: string): string {
  const origin = new URL(url).origin;
  // CSP: only the noVNC origin may be framed; no scripts run in the webview document itself.
  return `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${origin}; style-src 'unsafe-inline';">
<style>html,body{margin:0;padding:0;height:100%;overflow:hidden;background:#1e1e1e}iframe{border:0;width:100%;height:100%;display:block}</style>
</head><body>
<iframe src="${url}" allow="clipboard-read; clipboard-write" tabindex="0"></iframe>
</body></html>`;
}

function fillPanel(panel: vscode.WebviewPanel): void {
  panel.webview.options = {
    enableScripts: true,
    portMapping: [{ webviewPort: port(), extensionHostPort: port() }],
  };
  panel.webview.html = html(pageUrl());
}

export function activate(context: vscode.ExtensionContext): void {
  const open = (): vscode.WebviewPanel => {
    const panel = vscode.window.createWebviewPanel("kiwrapVnc.panel", "KiCad (noVNC)", vscode.ViewColumn.One, {
      enableScripts: true,
      retainContextWhenHidden: true,
      portMapping: [{ webviewPort: port(), extensionHostPort: port() }],
    });
    panel.webview.html = html(pageUrl());
    return panel;
  };

  context.subscriptions.push(vscode.commands.registerCommand("kiwrapVnc.open", () => open()));

  // *.kicad_pcb -> the same page (read-only custom editor; KiCad itself owns the file).
  const provider: vscode.CustomReadonlyEditorProvider = {
    openCustomDocument(uri: vscode.Uri): vscode.CustomDocument {
      return { uri, dispose(): void { /* nothing to release */ } };
    },
    resolveCustomEditor(_doc: vscode.CustomDocument, panel: vscode.WebviewPanel): void {
      panel.title = "KiCad (noVNC)";
      fillPanel(panel);
    },
  };
  context.subscriptions.push(
    vscode.window.registerCustomEditorProvider(VIEW_TYPE, provider, {
      webviewOptions: { retainContextWhenHidden: true },
      supportsMultipleEditorsPerDocument: false,
    }),
  );

  // Spike convenience: open the panel on start when the dev host was launched with KIWRAP_VNC_AUTO_OPEN=1.
  if (process.env.KIWRAP_VNC_AUTO_OPEN === "1") {
    open();
  }
}

export function deactivate(): void {
  // nothing to do
}
