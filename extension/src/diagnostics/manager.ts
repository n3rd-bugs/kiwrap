/**
 * Problems panel: a DiagnosticCollection fed by kw_sch_check (ERC) and kw_pcb_drc (DRC).
 * ERC diagnostics land on the `.kicad_sch` files, DRC ones on the `.kicad_pcb`; each one sits
 * on the line of its first item's uuid (mapping.ts). Refreshed on demand (the commands) and on
 * save of a design file (kiwrap.checkOnSave), debounced because kicad-cli runs take seconds.
 * A run's own rewrite of the board (the DRC refill save) is recognised by its file stamp
 * (selfWrite.ts) and never counts as a change, and on-save DRC runs pass save_board: false.
 */
import * as fs from 'node:fs';
import * as vscode from 'vscode';
import { boardFile, projectForFile, schematicFiles, type KicadProject } from '../project';
import type { KiwrapSession, ValidatorSummary } from '../session';
import { KiwrapError, type PcbDrcData, type SchCheckData } from '../types';
import { countBySeverity, mapViolations, type MappedDiagnostic, type MappedSeverity } from './mapping';
import { SelfWriteGuard } from './selfWrite';

const SEVERITY: Record<MappedSeverity, vscode.DiagnosticSeverity> = {
  error: vscode.DiagnosticSeverity.Error,
  warning: vscode.DiagnosticSeverity.Warning,
  information: vscode.DiagnosticSeverity.Information,
  hint: vscode.DiagnosticSeverity.Hint,
};

function readTexts(files: string[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const f of files) {
    try {
      out.set(f, fs.readFileSync(f, 'utf8'));
    } catch {
      /* missing file: uuid search falls back to line 1 */
    }
  }
  return out;
}

function lineRange(text: string | undefined, line: number): vscode.Range {
  const lines = text ? text.split('\n') : [];
  const len = lines[line]?.replace(/\r$/, '').length ?? 0;
  const start = lines[line] ? lines[line].length - lines[line].trimStart().length : 0;
  return new vscode.Range(line, start, line, Math.max(len, start + 1));
}

export function toVscodeDiagnostics(
  mapped: MappedDiagnostic[],
  texts: Map<string, string>,
): Map<string, vscode.Diagnostic[]> {
  const byFile = new Map<string, vscode.Diagnostic[]>();
  for (const d of mapped) {
    const diag = new vscode.Diagnostic(lineRange(texts.get(d.file), d.line), d.message, SEVERITY[d.severity]);
    diag.code = d.code;
    diag.source = d.source;
    if (d.related.length) {
      diag.relatedInformation = d.related.map(
        (r) =>
          new vscode.DiagnosticRelatedInformation(
            new vscode.Location(vscode.Uri.file(r.file), lineRange(texts.get(r.file), r.line)),
            r.message,
          ),
      );
    }
    const list = byFile.get(d.file) ?? [];
    list.push(diag);
    byFile.set(d.file, list);
  }
  return byFile;
}

export class DiagnosticsManager implements vscode.Disposable {
  readonly erc: vscode.DiagnosticCollection;
  readonly drc: vscode.DiagnosticCollection;
  private readonly disposables: vscode.Disposable[] = [];
  private saveTimers = new Map<string, NodeJS.Timeout>();
  private running = new Map<string, Promise<void>>();
  /** Files our own runs wrote (a DRC refill save): their watcher events are not external changes. */
  private readonly selfWrites = new SelfWriteGuard();
  /** Silent (on-save) runs counted per key, for the e2e test and the output channel. */
  readonly autoRuns = { erc: 0, drc: 0 };

