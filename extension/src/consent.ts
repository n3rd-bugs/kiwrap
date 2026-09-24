/**
 * The user's consent before kiwrap writes a project in a newer KiCad file format (D21, review
 * item 1 / B1: four default paths re-stamped a KiCad 10.0 project as 10.99, which 10.0 then refused
 * to open). The server never up-converts on its own: an operation that would save the design files
 * in a newer format fails with `E_FORMAT_UPGRADE_REQUIRED`, and this module turns that into a modal:
 *
 *   "<project> is in KiCad <x> format and no installed KiCad <x> can write it. Upgrade the project
 *    files to KiCad <y> format? KiCad <x> will no longer open them."
 *    [Upgrade]  [Install KiCad <x>...]  [Cancel]
 *
 * When an installed KiCad does write the files' format (the error's `candidates`: the server's
 * stamp guard rolled back a write that ran on a newer install), the second button is "Use KiCad
 * <v>" instead: `kw_project(action='retarget')` pins it (files untouched) and the call is retried.
 *
 * Upgrade calls `kw_project(action='upgrade', confirm=true)` (the only sanctioned up-conversion:
 * converted on a copy, pre-upgrade snapshot, ERC/DRC before and after) and retries the original call
 * once. Install puts the error's `install_hint` (`winget install KiCad.KiCad --version 10.0.6`) in
 * a terminal, or opens the KiCad download page. `E_FORMAT_NEWER` (files newer than every install)
 * becomes an error naming the KiCad that is needed, `E_READONLY` (D23) an information message.
 *
 * It plugs into `McpClient` as `onToolError`, so every call the extension makes gets it: ERC / DRC,
 * the live launch (`kw_live`, also through the live module's injected client) and
 * `kiwrap.core.callTool`. Background calls (`interactive: false`: on-save checks, previews, the
 * status refresh) never open a modal. Errors the user has seen are marked `handled`, so the caller
 * does not show a second popup. No `vscode` import: the window API is the injected `ConsentUi`
 * (consentUi.ts binds it; the unit tests stub it).
 */
import * as path from 'node:path';
import type { CallOptions, ToolErrorContext, ToolErrorHandler } from './mcpClient';
import { installFormats, matchingInstalls } from './status/installModel';
import type { InstallRow, KiwrapError, ProjectStatusData, Receipt } from './types';

export const E_FORMAT_UPGRADE_REQUIRED = 'E_FORMAT_UPGRADE_REQUIRED';
export const E_FORMAT_NEWER = 'E_FORMAT_NEWER';
export const E_READONLY = 'E_READONLY';

/** Where the KiCad installers are when the error carries no install hint. */
export const KICAD_DOWNLOAD_URL = 'https://www.kicad.org/download/';

export interface FormatFile {
  path: string;
  kind: string;
  stamp: number | null;
}

export interface FormatCandidate {
  version: string;
  formats: Record<string, number | null>;
}

/** The data of an `E_FORMAT_UPGRADE_REQUIRED` / `E_FORMAT_NEWER` error (contract (b)), normalised. */
export interface FormatErrorInfo {
  code: string;
  /** The project to upgrade: the error's `data.project`, else the call's `project` argument. */
  project: string | null;
  projectName: string;
  files: FormatFile[];
  /** `{kind: epoch}` of the project's files. */
  projectEpoch: Record<string, number>;
  /** The installed KiCads the server considered, with the formats each writes. */
  candidates: FormatCandidate[];
  /** The install the upgrade converts to (`data.upgrade_to`). */
  upgradeTo: string | null;
  /** A command that installs the matching release (`winget install KiCad.KiCad --version 10.0.6`). */
  installHint: string | null;
  /** The release line the files belong to ("10.0"): `data.project_kicad` / `format_kicad` / `needed_kicad`, else the hint's version. */
  formatKicad: string | null;
  /** The exact release the hint installs ("10.0.6"). */
  installVersion: string | null;
  /**
   * An installed KiCad that does write the files' format: the error's `candidates` (the server's
   * stamp guard rolled back a write that ran on a newer install although a matching one exists),
   * or the local match of the proactive "Upgrade project file format" command. The modal then
   * offers "Use KiCad <writer>" (kw_project retarget, files untouched) instead of an install.
   */
  writer?: string | null;
  /** E_FORMAT_NEWER: the KiCad that refused the files (`data.kicad`). */
  kicad: string | null;
  /** E_FORMAT_NEWER: installed KiCads that do read the files (the error's `candidates`). */
  readers: string[];
}

