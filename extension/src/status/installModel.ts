/**
 * What the "KiCad (kiwrap)" tree says about installs and the project's target, free of `vscode`
 * imports so the unit tests cover it with faked kw_env / kw_project status payloads.
 *
 * D21 (review item 1, fix layer 1 + 2): kiwrap runs a project on the install whose file-format
 * epoch matches the project's files; `kw_project status` reports that choice as `target_effective`
 * with `target_source` (`pinned` | `file_epoch` | `file_epoch_newer_readonly` | `created`),
 * `epoch_match` and `warnings`. `kiwrap.kicadInstall` empty (or "project") follows it; a version or
 * root there is an explicit choice that overrides it for the extension's own launches.
 */
import type { EnvData, InstallRow, ProjectStatusData } from '../types';

/** Values of kiwrap.kicadInstall that mean "match the project's file format" (D21). */
export function isProjectInstallSpec(value: string | undefined | null): boolean {
  const v = (value ?? '').trim().toLowerCase();
  return v === '' || v === 'project';
}

/** Whether several kw_env rows share this row's version (bench builds of one nightly). */
function sharesVersion(row: InstallRow, rows: InstallRow[]): boolean {
  return rows.filter((r) => r.version === row.version).length > 1;
}

/**
 * The row's label: `KiCad <version>`, plus the install root's basename for an explicitly registered
 * install or when several rows share the version, so the user can tell `KiCad 10.99.0 (6dd893f7d1-7)`
 * from the stock `KiCad 10.99.0`.
 */
export function installLabel(row: InstallRow, rows: InstallRow[]): string {
  if (row.source === 'explicit' || sharesVersion(row, rows)) {
    const base = row.root.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || row.root;
    return `KiCad ${row.version} (${base})`;
  }
  return `KiCad ${row.version}`;
}

/** The spec `kiwrap.kicadInstall` needs to select exactly this row: its root when the version is ambiguous. */
export function installSpec(row: InstallRow, rows: InstallRow[]): string {
  return row.source === 'explicit' || sharesVersion(row, rows) ? row.root : row.version;
}

/** Does a version spec ("10", "10.0", "10.0.6") or a path match this install row? */
export function matchesSpec(row: InstallRow, spec: string): boolean {
  const s = spec.trim();
  if (/^\d+(\.\d+){0,2}$/.test(s)) {
    return row.version === s || row.version.startsWith(s + '.');
  }
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  return norm(row.root) === norm(s) || norm(row.kicad_cli) === norm(s);
}

/** The file formats an install writes (`formats.sch` / `formats.pcb` capabilities), when kw_env reported them. */
export function installFormats(row: InstallRow): Record<string, number> {
  const out: Record<string, number> = {};
  for (const kind of ['sch', 'pcb']) {
    const v = row.capabilities?.[`formats.${kind}`];
    if (typeof v === 'number') {
      out[kind] = v;
    }
  }
  return out;
}

/** Installs whose formats equal the project's file stamps (newest first, as kw_env lists them). */
export function matchingInstalls(rows: InstallRow[], epochs: Record<string, number | null> | undefined): InstallRow[] {
  const wanted = Object.entries(epochs ?? {}).filter((e): e is [string, number] => typeof e[1] === 'number');
  if (!wanted.length) {
    return [];
  }
  return rows.filter((r) => {
    const f = installFormats(r);
    return wanted.every(([k, v]) => f[k] === v);
  });
}

/** The effective target of a status payload (the contract's name, then the review's spelling). */
export function effectiveTarget(st: ProjectStatusData | undefined): string | null {
  return st?.target_effective ?? st?.target_kicad_effective ?? null;
}

/** Plain words for `target_source`. */
export function sourceText(source: string | null | undefined): string {
  switch (source) {
    case 'pinned':
      return 'pinned';
    case 'file_epoch':
      return "matches the files' format";
    case 'file_epoch_newer_readonly':
      return 'newer than the files: read-only until upgraded';
    case 'created':
      return 'created by kiwrap';
    case null:
    case undefined:
    case '':
      return '';
    default:
      return source;
  }
}

/** Whether the server says the effective target writes the files' own format (null: not reported). */
export function epochMatch(st: ProjectStatusData | undefined): boolean | null {
  const m = st?.epoch_match;
  if (typeof m === 'boolean') {
    return m;
  }
  if (m && typeof m === 'object') {
    const values = Object.values(m).filter((v): v is boolean => typeof v === 'boolean');
    return values.length ? values.every(Boolean) : null;
  }
  return null;
}

export interface FieldText {
  description: string;
  tooltip: string;
  /** codicon name: `warning` when the files and the target disagree. */
  icon?: string;
}

/** The project's "target KiCad" field: the effective target and why (review item 1: status shows it). */
export function targetField(st: ProjectStatusData | undefined, explicit: string | undefined): FieldText {
  const effective = effectiveTarget(st);
  const source = st?.target_source ?? null;
  const lines: string[] = [];
  let description: string;
  if (effective) {
    const why = sourceText(source);
    description = why ? `${effective} (${why})` : effective;
    lines.push(`kiwrap runs this project on KiCad ${effective}${why ? ` - ${why}` : ''}.`);
  } else if (st?.target_kicad) {
    description = `${st.target_kicad} (pinned)`;
    lines.push(`Pinned in .kiwrap/state.json: KiCad ${st.target_kicad}.`);
  } else if (st) {
    description = 'matched to the file format by the server';
    lines.push('This kiwrap server does not report its effective target yet (kw_project status target_effective).');
  } else {
    description = 'status pending';
  }
  if (st?.target_kicad && effective && st.target_kicad !== effective) {
    lines.push(`Pinned target: ${st.target_kicad}.`);
  }
  if (explicit) {
    lines.push(`kiwrap.kicadInstall = ${explicit}: the extension's live launches use it instead.`);
  } else {
    lines.push('kiwrap.kicadInstall is empty: the extension follows the project (D21).');
  }
  const readOnly = source === 'file_epoch_newer_readonly' || epochMatch(st) === false;
  return { description, tooltip: lines.join('\n'), icon: readOnly ? 'warning' : undefined };
}

