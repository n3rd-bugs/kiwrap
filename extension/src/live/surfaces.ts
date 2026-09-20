/*
 * Surfaces model: the helper's list of captured KiCad frames, plus the mapping from a surface kind to
 * the VS Code tab that shows it (docs/DISPLAY.md 12.8 decision 12: custom editor per .kicad_pcb /
 * .kicad_sch, WebviewPanel for the 3D viewer and any other surface, the manager left alone by default).
 * No `vscode` import.
 */
import * as path from 'path';
import type { RectPx, Surface, SurfaceKind } from '../shared/types';

export type TabShape = 'editor' | 'panel';

/** Which tab shape a surface kind gets. */
export function tabShapeFor(kind: SurfaceKind): TabShape {
  return kind === 'pcb' || kind === 'sch' ? 'editor' : 'panel';
}

/** Surface kind a document extension maps to (null = not a live file). */
export function kindForFile(fsPath: string): SurfaceKind | null {
  const ext = path.extname(fsPath).toLowerCase();
  if (ext === '.kicad_pcb') { return 'pcb'; }
  if (ext === '.kicad_sch') { return 'sch'; }
  if (ext === '.kicad_pro') { return 'mgr'; }
  return null;
}

/** Editor kind (kw_live launch `editor` argument) for a file. */
export function editorForFile(fsPath: string): 'pcb' | 'sch' | 'project' {
  const k = kindForFile(fsPath);
  return k === 'pcb' ? 'pcb' : k === 'sch' ? 'sch' : 'project';
}

/**
 * Find the project directory for a KiCad file: the nearest ancestor (including its own dir) holding a
 * `.kicad_pro`; falls back to the file's directory. `exists` is injectable for tests.
 */
export function projectDirFor(fsPath: string, listDir: (dir: string) => string[]): string {
  let dir = path.dirname(fsPath);
  for (let i = 0; i < 6; i++) {
    let entries: string[] = [];
    try { entries = listDir(dir); } catch { entries = []; }
    if (entries.some((e) => e.toLowerCase().endsWith('.kicad_pro'))) { return dir; }
    const up = path.dirname(dir);
    if (up === dir) { break; }
    dir = up;
  }
  return path.dirname(fsPath);
}

export type LiveEditorKind = 'pcb' | 'sch' | 'project';

/**
 * The identity of a project for "the same project": its directory, resolved and lower-cased, so a
 * `.kicad_pro` path (kiwrap.openLive) and the directory (the tab's Start button) name one session
 * (reviewer finding 7: the raw strings differed and the running KiCad was killed and relaunched).
 */
export function projectKey(project: string): string {
  const p = path.resolve(project);
  const dir = /\.kicad_pro$/i.test(p) ? path.dirname(p) : p;
  return dir.replace(/[\\/]+$/, '').toLowerCase();
}

/** Surface kind an editor kind shows in its tab. */
export function surfaceKindFor(editor: LiveEditorKind): SurfaceKind {
  return editor === 'pcb' ? 'pcb' : editor === 'sch' ? 'sch' : 'mgr';
}

export const KIND_LABEL: Record<SurfaceKind, string> = {
  pcb: 'PCB editor', sch: 'Schematic editor', '3d': '3D viewer', mgr: 'Project manager', fp: 'Footprint editor', sym: 'Symbol editor', other: 'KiCad window',
};

export interface SurfacesListener { (surfaces: Surface[], change: { kind: 'reset' | 'create' | 'change' | 'destroy'; surface?: Surface; id?: string }): void }

export class SurfacesModel {
  private byId = new Map<string, Surface>();
  private listeners = new Set<SurfacesListener>();

  get all(): Surface[] { return [...this.byId.values()]; }
  get(id: string): Surface | undefined { return this.byId.get(id); }

  /** First surface of a kind; when a file is given prefers the one whose title mentions the file's stem. */
  find(kind: SurfaceKind, fsPath?: string): Surface | undefined {
    const ofKind = this.all.filter((s) => s.kind === kind);
    if (!ofKind.length) { return undefined; }
    if (fsPath) {
      const stem = path.basename(fsPath, path.extname(fsPath)).toLowerCase();
      const hit = ofKind.find((s) => s.title.toLowerCase().includes(stem));
      if (hit) { return hit; }
    }
    return ofKind[0];
  }

  reset(list: Surface[]): void {
    this.byId = new Map(list.map((s) => [s.id, s]));
    this.emit({ kind: 'reset' });
  }
  upsert(s: Surface): void {
    const existed = this.byId.has(s.id);
    this.byId.set(s.id, s);
    this.emit({ kind: existed ? 'change' : 'create', surface: s });
  }
  remove(id: string): void {
    if (this.byId.delete(id)) { this.emit({ kind: 'destroy', id }); }
  }
  clear(): void { if (this.byId.size) { this.byId.clear(); this.emit({ kind: 'reset' }); } }

  onChange(l: SurfacesListener): { dispose(): void } {
    this.listeners.add(l);
    return { dispose: () => { this.listeners.delete(l); } };
  }
  private emit(change: Parameters<SurfacesListener>[1]): void {
    const all = this.all;
    for (const l of this.listeners) { try { l(all, change); } catch { /* listener errors never break the model */ } }
  }
}

/** `[l, t, r, b]` (kiwrap-live wire form) or `{x, y, w, h}` -> RectPx. */
export function rectFromWire(r: unknown): RectPx {
  if (Array.isArray(r) && r.length === 4) { const [l, t, rr, b] = r.map((v) => Number(v) || 0); return { x: l, y: t, w: rr - l, h: b - t }; }
  const o = (r ?? {}) as Record<string, unknown>;
  return { x: Number(o.x) || 0, y: Number(o.y) || 0, w: Number(o.w) || 0, h: Number(o.h) || 0 };
}

/** Sanity-check a surface object from the wire; returns null when it is unusable. */
export function normalizeSurface(raw: unknown): Surface | null {
  if (!raw || typeof raw !== 'object') { return null; }
  const o = raw as Record<string, unknown>;
  const id = o.id != null ? String(o.id) : '';
  if (!id) { return null; }
  const kind = (['pcb', 'sch', '3d', 'mgr', 'fp', 'sym', 'other'] as SurfaceKind[]).includes(o.kind as SurfaceKind) ? (o.kind as SurfaceKind) : 'other';
  const dialogs = Array.isArray(o.dialogs) ? (o.dialogs as Array<Record<string, unknown>>).map((d) => ({ hwnd: Number(d.hwnd) || 0, title: typeof d.title === 'string' ? d.title : '', rect_px: rectFromWire(d.rect_px) })) : [];
  const s: Surface = {
    id, kind, title: typeof o.title === 'string' ? o.title : '', hwnd: Number(o.hwnd) || 0,
    rect_px: rectFromWire(o.rect_px), dpi: Number(o.dpi) || 96, cloaked: !!o.cloaked, dialogs,
  };
  if (o.frame_px !== undefined) { s.frame_px = rectFromWire(o.frame_px); }
  if (typeof o.foreground === 'boolean') { s.foreground = o.foreground; }
  if (typeof o.enabled === 'boolean') { s.enabled = o.enabled; }
  return s;
}