export interface ConsentUi {
  /** A modal warning; resolves to the chosen button, or undefined when closed / cancelled. */
  modal(message: string, detail: string, buttons: string[]): Promise<string | undefined>;
  /** Non-modal messages: the handler never awaits them (they resolve when dismissed). */
  error(message: string, buttons?: string[]): Promise<string | undefined>;
  info(message: string, buttons?: string[]): Promise<string | undefined>;
  /** Show the install command in a terminal (not run: the user presses Enter), else open the download page. */
  install(hint: string | null, version: string | null): Promise<void>;
  log(line: string): void;
}

export const UPGRADE_BUTTON = 'Upgrade';
export const CANCEL_BUTTON = 'Cancel';

/** The button that pins the project (kw_project retarget) to an installed KiCad that reads (E_FORMAT_NEWER) or writes (E_FORMAT_UPGRADE_REQUIRED) its files. */
export function useButton(version: string): string {
  return `Use KiCad ${version}`;
}

export function installButton(info: Pick<FormatErrorInfo, 'formatKicad'>): string {
  return info.formatKicad ? `Install KiCad ${info.formatKicad}\u2026` : 'Install KiCad\u2026';
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && /^\d+$/.test(v) ? Number(v) : null;
}

/** "10.0.6" -> "10.0"; anything else unchanged. */
export function majorMinor(version: string): string {
  const m = /^(\d+)\.(\d+)(?:\.\d+)?(?:[-+].*)?$/.exec(version.trim());
  return m ? `${m[1]}.${m[2]}` : version.trim();
}

/** The version a `winget install ... --version X` / `choco ... --version X` / `KiCad X` hint names. */
export function hintVersion(hint: string | null): string | null {
  if (!hint) {
    return null;
  }
  const m = /--version[= ]+"?(\d+(?:\.\d+){0,2})/.exec(hint) ?? /kicad[-_ ]?(\d+\.\d+(?:\.\d+)?)/i.exec(hint);
  return m ? m[1] : null;
}

/** `C:/work/demo/demo.kicad_pro` or `C:/work/demo` -> the project's name ("demo"). */
export function projectNameOf(project: string | null): string {
  if (!project) {
    return 'The project';
  }
  const trimmed = project.replace(/[\\/]+$/, '');
  const base = path.basename(trimmed.replace(/\\/g, '/'));
  return base.toLowerCase().endsWith('.kicad_pro') ? base.slice(0, -'.kicad_pro'.length) : base || trimmed;
}

/** One identity per project (a `.kicad_pro` path and its directory are the same project). */
export function projectKey(project: string | null): string {
  if (!project) {
    return '';
  }
  let p = project.replace(/\\/g, '/').replace(/\/+$/, '');
  if (p.toLowerCase().endsWith('.kicad_pro')) {
    p = p.slice(0, p.lastIndexOf('/') >= 0 ? p.lastIndexOf('/') : 0);
  }
  return p.toLowerCase();
}