/** The project's "file format" field: the stamps and whether an installed KiCad writes them. */
export function formatField(st: ProjectStatusData | undefined, env: EnvData | undefined): FieldText | undefined {
  if (!st) {
    return undefined;
  }
  const epochs = st.epochs ?? { sch: null, pcb: null };
  const parts = Object.entries(epochs)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k} ${v}`);
  if (!parts.length) {
    return undefined;
  }
  const local = matchingInstalls(env?.installs ?? [], epochs);
  const server = epochMatch(st);
  const effective = effectiveTarget(st);
  let verdict: string;
  let icon: string | undefined;
  if (server === true) {
    verdict = effective ? `KiCad ${effective} writes it` : 'an installed KiCad writes it';
    icon = 'pass';
  } else if (server === false) {
    verdict = 'no installed KiCad writes it';
    icon = 'warning';
  } else if (local.length) {
    verdict = `KiCad ${local[0].version} writes it`;
    icon = 'pass';
  } else if ((env?.installs ?? []).some((r) => Object.keys(installFormats(r)).length)) {
    verdict = 'no installed KiCad writes it';
    icon = 'warning';
  } else {
    verdict = '';
  }
  const tooltip = [
    `File format stamps: ${parts.join(', ')}.`,
    local.length ? `Installed KiCads writing exactly this format: ${local.map((r) => r.version).join(', ')}.` : 'No installed KiCad writes exactly this format.',
    icon === 'warning' ? 'Saving would upgrade the files; kiwrap asks first (Upgrade / Install the matching KiCad).' : '',
  ]
    .filter(Boolean)
    .join('\n');
  return { description: verdict ? `${parts.join(', ')} - ${verdict}` : parts.join(', '), tooltip, icon };
}

export interface InstallRoles {
  /** The row kiwrap.kicadInstall selects explicitly. */
  selected: boolean;
  /** The row kiwrap uses for the tracked project (its effective target). */
  project: boolean;
  /** kw_env's default (the newest install). */
  newest: boolean;
}

/**
 * Which row carries which role. kw_env lists newest first and `find()` takes the first prefix match,
 * so each role marks one row (six bench builds of one nightly are six rows - reviewer finding 18).
 */
export function installRoles(
  rows: InstallRow[],
  opts: { explicit?: string; effective?: string | null; newest?: string | null },
): Map<InstallRow, InstallRoles> {
  const selectedRow = opts.explicit ? rows.find((r) => matchesSpec(r, opts.explicit as string)) : undefined;
  const projectRow = opts.effective ? rows.find((r) => matchesSpec(r, opts.effective as string)) : undefined;
  const newestRow = opts.newest ? rows.find((r) => r.version === opts.newest) : undefined;
  return new Map(rows.map((r) => [r, { selected: r === selectedRow, project: r === projectRow, newest: r === newestRow }]));
}

/** The words after an install row's label (TreeItem.description renders no codicons). */
export function rolesText(roles: InstallRoles | undefined): string {
  if (!roles) {
    return '';
  }
  return [roles.selected ? 'selected' : '', roles.project ? 'project' : '', roles.newest ? 'newest' : ''].filter(Boolean).join(' ');
}

/** The detail rows an install row expands to (a click reveals them; it never changes the setting). */
export function installDetails(row: InstallRow): Array<[string, string]> {
  const out: Array<[string, string]> = [
    ['version', row.version],
    ['root', row.root],
    ['kicad-cli', row.kicad_cli],
    ['source', row.source],
  ];
  if (row.python) {
    out.push(['python', row.python]);
  }
  const f = installFormats(row);
  if (Object.keys(f).length) {
    out.push(['writes formats', Object.entries(f).map(([k, v]) => `${k} ${v}`).join(', ')]);
  }
  const caps = Object.entries(row.capabilities ?? {})
    .filter(([, v]) => v === true)
    .map(([k]) => k);
  if (caps.length) {
    out.push(['capabilities', caps.join(' ')]);
  }
  if (row.capabilities_error) {
    out.push(['capabilities error', row.capabilities_error.message]);
  }
  return out;
}

/** The first entry of the Select KiCad picker (it writes an empty kiwrap.kicadInstall). */
export const MATCH_PROJECT_LABEL = "match the project's file format (recommended)";

/**
 * The install spec a `kiwrap.selectInstall` argument names: a string, or the tree's install node
 * (the inline "Select" action and the context menu pass the element, whose `value` is the spec).
 * Undefined = open the picker.
 */
export function selectionArg(arg: unknown): string | undefined {
  if (typeof arg === 'string') {
    return arg;
  }
  if (arg && typeof arg === 'object' && (arg as { kind?: unknown }).kind === 'install') {
    const v = (arg as { value?: unknown }).value;
    return typeof v === 'string' ? v : undefined;
  }
  return undefined;
}
