/**
 * A fake kiwrap MCP server over stdio for the unit tests: the tools the extension calls, with
 * canned receipts in the real server's shape (summary line + JSON text block, structuredContent,
 * ImageContent for renders, isError teaching errors), plus `fake_crash` (exits the process) and
 * `fake_sleep` for the restart / timeout paths. kw_live is published here so the wrapper can be
 * exercised before the Python tool exists (contract 2 of the Phase 2 brief).
 *
 * Run as a child process: `node out/test/unit/fakeServer.js [--no-live]`.
 */
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

type Json = Record<string, unknown>;

function receipt(tool: string, data: Json, extra: Json = {}, imageBase64?: string) {
  const structured = {
    ok: (data.ok as boolean | undefined) ?? true,
    changed: { added: [], updated: [], removed: [] },
    validators: [],
    provider_used: 'fake',
    degraded: [],
    snapshot: null,
    stage: null,
    next_suggested: null,
    warnings: [],
    image_info: imageBase64 ? { width: 2, height: 2, bytes: 70, mime_type: 'image/png' } : null,
    data,
    ...extra,
  };
  const content: Array<{ type: 'text'; text: string } | { type: 'image'; data: string; mimeType: string }> = [
    { type: 'text', text: `${tool}: ok\n${JSON.stringify(structured, null, 1)}` },
  ];
  if (imageBase64) {
    content.push({ type: 'image', data: imageBase64, mimeType: 'image/png' });
  }
  return { content, structuredContent: structured };
}

function teachingError(code: string, message: string, extra: Json = {}) {
  const payload = { code, message, why: '', fix: '', candidates: [], example: null, data: {}, ...extra };
  return {
    content: [{ type: 'text' as const, text: `${code}: ${message}\n${JSON.stringify(payload)}` }],
    structuredContent: payload,
    isError: true,
  };
}

// a 2x2 PNG (RGBA), enough for image plumbing
const PNG_2X2 =
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFklEQVQIW2P8z8DwnwEIGIEEAwMDAwAgYQF/JQ2YbwAAAABJRU5ErkJggg==';

export const FAKE_ENV = {
  ok: true,
  kiwrap_version: '0.0.0-fake',
  platform: 'windows',
  installs: [
    {
      version: '10.99.0',
      major_minor: '10.99',
      root: 'C:/Program Files/KiCad/10.99',
      kicad_cli: 'C:/Program Files/KiCad/10.99/bin/kicad-cli.exe',
      share: 'C:/Program Files/KiCad/10.99/share/kicad',
      source: 'registry',
      python: null,
      capabilities: { 'cli.has_erc': true, 'cli.has_drc': true, 'ipc.headless': true, 'kiwrap.patches': [] },
    },
    {
      version: '10.0.6',
      major_minor: '10.0',
      root: 'C:/Program Files/KiCad/10.0',
      kicad_cli: 'C:/Program Files/KiCad/10.0/bin/kicad-cli.exe',
      share: 'C:/Program Files/KiCad/10.0/share/kicad',
      source: 'registry',
      python: 'C:/Program Files/KiCad/10.0/bin/python.exe',
      capabilities: { 'cli.has_erc': true, 'cli.has_drc': true, 'ipc.headless': false, 'kiwrap.patches': [] },
    },
  ],
  default_install: '10.99.0',
  engines: { freerouting: true, jre: true, krt: false, jlcparts: false, autorouter: { available: true } },
  data_backends: { ipc11: true },
  storage: { home: 'C:/fake/home' },
};