/** Normalise the error's data (tolerant: fields may be missing on an older or partial server). */
export function formatErrorInfo(
  err: Pick<KiwrapError, 'code' | 'data'> & { candidates?: string[] },
  args: Record<string, unknown> = {},
): FormatErrorInfo {
  const d = (err.data ?? {}) as Record<string, unknown>;
  const files: FormatFile[] = Array.isArray(d.files)
    ? (d.files as unknown[])
        .filter((f): f is Record<string, unknown> => !!f && typeof f === 'object')
        .map((f) => ({ path: String(f.path ?? ''), kind: String(f.kind ?? ''), stamp: num(f.stamp) }))
    : [];
  const projectEpoch: Record<string, number> = {};
  if (d.project_epoch && typeof d.project_epoch === 'object') {
    for (const [k, v] of Object.entries(d.project_epoch as Record<string, unknown>)) {
      const n = num(v);
      if (n !== null) {
        projectEpoch[k] = n;
      }
    }
  } else {
    for (const f of files) {
      if (f.stamp !== null && !(f.kind in projectEpoch)) {
        projectEpoch[f.kind] = f.stamp;
      }
    }
  }
  const candidates: FormatCandidate[] = Array.isArray(d.candidates)
    ? (d.candidates as unknown[])
        .filter((c): c is Record<string, unknown> => !!c && typeof c === 'object' && typeof (c as Record<string, unknown>).version === 'string')
        .map((c) => ({
          version: String(c.version),
          formats: Object.fromEntries(
            Object.entries((c.formats as Record<string, unknown>) ?? {}).map(([k, v]) => [k, num(v)]),
          ),
        }))
    : [];
  const installHint = str(d.install_hint);
  const installVersion = hintVersion(installHint);
  const named = str(d.project_kicad) ?? str(d.format_kicad) ?? str(d.needed_kicad) ?? installVersion;
  const project = str(d.project) ?? str(args.project) ?? str(args.dir);
  return {
    code: err.code,
    project,
    projectName: projectNameOf(project),
    files,
    projectEpoch,
    candidates,
    upgradeTo: str(d.upgrade_to),
    installHint,
    formatKicad: named ? majorMinor(named) : null,
    installVersion,
    kicad: str(d.kicad),
    readers: err.code === E_FORMAT_NEWER && Array.isArray(err.candidates) ? err.candidates.map(String) : [],
    writer: err.code === E_FORMAT_UPGRADE_REQUIRED && Array.isArray(err.candidates) && err.candidates.length ? String(err.candidates[0]) : null,
  };
}

/**
 * The modal data of the proactive "kiwrap: Upgrade project file format" command, built from
 * `kw_project status` and kw_env's install rows. The server's `project_kicad` and `install_hint`
 * are passed through: in the D21 headline case (a 10.0 project, only 10.99 installed) no install
 * writes the files' format, so the local match is empty and the modal used to lose "KiCad 10.0"
 * and the winget command (fixer finding 3). The server's per-file stamps (`file_epochs`, sub-sheets
 * included) and its `candidates[].matches` (format generations, not exact stamps: a 10.0 demo sheet
 * stamped 20260101 is still written by 10.0.6) are preferred to the local approximations.
 * `writers` lists the installed KiCads that write the files' own format.
 */
export function proactiveUpgradeInfo(
  project: string,
  st: ProjectStatusData | undefined,
  rows: InstallRow[],
  upgradeTo: string | undefined,
): { info: FormatErrorInfo; writers: string[] } {
  const epochs: Record<string, number> = {};
  let files: FormatFile[] = [];
  if (Array.isArray(st?.file_epochs) && st.file_epochs.length) {
    files = st.file_epochs.map((f) => ({ path: String(f.path), kind: String(f.kind), stamp: num(f.stamp) }));
    for (const f of files) {
      if (f.stamp !== null) {
        epochs[f.kind] = Math.max(epochs[f.kind] ?? 0, f.stamp);
      }
    }
  } else {
    for (const [k, v] of Object.entries(st?.epochs ?? {})) {
      if (typeof v === 'number') {
        epochs[k] = v;
      }
    }
    files = [
      ...(st?.files.sch ?? []).map((f) => ({ path: f, kind: 'sch', stamp: epochs.sch ?? null })),
      ...(st?.files.pcb ?? []).map((f) => ({ path: f, kind: 'pcb', stamp: epochs.pcb ?? null })),
    ];
  }
  const writers = Array.isArray(st?.candidates)
    ? st.candidates.filter((c) => c && c.matches === true && typeof c.version === 'string').map((c) => c.version)
    : matchingInstalls(rows, epochs).map((r) => r.version);
  const info = formatErrorInfo(
    {
      code: E_FORMAT_UPGRADE_REQUIRED,
      data: {
        project,
        project_epoch: epochs,
        files,
        candidates: rows.map((r) => ({ version: r.version, formats: installFormats(r) })),
        upgrade_to: upgradeTo,
        project_kicad: st?.project_kicad ?? writers[0] ?? null,
        install_hint: st?.install_hint ?? null,
      },
    },
    { project },
  );
  info.writer = writers[0] ?? null;
  return { info, writers };
}

