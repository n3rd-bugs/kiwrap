/** McpClient / KiwrapServer against the fake stdio server (no VS Code, no KiCad). */
import * as assert from 'node:assert/strict';
import * as path from 'node:path';
import { KiwrapServer, buildServerSpec } from '../../src/kiwrapServer';
import { McpClient, findExecutable, parseResult, preflightSpec, type ClientState } from '../../src/mcpClient';
import { KiwrapError } from '../../src/types';

const FAKE = path.join(__dirname, 'fakeServer.js');

function fakeSpec(extra: string[] = []) {
  return { command: process.execPath, args: [FAKE, ...extra], env: { ...(process.env as Record<string, string>) } };
}

describe('McpClient (fake stdio server)', function () {
  this.timeout(30000);
  let stderr: string[];
  let states: ClientState[];
  let server: KiwrapServer;

  beforeEach(async () => {
    stderr = [];
    states = [];
    server = new KiwrapServer(fakeSpec(), {
      onStderr: (l) => stderr.push(l),
      onState: (s) => states.push(s),
      maxRestarts: 2,
    });
    await server.start();
  });

  afterEach(async () => {
    await server.stop();
  });

  it('initializes, lists tools and pipes stderr', () => {
    assert.equal(server.state, 'ready');
    assert.ok(server.pid && server.pid > 0);
    assert.ok(server.hasTool('kw_env'));
    assert.ok(server.hasTool('kw_live'));
    assert.deepEqual(states.slice(0, 2), ['starting', 'ready']);
    assert.ok(stderr.some((l) => l.includes('fake kiwrap server starting')), `stderr: ${stderr.join('|')}`);
  });

  it('kw_env returns a typed receipt', async () => {
    const r = await server.env();
    assert.equal(r.ok, true);
    assert.equal(r.provider_used, 'discovery');
    assert.equal(r.data.default_install, '10.99.0');
    assert.equal(r.data.installs.length, 2);
    assert.equal(r.data.installs[1].version, '10.0.6');
    assert.equal(r.next_suggested, 'kw_project');
  });

  it('kw_project status / kw_sch_check / kw_pcb_drc / kw_pcb_check carry their data', async () => {
    const st = await server.projectStatus('C:/fake/fake.kicad_pro');
    assert.equal(st.data.stage, 'sch_written');
    assert.equal(st.stage, 'sch_written');
    const erc = await server.schCheck('C:/fake/fake.kicad_pro', { severity: 'all' });
    assert.equal(erc.ok, false);
    assert.equal(erc.data.errors, 1);
    assert.equal(erc.data.violations[0].type, 'pin_not_connected');
    assert.equal(erc.validators[0].name, 'erc');
    assert.equal(erc.next_suggested, 'kw_sch_connect');
    const drc = await server.pcbDrc('C:/fake/fake.kicad_pro', { schematic_parity: true });
    assert.equal(drc.data.must_fix, 1);
    assert.equal(drc.data.violations[0].class, 'must_fix');
    const chk = await server.pcbCheck('C:/fake/fake.kicad_pro', { checks: ['unrouted'] });
    assert.deepEqual(chk.data.checks, ['unrouted']);
    assert.equal(chk.data.summary.unrouted, 3);
  });

  it('renders come back with the image block', async () => {
    const sch = await server.schRender('C:/fake/fake.kicad_pro', { page: 1 });
    assert.equal(sch.data.mode, '2d');
    assert.ok(sch.imageBase64 && sch.imageBase64.startsWith('iVBOR'));
    assert.equal(sch.image_info?.mime_type, 'image/png');
    const pcb = await server.pcbRender('C:/fake/fake.kicad_pro', { view: '3d', side: 'bottom' });
    assert.equal(pcb.data.side, 'bottom');
    assert.ok(pcb.imageBase64);
    const svg = await server.pcbRender('C:/fake/fake.kicad_pro', { view: '2d', layers: ['svg-only'] });
    assert.equal(svg.data.mode, 'svg');
    assert.equal(svg.imageBase64, undefined);
    assert.deepEqual(svg.degraded, ['render_unavailable']);
  });

  it('isError results throw KiwrapError with the teaching fields', async () => {
    await assert.rejects(server.schCheck('C:/fake/missing.kicad_pro'), (err: unknown) => {
      assert.ok(err instanceof KiwrapError);
      assert.equal(err.code, 'E_PROJECT_NOT_FOUND');
      assert.equal(err.fix, 'kw_project create writes it');
      return true;
    });
    await assert.rejects(server.schRender('C:/fake/fake.kicad_pro', { page: 3 }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError && err.code === 'E_BAD_ARGS');
      assert.deepEqual(err.candidates, ['1']);
      return true;
    });
    // a schema-level rejection by the SDK is an isError too
    await assert.rejects(server.client.call('kw_project', { action: 'nope' }), (err: unknown) => {
      assert.ok(err instanceof KiwrapError);
      return true;
    });
  });

  it('kw_live launch / status / screenshot / detach through the wrapper', async () => {
    const launch = await server.live({ action: 'launch', project: 'C:/fake/fake.kicad_pro', editor: 'sch', mode: 'view' });
    assert.equal(launch.data.session_id, 'live-1');
    assert.equal(launch.data.mode_used, 'view');
    assert.equal(launch.data.pid, 4242);
    assert.deepEqual(launch.data.editors, ['sch']);
    const status = await server.live({ action: 'status', project: 'C:/fake/fake.kicad_pro', session_id: 'live-1' });
    assert.equal(status.data.alive, true);
    const shot = await server.live({ action: 'screenshot', project: 'C:/fake/fake.kicad_pro', session_id: 'live-1', max_px: 512 });
    assert.ok(shot.imageBase64);
    const detach = await server.live({ action: 'detach', project: 'C:/fake/fake.kicad_pro', session_id: 'live-1' });
    assert.equal(detach.data.alive, false);
  });

  it('a missing tool is E_NO_TOOL without a round trip', async () => {
    const noLive = new KiwrapServer(fakeSpec(['--no-live']));
    await noLive.start();
    try {
      assert.equal(noLive.liveAvailable, false);
      await assert.rejects(noLive.live({ action: 'status', project: 'x' }), (err: unknown) => {
        assert.ok(err instanceof KiwrapError && err.code === 'E_NO_TOOL');
        assert.ok(err.candidates.includes('kw_env'));
        return true;
      });
    } finally {
      await noLive.stop();
    }
  });

  it('restarts the server after a crash and gives up after maxRestarts', async () => {
    const pid1 = server.pid;
    await server.client.call('fake_crash').catch(() => undefined);
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(server.state, 'failed');
    const r = await server.env(); // restart #1
    assert.equal(r.data.default_install, '10.99.0');
    assert.equal(server.state, 'ready');
    assert.notEqual(server.pid, pid1);
    // explicit restart resets the counter
    await server.restart();
    assert.equal(server.state, 'ready');
  });

  it('stops restarting a server that keeps dying at startup', async () => {
    const dying = new McpClient({ command: process.execPath, args: ['-e', 'process.exit(7)'] }, { maxRestarts: 2 });
    await assert.rejects(dying.start(), (err: unknown) => err instanceof KiwrapError && err.code === 'E_CLIENT');
    assert.equal(dying.state, 'failed');
    await assert.rejects(dying.call('kw_env'), /could not start|exited/); // restart 1
    await assert.rejects(dying.call('kw_env'), /could not start|exited/); // restart 2
    await assert.rejects(dying.call('kw_env'), /keeps exiting/); // over the limit: no spawn
    await dying.restart().catch(() => undefined); // explicit restart resets the counter and tries again
    assert.equal(dying.state, 'failed');
    await dying.stop();
  });

  it('an explicit stop then a call restarts on purpose', async () => {
    await server.stop();
    assert.equal(server.state, 'stopped');
    const r = await server.env();
    assert.equal(r.ok, true);
  });

  it('honours the per-call timeout', async () => {
    const quick = new McpClient(fakeSpec(), { callTimeoutMs: 300 });
    await quick.start();
    try {
      await assert.rejects(quick.call('fake_sleep', { ms: 2000 }), (err: unknown) => {
        assert.ok(err instanceof KiwrapError && err.code === 'E_CLIENT');
        assert.match(err.message, /timed out|timeout/i);
        return true;
      });
    } finally {
      await quick.stop();
    }
  });
});