export const FAKE_ERC_VIOLATIONS = [
  {
    type: 'pin_not_connected',
    severity: 'error',
    description: 'Pin not connected',
    sheet: '/',
    excluded: false,
    pos: { x_mm: 100.33, y_mm: 50.8 },
    items: [{ uuid: '11111111-2222-3333-4444-555555555555', description: 'Symbol R1 Pin 1', pos: { x_mm: 100.33, y_mm: 50.8 } }],
  },
  {
    type: 'power_pin_not_driven',
    severity: 'warning',
    description: 'Input Power pin not driven by any Output Power pins',
    sheet: '/',
    excluded: false,
    pos: { x_mm: 12.7, y_mm: 25.4 },
    items: [
      { uuid: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', description: 'Symbol U1 Pin 8', pos: { x_mm: 12.7, y_mm: 25.4 } },
      { uuid: 'ffffffff-0000-1111-2222-333333333333', description: 'Symbol #PWR01 Pin 1', pos: { x_mm: 12.7, y_mm: 30.48 } },
    ],
  },
];

export const FAKE_DRC_VIOLATIONS = [
  {
    type: 'clearance',
    severity: 'error',
    description: 'Clearance violation (netclass Default clearance 0.2000 mm; actual 0.1000 mm)',
    sheet: null,
    excluded: false,
    pos: { x_mm: 121.285, y_mm: 136.525 },
    items: [
      { uuid: '9b19456e-3ad1-4dde-92c4-23451559e1ba', description: 'Track [GND] on F.Cu', pos: { x_mm: 121.285, y_mm: 136.525 } },
      { uuid: 'fa4e4619-2310-496a-9c63-4fb385cf27a0', description: 'Pad 1 of R1 on F.Cu', pos: { x_mm: 153.162, y_mm: 125.071 } },
    ],
    class: 'must_fix',
  },
  {
    type: 'silk_edge_clearance',
    severity: 'warning',
    description: 'Silkscreen clipped by board edge',
    sheet: null,
    excluded: false,
    pos: { x_mm: 1, y_mm: 2 },
    items: [{ uuid: 'deadbeef-dead-beef-dead-beefdeadbeef', description: 'Segment on Edge.Cuts', pos: { x_mm: 1, y_mm: 2 } }],
    class: 'cosmetic',
  },
];

/* eslint-disable @typescript-eslint/no-explicit-any */
// registerTool's zod generics make tsc run out of memory on a file with this many tools; the
// fake does not need the inferred argument types, so it goes through a loosely typed facade.
interface LooseServer {
  registerTool(name: string, config: { description?: string; inputSchema?: Record<string, unknown> }, cb: (args: any) => Promise<any>): unknown;
}

export function buildFakeServer(opts: { live?: boolean } = {}): McpServer {
  const mcp = new McpServer({ name: 'kiwrap-fake', version: '0.0.0' });
  const server = mcp as unknown as LooseServer;
  const project = z.string().describe('project directory or .kicad_pro path');

  server.registerTool('kw_env', { description: 'fake env', inputSchema: { refresh: z.boolean().optional() } }, async () =>
    receipt('kw_env', FAKE_ENV, { provider_used: 'discovery', next_suggested: 'kw_project' }),
  );

  server.registerTool(
    'kw_project',
    {
      description: 'fake project',
      inputSchema: { action: z.enum(['create', 'status', 'upgrade']), project: project.optional(), dir: z.string().optional(), name: z.string().optional(), target_kicad: z.string().optional(), board_size_mm: z.array(z.number()).optional() },
    },
    async (args) => {
      if (args.action === 'status') {
        if (!args.project) {
          return teachingError('E_BAD_ARGS', 'status needs `project`', { example: 'kw_project(action="status", project="C:/work/blinky")' });
        }
        return receipt(
          'kw_project',
          {
            ok: true,
            project: args.project,
            dir: 'C:/fake',
            name: 'fake',
            stage: 'sch_written',
            next_suggested: 'kw_sch_check',
            target_kicad: '10.99.0',
            target_installed: true,
            hashes: {},
            epochs: { sch: 20260830, pcb: 20260901 },
            files: { sch: ['fake.kicad_sch'], pcb: ['fake.kicad_pcb'] },
            editor_locks: [],
            snapshot_head: null,
            external_edit: null,
            history: [],
          },
          { stage: 'sch_written', next_suggested: 'kw_sch_check' },
        );
      }
      if (args.action === 'create') {
        return receipt('kw_project', { ok: true, project: `${args.dir}/${args.name}.kicad_pro`, name: args.name, target_kicad: args.target_kicad ?? '10.99.0' });
      }
      return teachingError('E_UNSUPPORTED', 'kw_project upgrade is not available in Phase 0');
    },
  );

  server.registerTool(
    'kw_sch_check',
    { description: 'fake erc', inputSchema: { project, severity: z.string().optional(), netlist: z.boolean().optional() } },
    async (args) => {
      if (args.project.includes('missing')) {
        return teachingError('E_PROJECT_NOT_FOUND', 'root schematic missing.kicad_sch is missing', { fix: 'kw_project create writes it' });
      }
      return receipt(
        'kw_sch_check',
        { ok: false, kicad_version: '10.99.0', errors: 1, warnings: 1, violations: FAKE_ERC_VIOLATIONS, violations_truncated: 0, netlist: null, intent_diff: null },
        { provider_used: 'cli:10.99.0', stage: 'sch_written', next_suggested: 'kw_sch_connect', validators: [{ name: 'erc', before: null, after: 1, extra: { warnings: 1 } }] },
      );
    },
  );

  server.registerTool(
    'kw_pcb_drc',
    { description: 'fake drc', inputSchema: { project, schematic_parity: z.boolean().optional(), refill_zones: z.boolean().nullable().optional(), save_board: z.boolean().nullable().optional(), severity: z.string().optional() } },
    async () =>
      receipt(
        'kw_pcb_drc',
        { ok: false, kicad_version: '10.99.0', errors: 1, warnings: 1, must_fix: 1, review: 0, cosmetic: 1, unconnected: 0, parity_violations: 0, capped: [], violations: FAKE_DRC_VIOLATIONS, violations_truncated: 0, options: { schematic_parity: false, refill_zones: false, save_board: false } },
        { provider_used: 'cli:10.99.0', stage: 'synced', next_suggested: 'kw_pcb_route' },
      ),
  );

  server.registerTool(
    'kw_pcb_check',
    { description: 'fake check', inputSchema: { project, checks: z.array(z.string()).optional(), refs: z.array(z.string()).optional(), nets: z.array(z.string()).optional(), bbox_mm: z.array(z.number()).optional(), cursor: z.string().optional(), limit: z.number().optional(), provider: z.string().optional() } },
    async (args) => receipt('kw_pcb_check', { ok: true, board: 'fake.kicad_pcb', checks: args.checks ?? ['overlaps'], filters: {}, summary: { courtyard_overlap: 0, off_board: 0, unrouted: 3 }, items: [], next_cursor: null }),
  );

  server.registerTool(
    'kw_sch_render',
    { description: 'fake sch render', inputSchema: { project, sheet: z.string().optional(), page: z.number().optional(), max_px: z.number().optional() } },
    async (args) => {
      if ((args.page ?? 1) > 1) {
        return teachingError('E_BAD_ARGS', `page ${args.page} does not exist; the PDF has 1 page(s)`, { candidates: ['1'] });
      }
      return receipt('kw_sch_render', { ok: true, mode: '2d', width: 2, height: 2, bytes: 70, png: 'C:/fake/sch.png', sheet: args.sheet ?? 'fake.kicad_sch', page: 1 }, {}, PNG_2X2);
    },
  );

  server.registerTool(
    'kw_pcb_render',
    { description: 'fake pcb render', inputSchema: { project, view: z.string().optional(), side: z.string().optional(), layers: z.array(z.string()).optional(), max_px: z.number().optional() } },
    async (args) => {
      if (args.view === '2d' && args.layers?.includes('svg-only')) {
        return receipt('kw_pcb_render', { ok: true, mode: 'svg', files: ['C:/fake/board.svg'], png: null, layers: args.layers, side: args.side ?? 'top' }, { degraded: ['render_unavailable'] });
      }
      return receipt('kw_pcb_render', { ok: true, mode: args.view ?? '3d', width: 2, height: 2, bytes: 70, png: 'C:/fake/board.png', side: args.side ?? 'top' }, {}, PNG_2X2);
    },
  );

  if (opts.live !== false) {
    server.registerTool(
      'kw_live',
      {
        description: 'fake live',
        inputSchema: { action: z.enum(['launch', 'attach', 'status', 'screenshot', 'detach', 'raise']), project: z.string().optional(), editor: z.enum(['pcb', 'sch', 'project']).optional(), mode: z.enum(['auto', 'hidden', 'mirror']).optional(), session_id: z.string().optional(), max_px: z.number().optional(), surface: z.string().optional() },
      },
      async (args) => {
        const session = { ok: true, session_id: 'live-1', pid: 4242, install: '10.99.0', api_socket: 'ipc://kicad-live-1', token: 'tok', editors: [args.editor ?? 'pcb'], alive: true, mode: args.mode === 'mirror' ? 'mirror' : 'hidden', project: args.project ?? null };
        if (args.action === 'screenshot') {
          return receipt('kw_live', { ok: true, width: 2, height: 2, bytes: 70 }, {}, PNG_2X2);
        }
        if (args.action === 'detach') {
          return receipt('kw_live', { ok: true, session_id: args.session_id ?? 'live-1', pid: 4242, install: '10.99.0', api_socket: null, token: null, editors: [], alive: false });
        }
        return receipt('kw_live', session);
      },
    );
  }

  server.registerTool('fake_crash', { description: 'exit the process', inputSchema: {} }, async () => {
    setTimeout(() => process.exit(3), 10);
    return { content: [{ type: 'text', text: 'bye' }] };
  });
  server.registerTool('fake_sleep', { description: 'sleep ms', inputSchema: { ms: z.number() } }, async (args) => {
    await new Promise((r) => setTimeout(r, args.ms));
    return { content: [{ type: 'text', text: 'slept' }], structuredContent: { ok: true, data: { slept: args.ms } } };
  });
  return mcp;
}

if (require.main === module) {
  const live = !process.argv.includes('--no-live');
  process.stderr.write('fake kiwrap server starting\n');
  const server = buildFakeServer({ live });
  void server.connect(new StdioServerTransport());
}
