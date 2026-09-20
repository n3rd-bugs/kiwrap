/**
 * Self-write guard for the on-save checks (kiwrap.checkOnSave). A `kw_pcb_drc` run refills the
 * zones and, when it saves, rewrites the board; the `**\/*.kicad_pcb` watcher then sees a change
 * and would schedule the next DRC, which rewrites the board again - an endless loop of kicad-cli
 * runs (reviewer finding 1, 2026-09-20: 22 runs in 60 s after one "Run DRC"). The manager records
 * the stamp (mtime + size) of every file a run may have written right after the run; a watcher
 * event whose file still carries that stamp is the run's own write and is ignored.
 *
 * Pure (no `vscode` import) so the unit tests cover it; `statOf` is injectable.
 */
import * as fs from 'node:fs';

export interface FileStamp {
  mtimeMs: number;
  size: number;
}

export function statStamp(file: string): FileStamp | null {
  try {
    const st = fs.statSync(file);
    return { mtimeMs: st.mtimeMs, size: st.size };
  } catch {
    return null;
  }
}

function key(file: string): string {
  return file.replace(/\//g, '\\').toLowerCase();
}

export class SelfWriteGuard {
  private readonly stamps = new Map<string, FileStamp>();

  constructor(private readonly statOf: (file: string) => FileStamp | null = statStamp) {}

  /** Record the files as they are NOW (right after one of our own runs finished). */
  record(files: Iterable<string>): void {
    for (const f of files) {
      const st = this.statOf(f);
      if (st) {
        this.stamps.set(key(f), st);
      } else {
        this.stamps.delete(key(f));
      }
    }
  }

  /** True when the file on disk still matches the stamp recorded after our last run on it. */
  isSelfWrite(file: string): boolean {
    const recorded = this.stamps.get(key(file));
    if (!recorded) {
      return false;
    }
    const now = this.statOf(file);
    return !!now && now.mtimeMs === recorded.mtimeMs && now.size === recorded.size;
  }

  forget(file: string): void {
    this.stamps.delete(key(file));
  }

  get size(): number {
    return this.stamps.size;
  }
}
