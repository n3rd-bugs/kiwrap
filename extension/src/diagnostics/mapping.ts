/**
 * Pure mapping from kw_sch_check / kw_pcb_drc violations to editor diagnostics. No `vscode`
 * import: `diagnostics/manager.ts` converts `MappedDiagnostic` into `vscode.Diagnostic`.
 *
 * Position rule: a violation lands on the line where its first item's uuid occurs in the
 * design file (KiCad writes `(uuid "…")` on the item's own line, so the line is the item);
 * line 1 (index 0) when the uuid is not found. ERC items are searched in every schematic file
 * of the project (the violation's `sheet` is a sheet path, not a file name); DRC items in the
 * board file. Positions in the message are the millimetre positions kiwrap already corrected
 * (CLAUDE.md: the ERC JSON x100 quirk is applied server-side).
 */
import type { Violation, ViolationItem } from '../types';

export type MappedSeverity = 'error' | 'warning' | 'information' | 'hint';

export interface MappedRelated {
  file: string;
  line: number; // 0-based
  message: string;
}

export interface MappedDiagnostic {
  file: string;
  line: number; // 0-based
  severity: MappedSeverity;
  message: string;
  /** KiCad's violation type id (`clearance`, `pin_not_connected`, ...). */
  code: string;
  source: 'kiwrap ERC' | 'kiwrap DRC';
  related: MappedRelated[];
  /** The uuid the diagnostic was anchored to (null when it fell back to line 1). */
  uuid: string | null;
  posMm: { x: number; y: number } | null;
}

/** File text per path; the caller decides how files are read (fs in the extension, strings in tests). */
export type FileTexts = ReadonlyMap<string, string>;

export interface MapOptions {
  kind: 'erc' | 'drc';
  /** Candidate files to search for uuids, in priority order (the root sheet / the board first). */
  files: string[];
  texts: FileTexts;
  /** Where a violation with no locatable uuid goes (default: files[0]). */
  fallbackFile?: string;
}

/** Index of uuid -> {file, line} built lazily per file (one linear pass per file). */
export class UuidIndex {
  private byFile = new Map<string, Map<string, number>>();

  constructor(private texts: FileTexts) {}

  private indexFor(file: string): Map<string, number> {
    let idx = this.byFile.get(file);
    if (!idx) {
      idx = new Map();
      const text = this.texts.get(file);
      if (text) {
        // KiCad uuids: 8-4-4-4-12 hex; `(uuid "…")` (sch/pcb >= 7) or `(uuid …)` / `(tstamp …)` (older)
        const re = /\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b/g;
        let line = 0;
        let lineStart = 0;
        let m: RegExpExecArray | null;
        while ((m = re.exec(text)) !== null) {
          // count newlines between lineStart and m.index
          for (let i = lineStart; i < m.index; i += 1) {
            if (text.charCodeAt(i) === 10) {
              line += 1;
            }
          }
          lineStart = m.index;
          const key = m[1].toLowerCase();
          if (!idx.has(key)) {
            idx.set(key, line);
          }
        }
      }
      this.byFile.set(file, idx);
    }
    return idx;
  }

  find(uuid: string, files: string[]): { file: string; line: number } | null {
    const key = uuid.toLowerCase();
    for (const file of files) {
      const line = this.indexFor(file).get(key);
      if (line !== undefined) {
        return { file, line };
      }
    }
    return null;
  }
}

export function severityOf(v: Violation, kind: 'erc' | 'drc'): MappedSeverity {
  if (kind === 'drc' && v.class === 'cosmetic') {
    return 'information';
  }
  if (v.severity === 'error') {
    return 'error';
  }
  if (v.severity === 'warning') {
    return 'warning';
  }
  if (v.severity === 'exclusion' || v.excluded) {
    return 'hint';
  }
  return 'information';
}

function fmtPos(pos: { x_mm: number; y_mm: number } | null): string {
  return pos ? ` at (${pos.x_mm.toFixed(3)}, ${pos.y_mm.toFixed(3)}) mm` : '';
}

export function messageOf(v: Violation, kind: 'erc' | 'drc'): string {
  const cls = kind === 'drc' && v.class ? `, ${v.class}` : '';
  const first = v.items[0];
  const what = first?.description ? `: ${first.description}` : '';
  const note = v.note ? ` (${v.note})` : '';
  return `${v.description} [${v.type}, ${v.severity}${cls}]${what}${fmtPos(v.pos ?? first?.pos ?? null)}${note}`;
}

function relatedOf(item: ViolationItem, index: UuidIndex, files: string[], fallback: string): MappedRelated {
  const hit = item.uuid ? index.find(item.uuid, files) : null;
  return {
    file: hit?.file ?? fallback,
    line: hit?.line ?? 0,
    message: `${item.description || 'related item'}${fmtPos(item.pos)}`,
  };
}

/** Map every violation to a diagnostic; excluded markers become hints. */
export function mapViolations(violations: Violation[], opts: MapOptions): MappedDiagnostic[] {
  const fallback = opts.fallbackFile ?? opts.files[0];
  const index = new UuidIndex(opts.texts);
  const source: MappedDiagnostic['source'] = opts.kind === 'erc' ? 'kiwrap ERC' : 'kiwrap DRC';
  const out: MappedDiagnostic[] = [];
  for (const v of violations) {
    const first = v.items[0];
    const hit = first?.uuid ? index.find(first.uuid, opts.files) : null;
    const related = v.items.slice(1).map((item) => relatedOf(item, index, opts.files, fallback));
    out.push({
      file: hit?.file ?? fallback,
      line: hit?.line ?? 0,
      severity: severityOf(v, opts.kind),
      message: messageOf(v, opts.kind),
      code: v.type,
      source,
      related,
      uuid: hit ? first.uuid : null,
      posMm: v.pos ? { x: v.pos.x_mm, y: v.pos.y_mm } : first?.pos ? { x: first.pos.x_mm, y: first.pos.y_mm } : null,
    });
  }
  return out;
}

export interface Counts {
  errors: number;
  warnings: number;
  infos: number;
}

export function countBySeverity(diags: MappedDiagnostic[]): Counts {
  const c: Counts = { errors: 0, warnings: 0, infos: 0 };
  for (const d of diags) {
    if (d.severity === 'error') {
      c.errors += 1;
    } else if (d.severity === 'warning') {
      c.warnings += 1;
    } else {
      c.infos += 1;
    }
  }
  return c;
}
