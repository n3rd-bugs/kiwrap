/*
 * Custom editor `kiwrap.live` for *.kicad_pcb (pcb surface) and *.kicad_sch (sch surface).
 * Read-only from VS Code's point of view: KiCad owns the file while a session is live
 * (ARCHITECTURE section 9 "warn when a GUI holds a file"). Priority "option" in contributes so a plain
 * open still gives the text editor; "Reopen Editor With..." / kiwrap.live.open switch to this one.
 */
import * as fs from 'fs';
import * as vscode from 'vscode';
import type { LiveSessionManager } from './liveSession';
import { LiveView } from './webviewHost';
import { editorForFile, kindForFile, projectDirFor } from './surfaces';
import { LIVE_COMMANDS, LIVE_EDITOR_VIEW_TYPE } from '../shared/types';

class LiveDocument implements vscode.CustomDocument {
  constructor(readonly uri: vscode.Uri) {}
  dispose(): void { /* nothing held */ }
}

export class LiveEditorProvider implements vscode.CustomReadonlyEditorProvider<LiveDocument> {
  readonly views = new Set<LiveView>();
  constructor(private readonly sessions: LiveSessionManager, private readonly mediaRoot: vscode.Uri, private readonly out: vscode.OutputChannel) {}

  static register(sessions: LiveSessionManager, mediaRoot: vscode.Uri, out: vscode.OutputChannel, retain: boolean): { provider: LiveEditorProvider; disposable: vscode.Disposable } {
    const provider = new LiveEditorProvider(sessions, mediaRoot, out);
    const disposable = vscode.window.registerCustomEditorProvider(LIVE_EDITOR_VIEW_TYPE, provider, {
      webviewOptions: { retainContextWhenHidden: retain },
      supportsMultipleEditorsPerDocument: false,
    });
    return { provider, disposable };
  }

  openCustomDocument(uri: vscode.Uri): LiveDocument { return new LiveDocument(uri); }

  resolveCustomEditor(doc: LiveDocument, panel: vscode.WebviewPanel): void {
    const kind = kindForFile(doc.uri.fsPath) ?? 'pcb';
    const view = new LiveView(panel, { kind, file: doc.uri.fsPath }, this.sessions, this.mediaRoot, this.out, {
      onStart: () => {
        const project = projectDirFor(doc.uri.fsPath, (d) => fs.readdirSync(d));
        void vscode.commands.executeCommand(LIVE_COMMANDS.open, { project, editor: editorForFile(doc.uri.fsPath), file: doc.uri.fsPath });
      },
      onDispose: (v) => { this.views.delete(v); },
    }, true);
    this.views.add(view);
    this.out.appendLine(`[live] editor resolved ${doc.uri.fsPath} -> ${kind}`);
  }

  /** The view currently showing a file (if any). */
  viewFor(fsPath: string): LiveView | undefined {
    const p = fsPath.toLowerCase();
    for (const v of this.views) { if ('file' in v.selector && v.selector.file?.toLowerCase() === p) { return v; } }
    return undefined;
  }
}
