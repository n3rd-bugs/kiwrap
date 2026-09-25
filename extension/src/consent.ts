/**
 * The user's consent before kiwrap converts a project to a newer KiCad file format (D21, amended by
 * D24; review item 1 / B1: four default paths re-stamped a KiCad 10.0 project as 10.99, which 10.0
 * then refused to open). The server never up-converts on its own: an operation that would save the
 * design files in a newer format fails with `E_FORMAT_UPGRADE_REQUIRED`, and this module turns that
 * into a modal:
 *
 *   "<project> is in KiCad <x> format and no installed KiCad <x> can write it. Upgrade the project
 *    files to KiCad <y> format? KiCad <x> will no longer open them."
 *    [Upgrade]  [Install KiCad <x>...]  [Cancel]
 *
 * The detail lists what the server says gets converted: from, to and the files (D24 `upgrade_args`).
 * When an installed KiCad does write the files' format (the error's `candidates`: the server's
 * stamp guard rolled back a write that ran on a newer install), the second button is "Use KiCad
 * <v>" instead: `kw_project(action='retarget')` pins it (files untouched) and the call is retried.
 *
 * [Upgrade] calls `kw_project_upgrade(**upgrade_args)` (D24: the only conversion; converted on a
 * copy, snapshot first, ERC/DRC before and after) and retries the original call once. The
 * arguments are the error's `data.upgrade_args`, or `kw_project status`'s when the error carries
 * none, passed verbatim: the server recomputes them and refuses a difference with E_BAD_ARGS
 * (`data.mismatch`), which here means "the project changed; run the command again" (no retry).
 * This client identifies as `kiwrap-vscode`, and the modal click is the consent the server records
 * (consent gate/via `kiwrap_vscode_modal`), so [Upgrade] is the only sender of `kw_project_upgrade`:
 * `McpClient.call` refuses it without the `MODAL_CONSENT` token (review finding 6). No `confirm`
 * argument exists any more: it was the model attesting consent to itself (D24).
 *
 * A call that asked for another KiCad than the one the project runs on (kw_live `target_kicad`)
 * gets a refusal with `data.retarget_first` and no `upgrade_args` (review findings 2 and 3): its
 * modal offers [Upgrade] = pin that KiCad (`kw_project retarget`, files untouched), then
 * `kw_project_upgrade(retarget_first.upgrade_args)`, then the retry; a failed upgrade restores the pin.
 *
 * Install puts the error's `install_hint` (`winget install KiCad.KiCad --version <x>`) in a
 * terminal, or opens the KiCad download page. `E_FORMAT_NEWER` (files newer than every install)
 * becomes an error naming the KiCad that is needed, `E_READONLY` (D23) an information message. A
 * read-only server does not publish `kw_project_upgrade` (D24), so [Upgrade] there ends in the
 * read-only message, once, without a retry.
 *
 * It plugs into `McpClient` as `onToolError`, so every call the extension makes gets it: ERC / DRC,
 * the live launch (`kw_live`, also through the live module's injected client) and
 * `kiwrap.core.callTool`. Background calls (`interactive: false`: on-save checks, previews, the
 * status refresh) never open a modal. Errors the user has seen are marked `handled`, so the caller
 * does not show a second popup. No `vscode` import: the window API is the injected `ConsentUi`
 * (consentUi.ts binds it; the unit tests stub it).
 */
import * as path from 'node:path';
import { MODAL_CONSENT, type CallOptions, type ToolErrorContext, type ToolErrorHandler } from './mcpClient';
import { effectiveTarget, epochMatch, installFormats, matchingInstalls } from './status/installModel';
import {
  asUpgradeArgs,
  upgradeCallArgs,
  type InstallRow,
  type KiwrapError,
  type ProjectStatusData,
  type Receipt,
  type UpgradeArgs,
  type UpgradeData,
} from './types';