function epochText(epochs: Record<string, number>): string {
  const values = [...new Set(Object.values(epochs))];
  if (values.length <= 1) {
    return values.length ? String(values[0]) : '';
  }
  return Object.entries(epochs)
    .map(([k, v]) => `${k} ${v}`)
    .join(', ');
}

/** "KiCad 10.0" or, when the release is unknown, "KiCad file format 20260206". */
export function formatLabel(info: Pick<FormatErrorInfo, 'formatKicad' | 'projectEpoch'>): string {
  if (info.formatKicad) {
    return `KiCad ${info.formatKicad}`;
  }
  const e = epochText(info.projectEpoch);
  return e ? `KiCad file format ${e}` : 'an older KiCad file format';
}

function upgradeLabel(info: FormatErrorInfo): string {
  const to = info.upgradeTo ?? info.candidates[0]?.version ?? null;
  return to ? `KiCad ${majorMinor(to)}` : 'the newest installed KiCad';
}

export interface UpgradePrompt {
  message: string;
  detail: string;
  buttons: string[];
}

/** The modal's text and buttons (the wording of the owner's request, D21). */
export function upgradePrompt(info: FormatErrorInfo): UpgradePrompt {
  const x = formatLabel(info);
  const y = upgradeLabel(info);
  const noWriter = info.writer
    ? `installed KiCad ${info.writer} still writes it`
    : info.formatKicad
      ? `no installed ${x} can write it`
      : 'no installed KiCad can write it';
  const loses = info.formatKicad ? `${x} will no longer open them.` : 'KiCad releases of that format will no longer open them.';
  const message = `${info.projectName} is in ${x} format and ${noWriter}. Upgrade the project files to ${y} format? ${loses}`;
  const lines: string[] = [];
  if (info.files.length) {
    lines.push('Files that would be converted:');
    for (const f of info.files) {
      lines.push(`  ${path.basename(f.path.replace(/\\/g, '/')) || f.path}${f.kind ? ` (${f.kind}` : ' ('}${f.stamp !== null ? `, format ${f.stamp})` : ')'}`);
    }
  }
  if (info.candidates.length) {
    lines.push(
      `Installed: ${info.candidates
        .map((c) => {
          const fm = Object.entries(c.formats).filter(([, v]) => v !== null);
          return `KiCad ${c.version}${fm.length ? ` (${fm.map(([k, v]) => `${k} ${v}`).join(', ')})` : ''}`;
        })
        .join('; ')}`,
    );
  }
  lines.push(
    'Upgrade converts a copy with kicad-cli, takes a snapshot first (kw_snapshot restore undoes it) and reports ERC/DRC before and after.',
  );
  if (info.writer) {
    lines.push(`Or keep the files as they are and run the project on KiCad ${info.writer}, which writes this format.`);
  } else if (info.installHint) {
    lines.push(`Or keep the files as they are and install the matching KiCad: ${info.installHint}`);
  } else {
    lines.push(`Or keep the files as they are and install ${x} (${KICAD_DOWNLOAD_URL}).`);
  }
  const alternative = info.writer ? useButton(info.writer) : installButton(info);
  return { message, detail: lines.join('\n'), buttons: [UPGRADE_BUTTON, alternative, CANCEL_BUTTON] };
}

/** The E_FORMAT_NEWER message: names the KiCad that is needed. */
export function newerMessage(info: FormatErrorInfo, err: Pick<KiwrapError, 'message'>): string {
  const x = formatLabel(info);
  if (info.readers.length) {
    // a pinned (older) target refused the files, but another install reads them
    return `kiwrap: ${info.projectName} is in ${x} format, newer than KiCad ${info.kicad ?? 'the project target'}. Installed KiCad ${info.readers[0]} reads it: use that one.`;
  }
  const installed = info.candidates.map((c) => c.version).join(', ');
  const needed = info.formatKicad ? `Install KiCad ${info.formatKicad} or newer to work on it.` : `Install the KiCad that wrote it (${err.message.replace(/^E_[A-Z_]+:\s*/, '')}).`;
  return `kiwrap: ${info.projectName} is in ${x} format, newer than ${installed ? `every installed KiCad (${installed})` : 'every installed KiCad'}. ${needed}`;
}

