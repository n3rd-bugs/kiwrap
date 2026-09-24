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

  // D21 / D23 scenarios, chosen by a marker in the project path (review item 1: a KiCad 10.0
  // project with only 10.99 installed):
  //   oldfmt       writes need an upgrade until kw_project(action='upgrade', confirm=true) ran
  //   oldstuck     still asks after the upgrade (the retry must not ask twice)
  //   upgradefail  the upgrade itself fails
  //   newerfmt     files newer than every install (E_FORMAT_NEWER)
  //   guardfmt     the stamp guard rolled back a write on 10.99 although 10.0.6 writes the format;
  //                passes once kw_project(action='retarget', target_kicad='10.0.6') pinned it
  //   newerpin     newer than the pinned 10.0.6, but the installed 10.99 reads them (E_FORMAT_NEWER + candidates)
  //   readonly     the server is read-only (E_READONLY)
  const calls: Json[] = [];
  const upgraded = new Set<string>();
  const pinned = new Set<string>();
  const keyOf = (p: string | undefined) => (p ?? '').replace(/\\/g, '/').replace(/\/[^/]+\.kicad_pro$/i, '').toLowerCase();
  const scenario = (p: string | undefined): string => {
    const m = /(oldfmt|oldstuck|upgradefail|newerfmt|newerpin|guardfmt|readonly)/.exec(p ?? '');
    return m ? m[1] : '';
  };
  const formatGate = (tool: string, args: Json) => {
    calls.push({ tool, args });
    const p = args.project as string | undefined;
    const sc = scenario(p);
    if ((sc === 'oldfmt' && !upgraded.has(keyOf(p))) || sc === 'oldstuck' || sc === 'upgradefail') {
      return teachingError('E_FORMAT_UPGRADE_REQUIRED', `${tool} would save ${p} in KiCad 10.99 format; no installed KiCad writes format 20260206`, {
        why: 'D21: kiwrap never writes a newer file format without the user\'s consent',
        fix: "ask the user; kw_project(action='upgrade', confirm=true) or install the matching KiCad",
        data: {
          files: [
            { path: 'C:/fake/oldfmt/oldfmt.kicad_pcb', kind: 'pcb', stamp: 20260206 },
            { path: 'C:/fake/oldfmt/oldfmt.kicad_sch', kind: 'sch', stamp: 20260206 },
          ],
          project_epoch: { pcb: 20260206, sch: 20260206 },
          candidates: [{ version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } }],
          upgrade_to: '10.99.0',
          install_hint: 'winget install KiCad.KiCad --version 10.0.6',
        },
      });
    }
    if (sc === 'newerfmt') {
      return teachingError('E_FORMAT_NEWER', `${p} is in format 20270101, newer than every installed KiCad`, {
        fix: 'install KiCad 11.0 or newer',
        data: {
          files: [{ path: 'C:/fake/newerfmt/newerfmt.kicad_pcb', kind: 'pcb', stamp: 20270101 }],
          project_epoch: { pcb: 20270101 },
          candidates: [
            { version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } },
            { version: '10.0.6', formats: { sch: 20260206, pcb: 20260206 } },
          ],
          needed_kicad: '11.0',
        },
      });
    }
    if (sc === 'guardfmt' && !pinned.has(keyOf(p))) {
      return teachingError('E_FORMAT_UPGRADE_REQUIRED', `${p} is in KiCad 10.0 format; saving it with KiCad 10.99.0 would upgrade the files`, {
        why: "a write raised the files' format stamp without the user's consent; the call was rolled back (D21)",
        candidates: ['10.0.6'],
        data: {
          project: p,
          files: [{ path: 'guardfmt.kicad_pcb', kind: 'pcb', stamp: 20260206 }],
          project_epoch: { pcb: 20260206 },
          project_kicad: '10.0',
          candidates: [
            { version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } },
            { version: '10.0.6', formats: { sch: 20260206, pcb: 20260206 } },
          ],
          upgrade_to: '10.99.0',
          install_hint: 'winget install KiCad.KiCad --version 10.0.6',
          guard: { files: [{ path: 'guardfmt.kicad_pcb', stamp_before: 20260206, stamp_after: 20260901 }], restored: true },
        },
      });
    }
    if (sc === 'newerpin') {
      // pinned to 10.0.6, but the files were saved by 10.99: the installed 10.99 reads them
      return teachingError('E_FORMAT_NEWER', `${p}'s files (pcb 20260901) are newer than KiCad 10.0.6 (pcb 20260206)`, {
        candidates: ['10.99.0'],
        data: {
          project: p,
          files: [{ path: 'C:/fake/newerpin/newerpin.kicad_pcb', kind: 'pcb', stamp: 20260901 }],
          project_epoch: { pcb: 20260901 },
          needed_kicad: '10.99',
          candidates: [
            { version: '10.99.0', formats: { sch: 20260830, pcb: 20260901 } },
            { version: '10.0.6', formats: { sch: 20260206, pcb: 20260206 } },
          ],
          kicad: '10.0.6',
          target_source: 'pinned',
        },
      });
    }
    if (sc === 'readonly' && (tool === 'kw_live' || args.save_board === true)) {
      return teachingError('E_READONLY', `${tool} would write ${p} but the server runs with --readonly`, {
        fix: 'restart kiwrap serve without --readonly (or unset KIWRAP_READONLY)',
      });
    }
    return null;
  };

  server.registerTool('kw_env', { description: 'fake env', inputSchema: { refresh: z.boolean().optional() } }, async () =>
    receipt('kw_env', FAKE_ENV, { provider_used: 'discovery', next_suggested: 'kw_project' }),
  );

  server.registerTool(
    'kw_project',
    {
      description: 'fake project',
      inputSchema: { action: z.enum(['create', 'status', 'upgrade', 'retarget']), project: project.optional(), dir: z.string().optional(), name: z.string().optional(), target_kicad: z.string().optional(), board_size_mm: z.array(z.number()).optional(), to_kicad: z.string().optional(), confirm: z.boolean().optional() },
    },
    async (args) => {
      calls.push({ tool: 'kw_project', args });
      if (args.action === 'status') {
        if (!args.project) {
          return teachingError('E_BAD_ARGS', 'status needs `project`', { example: 'kw_project(action="status", project="C:/work/blinky")' });
        }
        const old = scenario(args.project) === 'oldfmt' && !upgraded.has(keyOf(args.project));
        return receipt(
          'kw_project',
          {
            ok: true,
            project: args.project,
            dir: 'C:/fake',
            name: 'fake',
            stage: 'sch_written',
            next_suggested: 'kw_sch_check',
            target_kicad: old ? null : '10.99.0',
            target_installed: true,
            hashes: {},
            epochs: old ? { sch: 20260206, pcb: 20260206 } : { sch: 20260830, pcb: 20260901 },
            files: { sch: ['fake.kicad_sch'], pcb: ['fake.kicad_pcb'] },
            editor_locks: [],
            snapshot_head: null,
            external_edit: null,
            history: [],
            // D21 status fields (contract (c))
            target_effective: '10.99.0',
            target_source: old ? 'file_epoch_newer_readonly' : 'pinned',
            epoch_match: !old,
            warnings: old ? ['no installed KiCad writes format 20260206; writes need kw_project(action="upgrade", confirm=true)'] : [],
          },
          { stage: 'sch_written', next_suggested: 'kw_sch_check' },
        );
      }
      if (args.action === 'create') {
        return receipt('kw_project', { ok: true, project: `${args.dir}/${args.name}.kicad_pro`, name: args.name, target_kicad: args.target_kicad ?? '10.99.0' });
      }
      if (args.action === 'retarget') {
        if (!args.target_kicad) {
          return teachingError('E_BAD_ARGS', 'retarget needs `target_kicad`');
        }
        pinned.add(keyOf(args.project));
        return receipt('kw_project', { ok: true, project: args.project, target_kicad: args.target_kicad, target_source: 'pinned' });
      }
      // upgrade (D21): the only sanctioned up-conversion, needs confirm=true
      if (!args.confirm) {
        return teachingError('E_BAD_ARGS', 'upgrade needs confirm=true (the user consented)');
      }
      if (scenario(args.project) === 'upgradefail') {
        return teachingError('E_UPGRADE_FAILED', 'kicad-cli pcb upgrade failed (rc 3)', { fix: 'restore the snapshot' });
      }
      upgraded.add(keyOf(args.project));
      return receipt(
        'kw_project',
        { ok: true, project: args.project, upgraded_to: args.to_kicad ?? '10.99.0', files: ['fake.kicad_sch', 'fake.kicad_pcb'] },
        {
          snapshot: 'snap-pre-upgrade',
          validators: [
            { name: 'erc', before: 2, after: 2 },
            { name: 'drc', before: 5, after: 5 },
          ],
        },
      );
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
    async (args) =>
      formatGate('kw_pcb_drc', args) ??
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
        inputSchema: { action: z.enum(['launch', 'attach', 'status', 'screenshot', 'detach', 'raise']), project: z.string().optional(), editor: z.enum(['pcb', 'sch', 'project']).optional(), mode: z.enum(['view', 'hidden', 'auto', 'mirror']).optional(), session_id: z.string().optional(), max_px: z.number().optional(), surface: z.string().optional() },
      },
      async (args) => {
        const gated = args.action === 'launch' ? formatGate('kw_live', args) : null;
        if (gated) {
          return gated;
        }
        const session = { ok: true, session_id: 'live-1', pid: 4242, install: '10.99.0', api_socket: 'ipc://kicad-live-1', token: 'tok', editors: [args.editor ?? 'pcb'], alive: true, mode: args.mode ?? 'view', mode_used: args.mode === 'hidden' ? 'hidden' : 'view', cloaked: args.mode === 'hidden', project: args.project ?? null };
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

  // every call the D21 / D23 scenarios made, in order (the consent tests assert the upgrade call)
  server.registerTool('fake_calls', { description: 'the recorded calls', inputSchema: {} }, async () =>
    ({ content: [{ type: 'text', text: JSON.stringify(calls) }], structuredContent: { ok: true, data: { calls } } }),
  );

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
