/**
 * "kiwrap: Configure Claude Code" - the pure part. Computes the file edits that register the
 * kiwrap MCP server for Claude Code in a workspace (docs/ARCHITECTURE.md §9 "Registration",
 * D13/D18: Claude Code inside VS Code is the client):
 *
 *   <workspace>/.mcp.json                       mcpServers.kiwrap = uv run --project <kiwrap> kiwrap serve
 *   <workspace>/CLAUDE.md (+ AGENTS.md if any)  a managed block between kiwrap:begin / kiwrap:end markers
 *   <workspace>/.claude/skills/kiwrap-flow/SKILL.md   a copy of the kiwrap-flow skill
 *
 * Nothing is written here; `commands/configureClaude.ts` previews the plan as diffs and applies it.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export interface PlannedEdit {
  /** Absolute path of the file. */
  file: string;
  before: string | null; // null = the file does not exist
  after: string;
  reason: string;
}

export interface ConfigurePlanInput {
  workspace: string;
  /** The kiwrap checkout (uv project) that provides `kiwrap serve`. */
  serverProject: string;
  /** The uv executable (kiwrap.uvPath); the bare `uv` (PATH) when empty or absent. */
  uvPath?: string;
  /** Extra env for the server entry in .mcp.json (e.g. KIWRAP_HOME). */
  env?: Record<string, string>;
  /** Read a file or return null when missing (injectable for tests). */
  read?: (file: string) => string | null;
}

export const BEGIN_MARK = '<!-- kiwrap:begin -->';
export const END_MARK = '<!-- kiwrap:end -->';

function readOrNull(file: string): string | null {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
}

function toPosix(p: string): string {
  return p.replace(/\\/g, '/');
}

function samePath(a: string, b: string): boolean {
  return path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
}

/** The uv command for `.mcp.json`: kiwrap.uvPath when it names an executable, else the bare `uv` on PATH. */
export function uvCommand(uvPath: string | undefined): string {
  const p = (uvPath ?? '').trim();
  if (!p || p.toLowerCase() === 'uv' || p.toLowerCase() === 'uv.exe') {
    return 'uv';
  }
  return toPosix(p);
}

/** The `mcpServers.kiwrap` entry for `.mcp.json` (the same uv the extension itself runs: finding 22). */
export function mcpServerEntry(input: ConfigurePlanInput): Record<string, unknown> {
  const project = samePath(input.workspace, input.serverProject)
    ? '${CLAUDE_PROJECT_DIR:-.}'
    : toPosix(input.serverProject);
  return {
    command: uvCommand(input.uvPath),
    args: ['run', '--project', project, 'kiwrap', 'serve'],
    env: input.env ?? {},
  };
}

/** Merge the entry into an existing `.mcp.json` text (other servers are kept; invalid JSON is replaced). */
export function mergeMcpJson(existing: string | null, entry: Record<string, unknown>): string {
  let doc: Record<string, unknown> = {};
  if (existing) {
    try {
      const parsed = JSON.parse(existing);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        doc = parsed as Record<string, unknown>;
      }
    } catch {
      doc = {};
    }
  }
  const servers =
    doc.mcpServers && typeof doc.mcpServers === 'object' && !Array.isArray(doc.mcpServers)
      ? (doc.mcpServers as Record<string, unknown>)
      : {};
  doc.mcpServers = { ...servers, kiwrap: entry };
  return JSON.stringify(doc, null, 2) + '\n';
}

/** The always-on block for CLAUDE.md / AGENTS.md of a KiCad project. */
export function agentSnippet(): string {
  return [
    BEGIN_MARK,
    '## kiwrap (KiCad through MCP)',
    '',
    'This workspace is a KiCad project driven through the `kiwrap` MCP server registered in',
    '`.mcp.json` (`kw_*` tools). KiCad is the judge: validate with `kw_sch_check` (ERC) and',
    '`kw_pcb_drc` (DRC), never with your own geometry; never edit `.kicad_*` files by hand.',
    'Follow the flow and the data-first protocol in `.claude/skills/kiwrap-flow/SKILL.md`',
    '(`kw_env` -> `kw_project` -> `kw_intent` -> schematic tools -> `kw_sch_check` ->',
    '`kw_pcb_setup` / `kw_pcb_sync` / `kw_pcb_place` / `kw_pcb_route_auto` -> `kw_pcb_drc` ->',
    '`kw_fab_export`). Read every Receipt (`validators`, `stage`, `next_suggested`, `data`) before',
    'the next call; positions are millimetres; renders are secondary to `kw_*_get` / `kw_*_check`.',
    'The VS Code extension "KiCad (kiwrap)" shows ERC/DRC in the Problems panel and previews.',
    END_MARK,
  ].join('\n');
}

/** Insert or replace the managed block in a markdown file. */
export function upsertSnippet(existing: string | null, snippet: string = agentSnippet()): string {
  if (existing === null || existing.trim() === '') {
    return `# Project notes for agents\n\n${snippet}\n`;
  }
  const b = existing.indexOf(BEGIN_MARK);
  const e = existing.indexOf(END_MARK);
  if (b >= 0 && e > b) {
    return existing.slice(0, b) + snippet + existing.slice(e + END_MARK.length);
  }
  const sep = existing.endsWith('\n') ? (existing.endsWith('\n\n') ? '' : '\n') : '\n\n';
  return `${existing}${sep}${snippet}\n`;
}

export function skillSource(serverProject: string): string {
  return path.join(serverProject, '.claude', 'skills', 'kiwrap-flow', 'SKILL.md');
}

export function planConfigure(input: ConfigurePlanInput): PlannedEdit[] {
  const read = input.read ?? readOrNull;
  const edits: PlannedEdit[] = [];
  const push = (file: string, after: string, reason: string) => {
    const before = read(file);
    if (before !== after) {
      edits.push({ file, before, after, reason });
    }
  };

  const mcpFile = path.join(input.workspace, '.mcp.json');
  push(mcpFile, mergeMcpJson(read(mcpFile), mcpServerEntry(input)), 'register the kiwrap MCP server for Claude Code');

  const claudeMd = path.join(input.workspace, 'CLAUDE.md');
  push(claudeMd, upsertSnippet(read(claudeMd)), 'always-on kiwrap rules for Claude Code');
  const agentsMd = path.join(input.workspace, 'AGENTS.md');
  const agentsExisting = read(agentsMd);
  if (agentsExisting !== null) {
    push(agentsMd, upsertSnippet(agentsExisting), 'always-on kiwrap rules (AGENTS.md present)');
  }

  if (!samePath(input.workspace, input.serverProject)) {
    const src = read(skillSource(input.serverProject));
    if (src !== null) {
      const dst = path.join(input.workspace, '.claude', 'skills', 'kiwrap-flow', 'SKILL.md');
      push(dst, src, 'copy the kiwrap-flow skill');
    }
  }
  return edits;
}

/** Write the planned edits (creating directories). */
export function applyEdits(edits: PlannedEdit[]): void {
  for (const e of edits) {
    fs.mkdirSync(path.dirname(e.file), { recursive: true });
    fs.writeFileSync(e.file, e.after, 'utf8');
  }
}