/** The E_READONLY message (D23: `kiwrap serve --readonly` / `KIWRAP_READONLY=1` or `persist=false`). */
export function readonlyMessage(tool: string, err: Pick<KiwrapError, 'message' | 'fix'>): string {
  const why = err.message.replace(/^E_[A-Z_]+:\s*/, '');
  return `kiwrap is in read-only mode: ${tool} changed nothing (${why}).${err.fix ? ` ${err.fix}` : ''}`;
}

/** A one-line summary of the upgrade receipt (validators before/after, snapshot). */
export function upgradeSummary(info: FormatErrorInfo, r: Receipt<Record<string, unknown>>): string {
  const to = str(r.data?.upgraded_to) ?? str(r.data?.target_kicad) ?? info.upgradeTo;
  const checks = (r.validators ?? [])
    .map((v) => `${v.name} ${v.before ?? '?'} -> ${v.after ?? '?'}`)
    .join(', ');
  return (
    `kiwrap: upgraded ${info.projectName} to ${to ? `KiCad ${majorMinor(to)}` : 'the newer'} format` +
    `${checks ? ` (${checks})` : ''}${r.snapshot ? `; snapshot ${r.snapshot} restores the old files` : ''}.`
  );
}

type Decision = 'upgraded' | 'retargeted' | 'declined';

export interface ConsentHooks {
  /** After a successful upgrade or retarget (the extension refreshes the project status). */
  onProjectChanged?(project: string, receipt?: Receipt<Record<string, unknown>>): void;
}

function markHandled<E>(err: E): E {
  if (err && typeof err === 'object') {
    (err as { handled?: boolean }).handled = true;
  }
  return err;
}

export class FormatConsent {
  /** One modal per project at a time: concurrent calls share the answer. */
  private readonly pending = new Map<string, Promise<Decision>>();

  constructor(
    private readonly ui: ConsentUi,
    private readonly hooks: ConsentHooks = {},
  ) {}

  /** The `McpClient` hook. */
  readonly handle: ToolErrorHandler = async (ctx) => {
    switch (ctx.error.code) {
      case E_FORMAT_UPGRADE_REQUIRED:
        return this.onUpgradeRequired(ctx);
      case E_FORMAT_NEWER:
        return this.onNewer(ctx);
      case E_READONLY:
        return this.onReadonly(ctx);
      default:
        return undefined;
    }
  };

  private async onUpgradeRequired(ctx: ToolErrorContext): Promise<Receipt<Record<string, unknown>> | undefined> {
    const info = formatErrorInfo(ctx.error, ctx.args);
    this.ui.log(`${ctx.name}: ${ctx.error.message}`);
    const isUpgradeCall = ctx.name === 'kw_project' && ctx.args.action === 'upgrade';
    if (!ctx.interactive || ctx.afterConsent || isUpgradeCall) {
      // a background run logs it (its caller reports it quietly); a retry that still needs an
      // upgrade, or the upgrade call itself, is not asked about twice
      return undefined;
    }
    if (!info.project) {
      void this.ui.error(`kiwrap: ${upgradePrompt(info).message} (the server did not name the project, so kiwrap cannot upgrade it here)`);
      throw markHandled(ctx.error);
    }
    const decision = await this.decide(info, (n, a, o) => ctx.call(n, a, o));
    if (decision === 'declined') {
      throw markHandled(ctx.error);
    }
    this.ui.log(`${ctx.name}: retrying after the ${decision === 'upgraded' ? 'upgrade' : 'retarget'}`);
    return ctx.retry();
  }

  /**
   * Ask (or join the question already on screen for this project) and act on the answer. Also the
   * entry point of the proactive "Upgrade project file format" command.
   */
  decide(
    info: FormatErrorInfo,
    call: (name: string, args: Record<string, unknown>, opts?: CallOptions) => Promise<Receipt<Record<string, unknown>>>,
  ): Promise<Decision> {
    const key = projectKey(info.project);
    const inflight = this.pending.get(key);
    if (inflight) {
      return inflight;
    }
    const p = this.ask(info, call).finally(() => this.pending.delete(key));
    this.pending.set(key, p);
    return p;
  }