  constructor(private readonly session: KiwrapSession) {
    this.erc = vscode.languages.createDiagnosticCollection('kiwrap-erc');
    this.drc = vscode.languages.createDiagnosticCollection('kiwrap-drc');
    this.disposables.push(this.erc, this.drc);
    // refresh on save: VS Code text saves and external writes (the agent through kiwrap) alike
    const watcher = vscode.workspace.createFileSystemWatcher('**/*.{kicad_sch,kicad_pcb}');
    this.disposables.push(
      watcher,
      watcher.onDidChange((uri) => this.onFileChanged(uri.fsPath)),
      watcher.onDidCreate((uri) => this.onFileChanged(uri.fsPath)),
      vscode.workspace.onDidSaveTextDocument((doc) => this.onFileChanged(doc.uri.fsPath)),
    );
  }

  private onFileChanged(file: string): void {
    if (!vscode.workspace.getConfiguration('kiwrap').get<boolean>('checkOnSave', true)) {
      return;
    }
    const lower = file.toLowerCase();
    const isSch = lower.endsWith('.kicad_sch');
    const isPcb = lower.endsWith('.kicad_pcb');
    if (!isSch && !isPcb) {
      return;
    }
    const project = projectForFile(file);
    if (!project) {
      return;
    }
    // only the workspace's current project triggers automatic checks (one KiCad run at a time)
    const current = this.session.pickWorkspaceProject();
    if (!current || current.pro.toLowerCase() !== project.pro.toLowerCase()) {
      return;
    }
    // our own run rewrote the file (kw_pcb_drc refill + save): not a change to check again
    if (this.selfWrites.isSelfWrite(file)) {
      return;
    }
    const key = isSch ? 'erc' : 'drc';
    clearTimeout(this.saveTimers.get(key));
    this.saveTimers.set(
      key,
      setTimeout(() => {
        this.saveTimers.delete(key);
        if (this.selfWrites.isSelfWrite(file) || this.running.has(key)) {
          // the run that rewrote the file finished after the event (still our own write), or is
          // still running (its result covers the file as it is now)
          return;
        }
        this.autoRuns[key] += 1;
        this.session.log(`${key.toUpperCase()} on save: ${file} changed`);
        void (isSch ? this.runErc(project, { silent: true }) : this.runDrc(project, { silent: true }));
      }, 1500),
    );
  }

  private single(key: string, task: () => Promise<void>): Promise<void> {
    const inflight = this.running.get(key);
    if (inflight) {
      return inflight;
    }
    const p = task().finally(() => this.running.delete(key));
    this.running.set(key, p);
    return p;
  }

  /** kw_sch_check -> ERC diagnostics on the project's schematic files. */
  runErc(project: KicadProject, opts: { silent?: boolean } = {}): Promise<void> {
    return this.single('erc', async () => {
      this.session.setBusy('erc', true);
      try {
        const server = await this.session.ready();
        const receipt = await server.schCheck(project.pro);
        const data = receipt.data as SchCheckData;
        const files = schematicFiles(project);
        this.selfWrites.record(files);
        const texts = readTexts(files);
        const mapped = mapViolations(data.violations ?? [], { kind: 'erc', files, texts });
        this.erc.clear();
        for (const [file, diags] of toVscodeDiagnostics(mapped, texts)) {
          this.erc.set(vscode.Uri.file(file), diags);
        }
        this.session.lastErc = summary(receipt.provider_used, receipt.stage, data.kicad_version, data.ok, {
          errors: data.errors,
          warnings: data.warnings,
        });
        this.session.log(
          `ERC ${project.name}: ${data.ok ? 'ok' : 'FAIL'} errors=${data.errors} warnings=${data.warnings} ` +
            `(${receipt.provider_used}${receipt.degraded.length ? `, degraded: ${receipt.degraded.join(',')}` : ''})`,
        );
        for (const w of receipt.warnings) {
          this.session.log(`  warning: ${w}`);
        }
        if (!opts.silent) {
          const c = countBySeverity(mapped);
          void vscode.window.setStatusBarMessage(
            `kiwrap ERC: ${data.ok ? 'ok' : 'FAIL'} - ${c.errors} errors, ${c.warnings} warnings`,
            5000,
          );
        }
      } catch (err) {
        this.report('ERC', err, opts.silent);
      } finally {
        this.session.setBusy('erc', false);
        void this.session.refreshProject();
      }
    });
  }

