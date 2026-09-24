/**
 * `kiwrap.preview`: a read-only custom editor (priority "option") for *.kicad_sch / *.kicad_pcb
 * that shows the kw_sch_render / kw_pcb_render PNG (or the SVG fallback when the server has no
 * rasteriser) with zoom/pan, re-renders when the file changes on disk, and hands "Open Live
 * KiCad" to the live component (`kiwrap.live.open` when registered, else `kiwrap.openLive`).
 */
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { isSchematic, projectForFile, type KicadProject } from '../project';
import type { KiwrapSession } from '../session';
import { KiwrapError, type Receipt, type RenderData } from '../types';
import { previewHtml } from './previewHtml';

export const PREVIEW_VIEW_TYPE = 'kiwrap.preview';

class PreviewDocument implements vscode.CustomDocument {
  constructor(readonly uri: vscode.Uri) {}
  dispose(): void {
    /* nothing to release */
  }
}

interface RenderOptions {
  view: '3d' | '2d';
  side: 'top' | 'bottom';
  page: number;
}

/** What a finished render looked like (test hook / status line): the kw_*_render info. */
export interface PreviewRender {
  file: string;
  info: string;
  at: Date;
  error?: string;
}

export class PreviewProvider implements vscode.CustomReadonlyEditorProvider<PreviewDocument> {
  /** The one provider of this extension host (set by `register`); `renders` is the test hook. */
  static current: PreviewProvider | undefined;
  readonly renders = new Map<string, PreviewRender>();
  private readonly rendered = new vscode.EventEmitter<PreviewRender>();
  readonly onDidRender = this.rendered.event;

  static register(context: vscode.ExtensionContext, session: KiwrapSession): vscode.Disposable {
    const provider = new PreviewProvider(session);
    PreviewProvider.current = provider;
    return vscode.window.registerCustomEditorProvider(PREVIEW_VIEW_TYPE, provider, {
      webviewOptions: { retainContextWhenHidden: true },
      supportsMultipleEditorsPerDocument: true,
    });
  }

  constructor(private readonly session: KiwrapSession) {}

  private recordRender(r: PreviewRender): void {
    this.renders.set(r.file.toLowerCase(), r);
    this.rendered.fire(r);
  }

  openCustomDocument(uri: vscode.Uri): PreviewDocument {
    return new PreviewDocument(uri);
  }