  private async ask(
    info: FormatErrorInfo,
    call: (name: string, args: Record<string, unknown>, opts?: CallOptions) => Promise<Receipt<Record<string, unknown>>>,
  ): Promise<Decision> {
    const prompt = upgradePrompt(info);
    const choice = await this.ui.modal(prompt.message, prompt.detail, prompt.buttons);
    this.ui.log(`format upgrade of ${info.projectName}: ${choice ?? 'dismissed'}`);
    if (choice === UPGRADE_BUTTON) {
      const args: Record<string, unknown> = { action: 'upgrade', project: info.project, confirm: true };
      if (info.upgradeTo) {
        args.to_kicad = info.upgradeTo;
      }
      let receipt: Receipt<Record<string, unknown>>;
      try {
        receipt = await call('kw_project', args);
      } catch (err) {
        const e = err as KiwrapError;
        if (!e.handled) {
          const text = `${e.message ?? String(err)}${e.fix ? ` - ${e.fix}` : ''}`;
          void this.ui.error(`kiwrap: upgrading ${info.projectName} failed: ${text}`);
          markHandled(err);
        }
        throw err;
      }
      const summary = upgradeSummary(info, receipt);
      this.ui.log(summary);
      void this.ui.info(summary);
      this.hooks.onProjectChanged?.(info.project ?? '', receipt);
      return 'upgraded';
    }
    if (info.writer && choice === useButton(info.writer)) {
      try {
        await call('kw_project', { action: 'retarget', project: info.project, target_kicad: info.writer });
      } catch (err) {
        const e = err as KiwrapError;
        if (!e.handled) {
          void this.ui.error(`kiwrap: pinning ${info.projectName} to KiCad ${info.writer} failed: ${e.message ?? String(err)}`);
          markHandled(err);
        }
        throw err;
      }
      this.ui.log(`pinned ${info.projectName} to KiCad ${info.writer} (its files' format)`);
      void this.ui.info(`kiwrap: ${info.projectName} now runs on KiCad ${info.writer}; its files keep their format.`);
      this.hooks.onProjectChanged?.(info.project ?? '');
      return 'retargeted';
    }
    if (choice === installButton(info)) {
      await this.ui.install(info.installHint, info.installVersion ?? info.formatKicad);
    }
    return 'declined';
  }

  private async onNewer(ctx: ToolErrorContext): Promise<undefined> {
    const info = formatErrorInfo(ctx.error, ctx.args);
    const text = newerMessage(info, ctx.error);
    this.ui.log(`${ctx.name}: ${ctx.error.message}`);
    if (!ctx.interactive) {
      return undefined;
    }
    const reader = info.readers[0];
    const project = info.project;
    const button = reader && project ? useButton(reader) : info.formatKicad || info.installHint ? installButton(info) : undefined;
    void this.ui.error(text, button ? [button] : []).then(async (c) => {
      if (!c || c !== button) {
        return undefined;
      }
      if (reader && project) {
        // pin the project to the install that reads it (kw_project retarget: files untouched)
        try {
          await ctx.call('kw_project', { action: 'retarget', project, target_kicad: reader });
          void this.ui.info(`kiwrap: ${info.projectName} now runs on KiCad ${reader}; run the command again.`);
          this.hooks.onProjectChanged?.(project);
        } catch (err) {
          if (!(err as KiwrapError).handled) {
            void this.ui.error(`kiwrap: pinning ${info.projectName} to KiCad ${reader} failed: ${(err as Error).message ?? String(err)}`);
          }
        }
        return undefined;
      }
      return this.ui.install(info.installHint, info.installVersion ?? info.formatKicad);
    });
    throw markHandled(ctx.error);
  }

  private async onReadonly(ctx: ToolErrorContext): Promise<undefined> {
    this.ui.log(`${ctx.name}: ${ctx.error.message}`);
    if (!ctx.interactive) {
      return undefined;
    }
    void this.ui.info(readonlyMessage(ctx.name, ctx.error));
    throw markHandled(ctx.error);
  }
}