  /** kw_pcb_drc -> DRC diagnostics on the board. */
  runDrc(project: KicadProject, opts: { silent?: boolean; parity?: boolean } = {}): Promise<void> {
    return this.single('drc', async () => {
      this.session.setBusy('drc', true);
      try {
        const server = await this.session.ready();
        // an automatic (on-save) check never rewrites the board: the refill stays in kicad-cli's
        // memory for the check (save_board: false); the explicit command keeps the server default
        const receipt = await server.pcbDrc(project.pro, {
          schematic_parity: opts.parity ?? false,
          save_board: opts.silent ? false : undefined,
        });
        const data = receipt.data as PcbDrcData;
        const files = [boardFile(project)];
        this.selfWrites.record(files);
        const texts = readTexts(files);
        const mapped = mapViolations(data.violations ?? [], { kind: 'drc', files, texts });
        this.drc.clear();
        for (const [file, diags] of toVscodeDiagnostics(mapped, texts)) {
          this.drc.set(vscode.Uri.file(file), diags);
        }
        this.session.lastDrc = summary(receipt.provider_used, receipt.stage, data.kicad_version, data.ok, {
          must_fix: data.must_fix,
          review: data.review,
          cosmetic: data.cosmetic,
          unconnected: data.unconnected,
        });
        this.session.log(
          `DRC ${project.name}: ${data.ok ? 'ok' : 'FAIL'} must_fix=${data.must_fix} review=${data.review} ` +
            `cosmetic=${data.cosmetic} unconnected=${data.unconnected} (${receipt.provider_used}` +
            `${receipt.degraded.length ? `, degraded: ${receipt.degraded.join(',')}` : ''})`,
        );
        for (const w of receipt.warnings) {
          this.session.log(`  warning: ${w}`);
        }
        if (!opts.silent) {
          void vscode.window.setStatusBarMessage(
            `kiwrap DRC: ${data.ok ? 'ok' : 'FAIL'} - must_fix ${data.must_fix}, review ${data.review}, cosmetic ${data.cosmetic}`,
            5000,
          );
        }
      } catch (err) {
        this.report('DRC', err, opts.silent);
      } finally {
        this.session.setBusy('drc', false);
        void this.session.refreshProject();
      }
    });
  }

  private report(what: string, err: unknown, silent?: boolean): void {
    const msg = err instanceof KiwrapError ? `${err.code}: ${err.message.replace(/^E_[A-Z_]+:\s*/, '')}` : String(err);
    const fix = err instanceof KiwrapError && err.fix ? ` Fix: ${err.fix}` : '';
    this.session.log(`${what} failed: ${msg}${fix}`);
    if (!silent) {
      void vscode.window.showErrorMessage(`kiwrap ${what} failed: ${msg}${fix}`, 'Show output').then((choice) => {
        if (choice) {
          this.session.output.show(true);
        }
      });
    }
  }

  counts(): { erc: ReturnType<typeof countBySeverity>; drc: ReturnType<typeof countBySeverity> } {
    const tally = (coll: vscode.DiagnosticCollection) => {
      const c = { errors: 0, warnings: 0, infos: 0 };
      coll.forEach((_uri, diags) => {
        for (const d of diags) {
          if (d.severity === vscode.DiagnosticSeverity.Error) {
            c.errors += 1;
          } else if (d.severity === vscode.DiagnosticSeverity.Warning) {
            c.warnings += 1;
          } else {
            c.infos += 1;
          }
        }
      });
      return c;
    };
    return { erc: tally(this.erc), drc: tally(this.drc) };
  }

  clear(): void {
    this.erc.clear();
    this.drc.clear();
  }

  dispose(): void {
    for (const t of this.saveTimers.values()) {
      clearTimeout(t);
    }
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}

function summary(
  provider: string,
  stage: string | null,
  kicadVersion: string | null,
  ok: boolean,
  numbers: Record<string, number>,
): ValidatorSummary {
  return { at: new Date(), ok, numbers, provider, stage, kicadVersion };
}