export const E_FORMAT_UPGRADE_REQUIRED = 'E_FORMAT_UPGRADE_REQUIRED';
export const E_FORMAT_NEWER = 'E_FORMAT_NEWER';
export const E_READONLY = 'E_READONLY';
export const E_BAD_ARGS = 'E_BAD_ARGS';
/** Client-side code of `McpClient.call` for a tool the server does not publish. */
export const E_NO_TOOL = 'E_NO_TOOL';

/** The D24 conversion tool (published only by a writable server). */
export const UPGRADE_TOOL = 'kw_project_upgrade';

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

/** The data of an `E_FORMAT_UPGRADE_REQUIRED` / `E_FORMAT_NEWER` error (contract (b)), or of `kw_project status` (the proactive command), normalised. */
export interface FormatErrorInfo {
  /** The error code; `status` when the proactive command built it from `kw_project status`. */
  code: string;
  /** The project to upgrade: the error's `data.project`, else `upgrade_args.project`, else the call's `project` argument. */
  project: string | null;
  projectName: string;
  files: FormatFile[];
  /** `{kind: epoch}` of the project's files. */
  projectEpoch: Record<string, number>;
  /** The installed KiCads the server considered, with the formats each writes. */
  candidates: FormatCandidate[];
  /** The install the upgrade converts to (`data.upgrade_to`, else a version-like `upgrade_args.to_kicad`). */
  upgradeTo: string | null;
  /**
   * D24: the `kw_project_upgrade` arguments (`data.upgrade_args`; fetched with `kw_project status`
   * when an older server's error carries none). [Upgrade] passes them verbatim. null = unknown: the
   * modal then offers no [Upgrade].
   */
  upgradeArgs: UpgradeArgs | null;
  /** A command that installs the matching release (`winget install KiCad.KiCad --version <x>`). */
  installHint: string | null;
  /** The release line the files belong to ("10.0"): `data.project_kicad` / `format_kicad` / `needed_kicad` / `upgrade_args.from_kicad`, else the hint's version. */
  formatKicad: string | null;
  /** The exact release the hint installs. */
  installVersion: string | null;
  /**
   * An installed KiCad that does write the files' format: the error's `candidates` (the server's
   * stamp guard rolled back a write that ran on a newer install although a matching one exists),
   * or the server's match in the proactive "Upgrade project file format" command. The modal then
   * offers "Use KiCad <writer>" (kw_project retarget, files untouched) instead of an install.
   */
  writer?: string | null;
  /**
   * The error's `data.retarget_first` (review findings 2 and 3): the call asked for another KiCad than
   * the one the project runs on (kw_live `target_kicad`, from the kiwrap.kicadInstall setting). The
   * server then attaches no `upgrade_args` - `kw_project_upgrade` recomputes them from the project's
   * own target and would refuse them - but says which install to pin first and the `upgrade_args`
   * `kw_project status` returns after that pin. [Upgrade] runs `kw_project retarget` (files untouched)
   * and then `kw_project_upgrade(upgradeArgs)`; a failed upgrade puts `pinBefore` back ('auto' when null).
   */
  retargetFirst?: { targetKicad: string; pinBefore: string | null; runsOn: string | null } | null;
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

/** A version spec ("10", "10.0", "10.0.6", "10.99.0-rc1"), else null (an install root, `format sch=N,pcb=M`). */
function versionLike(v: unknown): string | null {
  const s = str(v);
  return s && /^\d+(?:\.\d+){0,2}(?:[-+].*)?$/.test(s) ? s : null;
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

function formatFiles(v: unknown): FormatFile[] {
  return Array.isArray(v)
    ? (v as unknown[])
        .filter((f): f is Record<string, unknown> => !!f && typeof f === 'object')
        .map((f) => ({ path: String(f.path ?? ''), kind: String(f.kind ?? ''), stamp: num(f.stamp) }))
    : [];
}

/** Normalise the error's data (tolerant: fields may be missing on an older or partial server). */
export function formatErrorInfo(
  err: Pick<KiwrapError, 'code' | 'data'> & { candidates?: string[] },
  args: Record<string, unknown> = {},
): FormatErrorInfo {
  const d = (err.data ?? {}) as Record<string, unknown>;
  const files = formatFiles(d.files);
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
  const rf = d.retarget_first && typeof d.retarget_first === 'object' ? (d.retarget_first as Record<string, unknown>) : null;
  const retargetFirst =
    err.code === E_FORMAT_UPGRADE_REQUIRED && rf && str(rf.target_kicad)
      ? { targetKicad: str(rf.target_kicad) as string, pinBefore: str(rf.target_kicad_before), runsOn: str(d.runs_on) }
      : null;
  const upgradeArgs = asUpgradeArgs(d.upgrade_args) ?? (retargetFirst ? asUpgradeArgs(rf?.upgrade_args) : null);
  const installHint = str(d.install_hint);
  const installVersion = hintVersion(installHint);
  const named =
    str(d.project_kicad) ?? str(d.format_kicad) ?? str(d.needed_kicad) ?? versionLike(upgradeArgs?.from_kicad) ?? installVersion;
  const project = str(d.project) ?? upgradeArgs?.project ?? str(args.project) ?? str(args.dir);
  return {
    code: err.code,
    project,
    projectName: projectNameOf(project),
    files,
    projectEpoch,
    candidates,
    upgradeTo: str(d.upgrade_to) ?? versionLike(upgradeArgs?.to_kicad),
    upgradeArgs,
    installHint,
    formatKicad: named ? majorMinor(named) : null,
    installVersion,
    kicad: str(d.kicad),
    readers: err.code === E_FORMAT_NEWER && Array.isArray(err.candidates) ? err.candidates.map(String) : [],
    // with retarget_first the retried call asks for the other KiCad again: "Use KiCad <writer>" would
    // pin a KiCad the call does not use, so no writer button is offered
    writer:
      err.code === E_FORMAT_UPGRADE_REQUIRED && !retargetFirst && Array.isArray(err.candidates) && err.candidates.length
        ? String(err.candidates[0])
        : null,
    retargetFirst: retargetFirst && upgradeArgs ? retargetFirst : null,
  };
}

/**
 * The modal data of the proactive "kiwrap: Upgrade project file format" command, built from
 * `kw_project status` (D24: its `upgrade_args`, present whenever a write would need an upgrade)
 * and kw_env's install rows - no error is faked locally. The server's `project_kicad` and
 * `install_hint` are passed through: in the D21 headline case (a 10.0 project, only 10.99
 * installed) no install writes the files' format, so a local match is empty and the modal would
 * lose "KiCad 10.0" and the winget command (fixer finding 3). The server's per-file stamps
 * (`file_epochs`, sub-sheets included) and its `candidates[].matches` (format generations, not
 * exact stamps: a 10.0 demo sheet stamped 20260101 is still written by 10.0.6) are preferred to
 * the local approximations. `writers` lists the installed KiCads that write the files' own format.
 */
export function proactiveUpgradeInfo(
  project: string,
  st: ProjectStatusData | undefined,
  rows: InstallRow[],
): { info: FormatErrorInfo; writers: string[] } {
  const epochs: Record<string, number> = {};
  let files: FormatFile[] = [];
  if (Array.isArray(st?.file_epochs) && st.file_epochs.length) {
    files = formatFiles(st.file_epochs);
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
  const upgradeArgs = asUpgradeArgs(st?.upgrade_args);
  const installHint = str(st?.install_hint);
  const installVersion = hintVersion(installHint);
  const named = str(st?.project_kicad) ?? versionLike(upgradeArgs?.from_kicad) ?? writers[0] ?? installVersion;
  const proj = upgradeArgs?.project ?? project;
  const info: FormatErrorInfo = {
    code: 'status',
    project: proj,
    projectName: projectNameOf(proj),
    files,
    projectEpoch: epochs,
    candidates: rows.map((r) => ({ version: r.version, formats: installFormats(r) })),
    upgradeTo: versionLike(upgradeArgs?.to_kicad) ?? effectiveTarget(st),
    upgradeArgs,
    installHint,
    formatKicad: named ? majorMinor(named) : null,
    installVersion,
    kicad: null,
    readers: [],
    writer: writers[0] ?? null,
  };
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

function formatsText(formats: Record<string, number | null | undefined> | undefined): string {
  const fm = Object.entries(formats ?? {}).filter(([, v]) => typeof v === 'number');
  return fm.map(([k, v]) => `${k} ${v}`).join(', ');
}

/**
 * The version the upgrade converts to. `upgrade_args.to_kicad` is a version, or an install root when
 * installs sharing a version write different formats (D24): a root is named by the candidate whose
 * formats equal `to_format`.
 */
export function upgradeTarget(info: Pick<FormatErrorInfo, 'upgradeArgs' | 'upgradeTo' | 'candidates'>): string | null {
  const a = info.upgradeArgs;
  const v = versionLike(a?.to_kicad);
  if (v) {
    return v;
  }
  const want = Object.entries(a?.to_format ?? {}).filter(([, n]) => typeof n === 'number');
  if (want.length) {
    const c = info.candidates.find((cand) => want.every(([k, n]) => cand.formats[k] === n));
    if (c) {
      return c.version;
    }
  }
  return info.upgradeTo ?? info.candidates[0]?.version ?? null;
}

function upgradeLabel(info: FormatErrorInfo): string {
  const to = upgradeTarget(info);
  return to ? `KiCad ${majorMinor(to)}` : 'the newest installed KiCad';
}

/** `upgrade_args.from_kicad` in words: "KiCad 10.0", else the format stamps (`format sch=N,pcb=M`). */
function fromText(a: UpgradeArgs): string {
  const v = versionLike(a.from_kicad);
  return v ? `KiCad ${v}` : `KiCad file ${a.from_kicad.replace(/=/g, ' ').replace(/,\s*/g, ', ')}`;
}

function toText(a: UpgradeArgs, info: FormatErrorInfo): string {
  const version = upgradeTarget(info);
  const where = versionLike(a.to_kicad) ? '' : ` at ${a.to_kicad}`;
  const fm = formatsText(a.to_format);
  return `${version ? `KiCad ${version}` : 'KiCad'}${where}${fm ? ` (${fm})` : ''}`;
}

/** The stamp the error / status reported for a project-relative file, when it did. */
function stampOf(rel: string, files: FormatFile[]): FormatFile | undefined {
  const want = rel.replace(/\\/g, '/').toLowerCase();
  return files.find((f) => {
    const p = f.path.replace(/\\/g, '/').toLowerCase();
    return p === want || p.endsWith(`/${want}`);
  });
}

export interface UpgradePrompt {
  message: string;
  detail: string;
  buttons: string[];
}

/** The modal's text and buttons (the wording of the owner's request, D21; from, to and files per D24). */
export function upgradePrompt(info: FormatErrorInfo): UpgradePrompt {
  const x = formatLabel(info);
  const y = upgradeLabel(info);
  const noWriter = info.writer
    ? `installed KiCad ${info.writer} still writes it`
    : info.formatKicad
      ? `no installed ${x} can write it`
      : 'no installed KiCad can write it';
  const loses = info.formatKicad ? `${x} will no longer open them.` : 'KiCad releases of that format will no longer open them.';
  const rf = info.retargetFirst;
  if (rf && info.upgradeArgs) {
    return retargetFirstPrompt(info, rf, x, loses);
  }
  const message = info.upgradeArgs || !info.writer
    ? `${info.projectName} is in ${x} format and ${noWriter}. Upgrade the project files to ${y} format? ${loses}`
    : // no upgrade is pending for the KiCad the project runs on (the stamp guard caught a write by
      // another install, review finding 8): the one useful answer is the writer
      `${info.projectName} is in ${x} format and ${noWriter}; a write by KiCad ${upgradeTarget(info) ?? 'another install'} would have converted it, so nothing was saved.`;
  const lines: string[] = [];
  const a = info.upgradeArgs;
  if (a) {
    lines.push(`From: ${fromText(a)}`);
    lines.push(`To: ${toText(a, info)}`);
    lines.push(`Files that will be converted (${a.files.length}):`);
    for (const rel of a.files) {
      const f = stampOf(rel, info.files);
      lines.push(`  ${rel}${f ? ` (${f.kind || '?'}${f.stamp !== null ? `, format ${f.stamp}` : ''})` : ''}`);
    }
    lines.push('Never converted: .kicad_pro, .kicad_prl, .kicad_dru, the library tables and project libraries.');
  } else if (info.files.length) {
    lines.push('Files that would be converted:');
    for (const f of info.files) {
      lines.push(`  ${path.basename(f.path.replace(/\\/g, '/')) || f.path}${f.kind ? ` (${f.kind}` : ' ('}${f.stamp !== null ? `, format ${f.stamp})` : ')'}`);
    }
  }
  if (info.candidates.length) {
    lines.push(
      `Installed: ${info.candidates
        .map((c) => {
          const fm = formatsText(c.formats);
          return `KiCad ${c.version}${fm ? ` (${fm})` : ''}`;
        })
        .join('; ')}`,
    );
  }
  if (a) {
    lines.push(
      'Upgrade converts a copy with kicad-cli, takes a snapshot first (kw_snapshot restore undoes it) and reports ERC/DRC before and after.',
    );
  } else if (!info.writer) {
    lines.push(
      'This kiwrap server did not say which files an upgrade converts (no upgrade_args), so it cannot be upgraded from here: update kiwrap and restart the server.',
    );
  }
  const keep = a ? 'Or keep' : 'Keep';
  if (info.writer) {
    lines.push(`${keep} the files as they are and run the project on KiCad ${info.writer}, which writes this format.`);
  } else if (info.installHint) {
    lines.push(`${keep} the files as they are and install the matching KiCad: ${info.installHint}`);
  } else {
    lines.push(`${keep} the files as they are and install ${x} (${KICAD_DOWNLOAD_URL}).`);
  }
  const alternative = info.writer ? useButton(info.writer) : installButton(info);
  const buttons = a ? [UPGRADE_BUTTON, alternative, CANCEL_BUTTON] : [alternative, CANCEL_BUTTON];
  return { message, detail: lines.join('\n'), buttons };
}

/**
 * The modal for a call that asked for another KiCad than the one the project runs on
 * (`retarget_first`, review findings 2 and 3): [Upgrade] pins that KiCad first, then converts. No
 * "Use KiCad <v>" / install button: the retried call asks for the same KiCad again.
 */
function retargetFirstPrompt(
  info: FormatErrorInfo,
  rf: NonNullable<FormatErrorInfo['retargetFirst']>,
  x: string,
  loses: string,
): UpgradePrompt {
  const a = info.upgradeArgs as UpgradeArgs;
  const to = upgradeTarget(info);
  const toLabel = to ? `KiCad ${to}` : `the KiCad at ${rf.targetKicad}`;
  const runs = rf.runsOn ? `runs on KiCad ${rf.runsOn}` : 'runs on another KiCad';
  const message =
    `${info.projectName} ${runs}; the selected ${toLabel} would convert its ${x}-format files. ` +
    `Pin the project to ${toLabel} and upgrade the files to its format? ${loses}`;
  const lines = [`From: ${fromText(a)}`, `To: ${toText(a, info)}`, `Files that will be converted (${a.files.length}):`];
  for (const rel of a.files) {
    const f = stampOf(rel, info.files);
    lines.push(`  ${rel}${f ? ` (${f.kind || '?'}${f.stamp !== null ? `, format ${f.stamp}` : ''})` : ''}`);
  }
  lines.push('Never converted: .kicad_pro, .kicad_prl, .kicad_dru, the library tables and project libraries.');
  lines.push(
    `Upgrade first pins the project to ${toLabel} (kw_project retarget, no file changes), then converts a copy with kicad-cli, takes a snapshot first (kw_snapshot restore undoes it) and reports ERC/DRC before and after.`,
  );
  lines.push(
    `Or keep the files as they are${rf.runsOn ? ` and run the project on KiCad ${rf.runsOn}` : ''}: set the KiCad install to match the project's file format (kiwrap: Select KiCad install).`,
  );
  return { message, detail: lines.join('\n'), buttons: [UPGRADE_BUTTON, CANCEL_BUTTON] };
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

/**
 * What a server that does not publish `kw_project_upgrade` means (D24: a read-only server does not
 * register it; a server older than D24 never had it). Shown with `readonlyMessage`.
 */
export const NO_UPGRADE_TOOL = {
  message: `this kiwrap server does not offer ${UPGRADE_TOOL}`,
  fix: 'A read-only server (kiwrap serve --readonly, D23) never converts files: restart it without --readonly, or update kiwrap if it predates kw_project_upgrade.',
};

/** The mismatch message (D24: the server recomputed `upgrade_args` and they differ from the modal's). */
export function changedMessage(info: Pick<FormatErrorInfo, 'projectName'>): string {
  return `kiwrap: the project ${info.projectName} changed; run the command again. Nothing was converted.`;
}

/** "erc 2 -> 3, drc 5 -> 5" from `checks {before, after}` (numbers, or `{errors: n}` per check). */
function checksText(checks: UpgradeData['checks']): string {
  const count = (v: unknown): number | null =>
    typeof v === 'number' ? v : v && typeof v === 'object' ? num((v as Record<string, unknown>).errors) : null;
  const before = (checks?.before ?? {}) as Record<string, unknown>;
  const after = (checks?.after ?? {}) as Record<string, unknown>;
  return [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .map((k) => [k, count(before[k]), count(after[k])] as const)
    .filter(([, b, a]) => b !== null || a !== null)
    .map(([k, b, a]) => `${k} ${b ?? '?'} -> ${a ?? '?'}`)
    .join(', ');
}

/** A one-line summary of the `kw_project_upgrade` receipt (files converted, ERC/DRC before/after, the undo). */
export function upgradeSummary(info: FormatErrorInfo, r: Receipt<Record<string, unknown>>): string {
  const d = r.data as UpgradeData;
  const to = str(d.target?.kicad) ?? upgradeTarget(info);
  const converted = Array.isArray(d.files) ? d.files.filter((f) => f && f.changed).length : Array.isArray(d.replaced) ? d.replaced.length : null;
  const checks =
    (r.validators ?? []).map((v) => `${v.name} ${v.before ?? '?'} -> ${v.after ?? '?'}`).join(', ') || checksText(d.checks);
  const inside = [converted !== null ? `${converted} file${converted === 1 ? '' : 's'}` : '', checks].filter(Boolean).join('; ');
  const dropped = Array.isArray(d.stage_dropped) && d.stage_dropped.length ? `; stage ${d.stage_dropped.join(', ')} dropped` : '';
  const snapshot = str(d.snapshot_before) ?? r.snapshot;
  return (
    `kiwrap: upgraded ${info.projectName} to ${to ? `KiCad ${majorMinor(to)}` : 'the newer'} format` +
    `${inside ? ` (${inside})` : ''}${dropped}${snapshot ? `; kw_snapshot restore ${snapshot} undoes it` : ''}.`
  );
}

export type Decision = 'upgraded' | 'retargeted' | 'declined';

/** What the proactive command did: a modal decision, or why no modal was needed. */
export type ProactiveOutcome = Decision | 'up_to_date' | 'unavailable';

export interface ConsentHooks {
  /** After a successful upgrade or retarget, or a refused one (the extension refreshes the project status). */
  onProjectChanged?(project: string, receipt?: Receipt<Record<string, unknown>>): void;
}

type CallFn = (name: string, args: Record<string, unknown>, opts?: CallOptions) => Promise<Receipt<Record<string, unknown>>>;

function markHandled<E>(err: E): E {
  if (err && typeof err === 'object') {
    (err as { handled?: boolean }).handled = true;
  }
  return err;
}

function errorCode(err: unknown): string {
  return err && typeof err === 'object' && typeof (err as { code?: unknown }).code === 'string' ? (err as { code: string }).code : '';
}

/**
 * An E_BAD_ARGS from `kw_project_upgrade` whose `data.mismatch` names a difference (D24: the project
 * changed since the modal). The server's "nothing to convert" refusal carries `mismatch: null` and
 * names kw_project retarget instead - it must not read as "the project changed" (review finding 9).
 */
export function hasMismatch(data: unknown): boolean {
  const m = data && typeof data === 'object' ? (data as Record<string, unknown>).mismatch : null;
  return !!m && typeof m === 'object' && Object.keys(m as Record<string, unknown>).length > 0;
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
    const isUpgradeCall = ctx.name === UPGRADE_TOOL;
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
   * entry point of the proactive "Upgrade project file format" command. A failed upgrade or
   * retarget rejects with the error, already shown and marked `handled`.
   */
  decide(info: FormatErrorInfo, call: CallFn): Promise<Decision> {
    const key = projectKey(info.project);
    const inflight = this.pending.get(key);
    if (inflight) {
      return inflight;
    }
    const p = this.ask(info, call).finally(() => this.pending.delete(key));
    this.pending.set(key, p);
    return p;
  }

  /**
   * The proactive "kiwrap: Upgrade project file format" command (D24): `kw_project status`
   * (background, `interactive: false`) supplies `upgrade_args`, then the modal, then
   * `kw_project_upgrade(**upgrade_args)`. No error is built locally. Without `upgrade_args` no
   * write needs an upgrade, so there is nothing to ask; without `kw_project_upgrade` (a read-only
   * or older server) the read-only message replaces a modal whose [Upgrade] could only fail.
   * `hasUpgradeTool` is `McpClient.hasTool('kw_project_upgrade')`.
   */
  async proactive(project: string, rows: InstallRow[], call: CallFn, hasUpgradeTool: boolean): Promise<ProactiveOutcome> {
    const status = await call('kw_project', { action: 'status', project }, { interactive: false, handleErrors: false });
    const st = status.data as unknown as ProjectStatusData;
    const { info } = proactiveUpgradeInfo(project, st, rows);
    if (!info.upgradeArgs) {
      const effective = effectiveTarget(st);
      const match = epochMatch(st);
      const text =
        match === false
          ? `kiwrap: this kiwrap server does not report which files of ${info.projectName} an upgrade converts (no upgrade_args); update kiwrap and restart the server.`
          : `kiwrap: ${info.projectName} is already in the format ${effective ? `KiCad ${effective}` : 'its KiCad'} writes; nothing to upgrade. ` +
            'To move it to a newer KiCad, pin the project to that install (Select KiCad install), then run this command again.';
      this.ui.log(text);
      void this.ui.info(text);
      return match === false ? 'unavailable' : 'up_to_date';
    }
    if (!hasUpgradeTool) {
      void this.ui.info(readonlyMessage(UPGRADE_TOOL, NO_UPGRADE_TOOL));
      return 'unavailable';
    }
    this.ui.log(`upgrade requested for ${info.projectName} (${info.upgradeArgs.from_kicad} -> ${info.upgradeArgs.to_kicad})`);
    return this.decide(info, call);
  }

  /** `kw_project status`'s `upgrade_args` for an error that carries none (an older server's refusal). */
  private async fetchUpgradeArgs(project: string, call: CallFn): Promise<UpgradeArgs | null> {
    try {
      const r = await call('kw_project', { action: 'status', project }, { interactive: false, handleErrors: false });
      return asUpgradeArgs((r.data as { upgrade_args?: unknown }).upgrade_args);
    } catch (err) {
      this.ui.log(`kw_project status for the upgrade arguments failed: ${(err as Error).message ?? String(err)}`);
      return null;
    }
  }

  private async ask(info: FormatErrorInfo, call: CallFn): Promise<Decision> {
    if (!info.upgradeArgs && info.project && !info.retargetFirst) {
      info = { ...info, upgradeArgs: await this.fetchUpgradeArgs(info.project, call) };
    }
    const prompt = upgradePrompt(info);
    const choice = await this.ui.modal(prompt.message, prompt.detail, prompt.buttons);
    this.ui.log(`format upgrade of ${info.projectName}: ${choice ?? 'dismissed'}`);
    if (choice === UPGRADE_BUTTON && info.upgradeArgs) {
      return this.upgrade(info, info.upgradeArgs, call);
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

  /**
   * [Upgrade]: `kw_project_upgrade` with exactly `upgrade_args` (D24). `handleErrors: false`, so a
   * failure never reaches this handler again: a failed upgrade never reopens the modal. Each
   * failure is shown once and rethrown `handled`; the original call is not retried.
   */
  private async upgrade(info: FormatErrorInfo, args: UpgradeArgs, call: CallFn): Promise<Decision> {
    const rf = info.retargetFirst;
    if (rf) {
      // the call asked for another KiCad than the project's own: pin it first (files untouched), so
      // the server's recompute yields exactly the upgrade_args the modal showed (findings 2 and 3)
      try {
        await call('kw_project', { action: 'retarget', project: args.project, target_kicad: rf.targetKicad }, { handleErrors: false });
      } catch (err) {
        throw this.upgradeFailed(info, err);
      }
      this.ui.log(`pinned ${info.projectName} to ${rf.targetKicad} before the upgrade`);
    }
    let receipt: Receipt<Record<string, unknown>>;
    try {
      // MODAL_CONSENT: the user just clicked [Upgrade] - the only place kw_project_upgrade is sent from (finding 6)
      receipt = await call(UPGRADE_TOOL, upgradeCallArgs(args), { handleErrors: false, consent: MODAL_CONSENT });
    } catch (err) {
      if (rf) {
        await this.restorePin(info, args.project, rf.pinBefore, call);
      }
      throw this.upgradeFailed(info, err);
    }
    const summary = upgradeSummary(info, receipt);
    this.ui.log(summary);
    for (const w of receipt.warnings ?? []) {
      this.ui.log(`${UPGRADE_TOOL}: ${w}`);
    }
    void this.ui.info(summary);
    this.hooks.onProjectChanged?.(info.project ?? args.project, receipt);
    return 'upgraded';
  }

  /** Put the pin a failed retarget-first upgrade replaced back ('auto' when there was none); best effort, logged. */
  private async restorePin(info: FormatErrorInfo, project: string, before: string | null, call: CallFn): Promise<void> {
    try {
      await call('kw_project', { action: 'retarget', project, target_kicad: before ?? 'auto' }, { handleErrors: false, interactive: false });
      this.ui.log(`the upgrade failed: ${info.projectName} is pinned to ${before ?? 'auto'} again`);
    } catch (err) {
      this.ui.log(`the upgrade failed and restoring the pin of ${info.projectName} failed too: ${(err as Error).message ?? String(err)}`);
    }
    this.hooks.onProjectChanged?.(project);
  }

  private upgradeFailed(info: FormatErrorInfo, err: unknown): unknown {
    const e = (err && typeof err === 'object' ? err : { message: String(err) }) as Partial<KiwrapError>;
    if (e.handled) {
      return err;
    }
    const code = errorCode(err);
    this.ui.log(`${UPGRADE_TOOL}: ${e.message ?? String(err)}`);
    if (code === E_NO_TOOL) {
      void this.ui.info(readonlyMessage(UPGRADE_TOOL, NO_UPGRADE_TOOL));
    } else if (code === E_READONLY) {
      void this.ui.info(readonlyMessage(UPGRADE_TOOL, { message: e.message ?? '', fix: e.fix ?? '' }));
    } else if (code === E_BAD_ARGS && hasMismatch(e.data)) {
      // D24: the server recomputed upgrade_args under the project lock and they differ from what
      // the modal showed; the user saw other files than would be converted, so nothing is retried
      void this.ui.error(changedMessage(info));
      this.hooks.onProjectChanged?.(info.project ?? '');
    } else {
      const text = `${e.message ?? String(err)}${e.fix ? ` - ${e.fix}` : ''}`;
      void this.ui.error(`kiwrap: upgrading ${info.projectName} failed: ${text}`);
    }
    return markHandled(err);
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