describe('parseResult', () => {
  it('reads a receipt without structuredContent from the text body', () => {
    const body = { ok: true, data: { x: 1 }, provider_used: 'file' };
    const r = parseResult<{ x: number }>('kw_x', { content: [{ type: 'text', text: `kw_x: ok\n${JSON.stringify(body)}` }] });
    assert.equal(r.data.x, 1);
    assert.equal(r.provider_used, 'file');
    assert.deepEqual(r.changed, { added: [], updated: [], removed: [] });
  });
  it('reads an error from the text lines when structuredContent is absent', () => {
    assert.throws(
      () => parseResult('kw_x', { isError: true, content: [{ type: 'text', text: 'E_NO_KICAD: nothing\nfix: install\n{"code":"E_NO_KICAD","message":"nothing","fix":"install"}' }] }),
      (err: unknown) => err instanceof KiwrapError && err.code === 'E_NO_KICAD' && err.fix === 'install',
    );
    assert.throws(
      () => parseResult('kw_x', { isError: true, content: [{ type: 'text', text: 'E_TIMEOUT: too slow' }] }),
      (err: unknown) => err instanceof KiwrapError && err.code === 'E_TIMEOUT',
    );
  });
});

describe('buildServerSpec', () => {
  it('runs kiwrap serve from the checkout with --storage-home', () => {
    const spec = buildServerSpec({ uvPath: 'C:/tools/uv.exe', serverProject: 'C:/src/kiwrap', storageHome: 'C:/home/kw' });
    assert.equal(spec.command, 'C:/tools/uv.exe');
    assert.deepEqual(spec.args, ['run', '--project', 'C:/src/kiwrap', 'kiwrap', '--storage-home', 'C:/home/kw', 'serve']);
    assert.equal(spec.cwd, 'C:/src/kiwrap');
    assert.equal(spec.env?.KIWRAP_HOME, 'C:/home/kw');
    assert.ok(spec.env?.PATH ?? spec.env?.Path, 'inherits the environment');
  });
  it('leaves the storage home to kiwrap when unset (one home shared with the agent)', () => {
    const spec = buildServerSpec({ uvPath: '', serverProject: 'C:/src/kiwrap', storageHome: '' });
    assert.equal(spec.command, 'uv');
    assert.deepEqual(spec.args, ['run', '--project', 'C:/src/kiwrap', 'kiwrap', 'serve']);
    assert.equal(spec.env?.KIWRAP_HOME, process.env.KIWRAP_HOME);
  });
});

