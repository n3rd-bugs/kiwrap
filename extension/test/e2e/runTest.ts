/**
 * End-to-end driver (Phase 2 integration, docs/EXTENSION.md): the REAL extension in an Extension
 * Development Host of the installed VS Code, the real `uv run kiwrap serve`, the real Rust helper,
 * real sandboxed KiCad GUIs (stock 10.99 view-only, the display-input bench build hidden), and a
 * second kiwrap MCP client (test/e2e/agent.py) playing the agent. See suite/e2e.test.ts.
 *
 *   npm run test:e2e            (or node out/test/e2e/runTest.js)
 *
 * Environment: KIWRAP_REPO (default: the parent of extension/), KIWRAP_E2E_SCRATCH (default:
 * os.tmpdir()/kiwrap-e2e), KIWRAP_E2E_IMG (where the phase2-*.png evidence goes; default
 * <repo>/docs/research/display/img), KIWRAP_KICAD_DEMOS (a directory holding ecc83), KIWRAP_E2E_STOCK
 * (the stock install spec, default "10.99"), KIWRAP_E2E_HIDDEN (the install root carrying
 * mr/display-input; default: the first kw_env install with gui.input_from_events; "skip" skips).
 *
 * The scratch workspace holds a copy of the ecc83 demo (the v2 project removed) and the Phase 1b
 * blinker board (tests/fixtures/specctra/blinker_10). The extension uses kiwrap's DEFAULT storage
 * home (no --storage-home, no KIWRAP_HOME) because the agent's server from .mcp.json must share it
 * (installs registry, sandbox roots, GUI session records) - the product configuration.
 */
