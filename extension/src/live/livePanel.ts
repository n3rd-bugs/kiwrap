/*
 * WebviewPanels (`kiwrap.live.panel`) for surfaces that are not files: the 3D viewer, the footprint /
 * symbol editors, the project manager and anything `other`. One panel per surface id; the 3D viewer
 * may auto-open (setting kiwrap.live.autoOpen3d).
 */
import * as vscode from 'vscode';
import type { LiveSessionManager } from './liveSession';
import { LiveView } from './webviewHost';
import { KIND_LABEL } from './surfaces';
import { LIVE_PANEL_VIEW_TYPE, LIVE_SETTINGS_SECTION, type Surface } from '../shared/types';

export class LivePanels implements vscode.Disposable {
  private readonly byId = new Map<string, LiveView>();
  private readonly disposables: vscode.Disposable[] = [];

  constructor(private readonly sessions: LiveSessionManager, private readonly mediaRoot: vscode.Uri, private readonly out: vscode.OutputChannel) {
    this.disposables.push(sessions.surfaces.onChange((all, change) => {
      const auto = vscode.workspace.getConfiguration(LIVE_SETTINGS_SECTION).get<boolean>('autoOpen3d', false);
      if (!auto) { return; }
      const candidates = change.kind === 'create' && change.surface ? [change.surface] : change.kind === 'reset' ? all : [];
      for (const s of candidates) { if (s.kind === '3d' && !this.byId.has(s.id)) { this.open(s, vscode.ViewColumn.Beside, true); } }
    }));
  }

  get views(): LiveView[] { return [...this.byId.values()]; }

  open(surface: Surface, column: vscode.ViewColumn = vscode.ViewColumn.Active, preserveFocus = false): LiveView {
    const existing = this.byId.get(surface.id);
    if (existing) { existing.panel.reveal(column, preserveFocus); return existing; }
    const retain = vscode.workspace.getConfiguration(LIVE_SETTINGS_SECTION).get<boolean>('retainContextWhenHidden', true);
    const panel = vscode.window.createWebviewPanel(LIVE_PANEL_VIEW_TYPE, surface.title || KIND_LABEL[surface.kind], { viewColumn: column, preserveFocus }, {
      enableScripts: true, retainContextWhenHidden: retain, enableFindWidget: false, localResourceRoots: [this.mediaRoot],
    });
    const view = new LiveView(panel, { id: surface.id }, this.sessions, this.mediaRoot, this.out, { onDispose: (v) => { if (this.byId.get(surface.id) === v) { this.byId.delete(surface.id); } } });
    this.byId.set(surface.id, view);
    return view;
  }

  /** Quick pick over the surfaces that are not shown by a custom editor. */
  async pickAndOpen(): Promise<LiveView | undefined> {
    const all = this.sessions.surfaces.all;
    if (!all.length) { void vscode.window.showInformationMessage('Live KiCad: no KiCad windows are captured yet.'); return undefined; }
    const items = all.map((s) => ({ label: s.title || KIND_LABEL[s.kind], description: `${s.kind}  ${s.rect_px.w}x${s.rect_px.h}${s.cloaked ? '  cloaked' : ''}`, surface: s }));
    const pick = await vscode.window.showQuickPick(items, { placeHolder: 'KiCad window to show in a tab' });
    return pick ? this.open(pick.surface) : undefined;
  }

  dispose(): void {
    for (const d of this.disposables.splice(0)) { d.dispose(); }
    for (const v of this.byId.values()) { v.panel.dispose(); }
    this.byId.clear();
  }
}