describe('launch preflight (finding 19)', () => {
  it('finds executables on PATH (with PATHEXT) and by path', () => {
    assert.equal(findExecutable(process.execPath), process.execPath);
    assert.equal(findExecutable('C:/nope/uv.exe'), null);
    const dir = path.dirname(process.execPath);
    const name = path.basename(process.execPath).replace(/\.exe$/i, '');
    assert.ok(findExecutable(name, { PATH: dir, PATHEXT: '.EXE' })?.toLowerCase().startsWith(dir.toLowerCase()));
    assert.equal(findExecutable('definitely-not-a-command-xyz', { PATH: dir, PATHEXT: '.EXE' }), null);
  });
  it('names a missing uv and a server project without pyproject.toml', () => {
    const missing = preflightSpec({ command: 'C:/nope/uv.exe', args: ['run', '--project', 'C:/src/kiwrap', 'kiwrap', 'serve'] });
    assert.match(missing ?? '', /uv.*not found.*kiwrap\.uvPath/);
    const bare = preflightSpec({ command: 'uv', args: [], env: { PATH: 'C:/nope' } });
    assert.match(bare ?? '', /uv not found on PATH/);
    const noProject = preflightSpec({ command: process.execPath, args: ['run', '--project', 'C:/nope/kiwrap', 'kiwrap', 'serve'] });
    assert.match(noProject ?? '', /has no pyproject\.toml/);
    assert.equal(preflightSpec({ command: process.execPath, args: [FAKE] }), null);
  });
  it('a missing uv is a specific E_CLIENT, not "Connection closed"', async () => {
    const c = new McpClient({ command: 'C:/nope/uv.exe', args: ['run', 'kiwrap', 'serve'] });
    await assert.rejects(c.start(), (err: unknown) => err instanceof KiwrapError && err.code === 'E_CLIENT' && /uv.*not found/.test(err.message) && !/Connection closed/.test(err.message));
    assert.equal(c.state, 'failed');
  });
});