  resolveCustomEditor(doc: PreviewDocument, panel: vscode.WebviewPanel): void {
    const file = doc.uri.fsPath;
    const kind: 'sch' | 'pcb' = isSchematic(file) ? 'sch' : 'pcb';
    const nonce = crypto.randomBytes(16).toString('hex');
    panel.webview.options = { enableScripts: true, localResourceRoots: [] };
    panel.webview.html = previewHtml(panel.webview, { kind, title: path.basename(file), nonce });
    const options: RenderOptions = { view: '3d', side: 'top', page: 1 };
    const disposables: vscode.Disposable[] = [];
    let generation = 0;
    let timer: NodeJS.Timeout | undefined;
    let disposed = false;

    const post = (m: Record<string, unknown>) => {
      if (!disposed) {
        void panel.webview.postMessage(m).then(undefined, () => undefined);
      }
    };
    const render = async (keepView: boolean) => {
      const gen = ++generation;
      post({ type: 'status', text: 'rendering...' });
      try {
        const { dataUri, info } = await this.render(file, kind, options);
        if (gen === generation) {
          post({ type: 'image', dataUri, info, keepView });
          this.recordRender({ file, info, at: new Date() });
        }
      } catch (err) {
        const text = err instanceof KiwrapError ? `${err.message}${err.fix ? `\n\nfix: ${err.fix}` : ''}` : String(err);
        this.session.log(`preview ${path.basename(file)} failed: ${text.split('\n')[0]}`);
        if (gen === generation) {
          post({ type: 'error', text });
          this.recordRender({ file, info: '', at: new Date(), error: text });
        }
      }
    };
    const scheduleRender = () => {
      clearTimeout(timer);
      timer = setTimeout(() => void render(true), 800);
    };

    disposables.push(
      panel.webview.onDidReceiveMessage((m: { type: string; key?: string; value?: string }) => {
        if (m.type === 'ready' || m.type === 'refresh') {
          void render(m.type === 'refresh');
        } else if (m.type === 'option' && m.key) {
          if (m.key === 'view' && (m.value === '3d' || m.value === '2d')) {
            options.view = m.value;
          } else if (m.key === 'side' && (m.value === 'top' || m.value === 'bottom')) {
            options.side = m.value;
          } else if (m.key === 'page') {
            options.page = Math.max(1, Number(m.value) || 1);
          }
          void render(false);
        } else if (m.type === 'live') {
          void vscode.commands.executeCommand('kiwrap.openLive', doc.uri);
        }
      }),
    );
    // re-render when the file changes on disk (the agent writes through kiwrap; VS Code saves)
    const watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(path.dirname(file)), path.basename(file)),
    );
    disposables.push(watcher, watcher.onDidChange(scheduleRender), watcher.onDidCreate(scheduleRender));
    panel.onDidDispose(() => {
      disposed = true;
      generation += 1; // drop the result of a render still running
      clearTimeout(timer);
      for (const d of disposables) {
        d.dispose();
      }
    });
  }

  private async render(
    file: string,
    kind: 'sch' | 'pcb',
    options: RenderOptions,
  ): Promise<{ dataUri: string; info: string }> {
    const project = projectForFile(file);
    if (!project) {
      throw new KiwrapError({
        code: 'E_PROJECT_NOT_FOUND',
        message: `no .kicad_pro next to ${path.basename(file)}`,
        fix: 'kiwrap renders through the project; put the file in a KiCad project directory',
      });
    }
    const server = await this.session.ready();
    let receipt: Receipt<RenderData>;
    if (kind === 'sch') {
      const sheet = this.sheetName(project, file);
      receipt = await server.schRender(project.pro, { sheet, page: options.page, max_px: 1024 }, { interactive: false });
    } else {
      if (path.basename(file).toLowerCase() !== `${project.name.toLowerCase()}.kicad_pcb`) {
        throw new KiwrapError({
          code: 'E_PROJECT_NOT_FOUND',
          message: `${path.basename(file)} is not the board of project ${project.name}`,
          fix: 'kw_pcb_render renders the project board; open the .kicad_pcb that shares the .kicad_pro stem',
        });
      }
      receipt = await server.pcbRender(project.pro, { view: options.view, side: options.side, max_px: 1024 }, { interactive: false });
    }
    const d = receipt.data;
    const degraded = receipt.degraded.length ? ` degraded: ${receipt.degraded.join(', ')}` : '';
    if (receipt.imageBase64) {
      const info = `${d.mode} ${d.width ?? '?'}x${d.height ?? '?'} ${fmtBytes(d.bytes ?? 0)} via ${receipt.provider_used}${degraded}`;
      return { dataUri: `data:image/png;base64,${receipt.imageBase64}`, info };
    }
    if (d.mode === 'svg' && d.files?.length) {
      const svg = d.files.find((f) => f.toLowerCase().endsWith('.svg')) ?? d.files[0];
      const text = fs.readFileSync(svg, 'utf8');
      const info = `svg (no rasteriser) via ${receipt.provider_used}${degraded}`;
      return { dataUri: `data:image/svg+xml;base64,${Buffer.from(text, 'utf8').toString('base64')}`, info };
    }
    if (d.png) {
      const buf = fs.readFileSync(d.png);
      return { dataUri: `data:image/png;base64,${buf.toString('base64')}`, info: `${d.mode} via ${receipt.provider_used}${degraded}` };
    }
    throw new KiwrapError({
      code: 'E_UNSUPPORTED',
      message: `the server returned no image (mode ${d.mode})${d.pdf ? `; PDF at ${d.pdf}` : ''}`,
      fix: 'install the kiwrap `render` extra (uv sync --all-extras) so PDFs can be rasterised',
    });
  }

  private sheetName(project: KicadProject, file: string): string | undefined {
    const base = path.basename(file);
    return base.toLowerCase() === `${project.name.toLowerCase()}.kicad_sch` ? undefined : base;
  }
}

function fmtBytes(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(0)} KB` : `${n} B`;
}
