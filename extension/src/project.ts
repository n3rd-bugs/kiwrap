/**
 * Which KiCad project a file / workspace belongs to. Pure (fs only).
 *
 * kiwrap addresses a project by its `.kicad_pro` path; the root schematic and the board share
 * its stem (`kiwrap.core.state.project.Project`). A directory may hold several projects (the
 * ecc83 demo ships `ecc83-pp` and `ecc83-pp_v2` side by side), so a design file maps to the
 * `.kicad_pro` with the same stem first, then to the only `.kicad_pro` in its directory (a
 * hierarchical sheet), else to nothing.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export interface KicadProject {
  /** Absolute `.kicad_pro` path. */
  pro: string;
  dir: string;
  name: string;
}

export function listProjects(dir: string): KicadProject[] {
  let entries: string[];
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return entries
    .filter((e) => e.toLowerCase().endsWith('.kicad_pro'))
    .sort()
    .map((e) => ({ pro: path.join(dir, e), dir, name: e.slice(0, -'.kicad_pro'.length) }));
}

/** Projects directly in `root` and, when there are none, in its first-level subdirectories. */
export function findProjects(root: string, depth = 2): KicadProject[] {
  const found = listProjects(root);
  if (found.length || depth <= 1) {
    return found;
  }
  let subs: string[] = [];
  try {
    subs = fs
      .readdirSync(root, { withFileTypes: true })
      .filter((d) => d.isDirectory() && !d.name.startsWith('.') && d.name !== 'node_modules')
      .map((d) => path.join(root, d.name));
  } catch {
    return [];
  }
  return subs.flatMap((s) => findProjects(s, depth - 1));
}

export function projectForFile(file: string): KicadProject | undefined {
  const dir = path.dirname(file);
  const stem = path.basename(file).replace(/\.(kicad_sch|kicad_pcb|kicad_pro)$/i, '');
  const projects = listProjects(dir);
  const same = projects.find((p) => p.name.toLowerCase() === stem.toLowerCase());
  if (same) {
    return same;
  }
  if (projects.length === 1) {
    return projects[0];
  }
  return undefined;
}

export function isSchematic(file: string): boolean {
  return file.toLowerCase().endsWith('.kicad_sch');
}

export function isBoard(file: string): boolean {
  return file.toLowerCase().endsWith('.kicad_pcb');
}

export function rootSchematic(p: KicadProject): string {
  return path.join(p.dir, `${p.name}.kicad_sch`);
}

export function boardFile(p: KicadProject): string {
  return path.join(p.dir, `${p.name}.kicad_pcb`);
}

/** Every `.kicad_sch` in the project directory (root first). */
export function schematicFiles(p: KicadProject): string[] {
  const root = rootSchematic(p);
  let entries: string[] = [];
  try {
    entries = fs.readdirSync(p.dir);
  } catch {
    /* empty */
  }
  const others = entries
    .filter((e) => e.toLowerCase().endsWith('.kicad_sch'))
    .map((e) => path.join(p.dir, e))
    .filter((f) => f.toLowerCase() !== root.toLowerCase())
    .sort();
  return fs.existsSync(root) ? [root, ...others] : others;
}