import { runTests } from '@vscode/test-electron';
import { execSync, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

function findVscode(): string | undefined {
  if (process.env.KIWRAP_VSCODE_EXE && fs.existsSync(process.env.KIWRAP_VSCODE_EXE)) {
    return process.env.KIWRAP_VSCODE_EXE;
  }
  try {
    const where = execSync(process.platform === 'win32' ? 'where code' : 'which code', { encoding: 'utf8' })
      .split(/\r?\n/)
      .map((l) => l.trim())
      .find((l) => l && !l.endsWith('.ps1'));
    if (!where) {
      return undefined;
    }
    const candidate = path.join(path.dirname(path.dirname(where)), process.platform === 'win32' ? 'Code.exe' : 'code');
    return fs.existsSync(candidate) ? candidate : undefined;
  } catch {
    return undefined;
  }
}

function findDemos(): string {
  if (process.env.KIWRAP_KICAD_DEMOS) {
    return process.env.KIWRAP_KICAD_DEMOS;
  }
  const base = 'C:/Program Files/KiCad';
  const versions = fs.existsSync(base) ? fs.readdirSync(base).sort().reverse() : [];
  for (const v of versions) {
    const demo = path.join(base, v, 'share', 'kicad', 'demos', 'ecc83');
    if (fs.existsSync(path.join(demo, 'ecc83-pp.kicad_pro'))) {
      return demo;
    }
  }
  throw new Error('no KiCad demos found; set KIWRAP_KICAD_DEMOS to a directory holding ecc83');
}

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, '..', '..', '..');
  const repo = path.resolve(process.env.KIWRAP_REPO ?? path.join(extensionDevelopmentPath, '..'));
  const scratchRoot = path.resolve(process.env.KIWRAP_E2E_SCRATCH ?? path.join(os.tmpdir(), 'kiwrap-e2e'));
  const img = path.resolve(process.env.KIWRAP_E2E_IMG ?? path.join(repo, 'docs', 'research', 'display', 'img'));
  const workspace = path.join(scratchRoot, 'workspace');
  const udd = path.join(scratchRoot, 'udd');
  const out = path.join(scratchRoot, 'out');
  fs.rmSync(workspace, { recursive: true, force: true });
  fs.rmSync(udd, { recursive: true, force: true });
  fs.rmSync(out, { recursive: true, force: true });
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(udd, { recursive: true });
  fs.mkdirSync(out, { recursive: true });
  fs.mkdirSync(img, { recursive: true });
  fs.cpSync(findDemos(), path.join(workspace, 'ecc83'), { recursive: true });
  for (const f of fs.readdirSync(path.join(workspace, 'ecc83'))) {
    if (f.startsWith('ecc83-pp_v2')) {
      fs.rmSync(path.join(workspace, 'ecc83', f), { recursive: true, force: true });
    }
  }
  const blinker = path.join(repo, 'tests', 'fixtures', 'specctra', 'blinker_10');
  if (fs.existsSync(blinker)) {
    fs.cpSync(blinker, path.join(workspace, 'blinker'), { recursive: true });
  }
  fs.mkdirSync(path.join(workspace, '.vscode'), { recursive: true });
  fs.writeFileSync(
    path.join(workspace, '.vscode', 'settings.json'),
    JSON.stringify(
      {
        // kiwrap.serverProject is machine-scoped (finding 14): a workspace cannot set it; the
        // extension finds the checkout through KIWRAP_REPO (extensionTestsEnv below)
        'kiwrap.storageHome': '',
        'kiwrap.checkOnSave': false,
        'kiwrap.live.mode': 'auto',
        'kiwrap.live.hud': true,
        'security.workspace.trust.enabled': false,
        'update.mode': 'none',
        'telemetry.telemetryLevel': 'off',
        'workbench.startupEditor': 'none',
        'window.zoomLevel': 0,
      },
      null,
      2,
    ),
  );
  const env = {
    KIWRAP_REPO: repo,
    KIWRAP_E2E_WORKSPACE: workspace,
    KIWRAP_E2E_REPO: repo,
    KIWRAP_E2E_OUT: out,
    KIWRAP_E2E_IMG: img,
    KIWRAP_E2E_UDD: udd,
    KIWRAP_E2E_STOCK: process.env.KIWRAP_E2E_STOCK ?? (fs.existsSync('C:/Program Files/KiCad/10.99') ? 'C:/Program Files/KiCad/10.99' : '10.99'),
    KIWRAP_E2E_HIDDEN: process.env.KIWRAP_E2E_HIDDEN ?? '',
    ELECTRON_ENABLE_LOGGING: '0',
  };
  const vscodeExecutablePath = findVscode();
  console.log(`[e2e] vscode: ${vscodeExecutablePath ?? '(download stable)'}`);
  console.log(`[e2e] workspace: ${workspace}\n[e2e] evidence: ${img}\n[e2e] out: ${out}`);
  let code = 0;
  try {
    await runTests({
      vscodeExecutablePath,
      extensionDevelopmentPath,
      extensionTestsPath: path.resolve(__dirname, 'suite', 'index'),
      launchArgs: [workspace, '--disable-extensions', '--user-data-dir', udd, '--disable-workspace-trust', '--new-window'],
      extensionTestsEnv: env,
    });
  } catch (err) {
    console.error('[e2e] failed:', err);
    code = 1;
  }
  const report = path.join(out, 'e2e-report.json');
  if (fs.existsSync(report)) {
    console.log(fs.readFileSync(report, 'utf8'));
  }
  // the evidence PNGs must stay small (docs/research/display/img rule: < 300 KB); Pillow is a tool-time dependency
  const shrink = spawnSync('uv', ['run', '--with', 'pillow', 'python', path.join(extensionDevelopmentPath, 'test', 'e2e', 'shrink.py'), img, '--max-kb', '290'], { cwd: repo, encoding: 'utf8', shell: true });
  console.log(`[e2e] shrink: ${(shrink.stdout || '').trim()}${shrink.stderr ? ' | ' + shrink.stderr.trim() : ''}`);
  process.exit(code);
}

main().catch((err) => {
  console.error('[e2e] failed:', err);
  process.exit(1);
});
