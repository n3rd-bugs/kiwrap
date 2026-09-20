/**
 * @vscode/test-electron driver: prepares a scratch workspace with a copy of KiCad's ecc83 demo,
 * an isolated user-data-dir and a settings.json pointing the extension at this checkout, then
 * runs test/integration/suite inside an Extension Development Host. Uses the installed VS Code
 * when `code` is on PATH (KIWRAP_VSCODE_EXE overrides), else downloads a stable build.
 *
 *   npm run test:integration
 *
 * Environment: KIWRAP_REPO (default: the parent of extension/), KIWRAP_IT_SCRATCH (default: this
 * session's scratchpad or os.tmpdir()), KIWRAP_KICAD_DEMOS (default: the newest KiCad under
 * C:/Program Files/KiCad with share/kicad/demos/ecc83).
 */
import { runTests } from '@vscode/test-electron';
import { execSync } from 'node:child_process';
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
    // <install>/bin/code.cmd -> <install>/Code.exe
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
  const scratchRoot = path.resolve(process.env.KIWRAP_IT_SCRATCH ?? path.join(os.tmpdir(), 'kiwrap-extension-it'));
  const workspace = path.join(scratchRoot, 'workspace');
  const udd = path.join(scratchRoot, 'udd');
  const storage = path.join(scratchRoot, 'storage');
  fs.rmSync(workspace, { recursive: true, force: true });
  fs.rmSync(udd, { recursive: true, force: true });
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(udd, { recursive: true });
  fs.mkdirSync(storage, { recursive: true });
  fs.cpSync(findDemos(), path.join(workspace, 'ecc83'), { recursive: true });
  fs.mkdirSync(path.join(workspace, '.vscode'), { recursive: true });
  fs.writeFileSync(
    path.join(workspace, '.vscode', 'settings.json'),
    JSON.stringify(
      {
        // kiwrap.serverProject is machine-scoped (a workspace cannot set it): KIWRAP_REPO below
        'kiwrap.storageHome': storage,
        'kiwrap.checkOnSave': false,
        'security.workspace.trust.enabled': false,
        'update.mode': 'none',
        'telemetry.telemetryLevel': 'off',
      },
      null,
      2,
    ),
  );
  process.env.KIWRAP_IT_WORKSPACE = workspace;
  process.env.KIWRAP_IT_REPO = repo;

  const vscodeExecutablePath = findVscode();
  console.log(`[it] vscode: ${vscodeExecutablePath ?? '(download stable)'}`);
  console.log(`[it] workspace: ${workspace}`);
  await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath,
    extensionTestsPath: path.resolve(__dirname, 'suite', 'index'),
    launchArgs: [workspace, '--disable-extensions', '--user-data-dir', udd, '--disable-workspace-trust', '--disable-gpu'],
    extensionTestsEnv: { KIWRAP_REPO: repo, KIWRAP_IT_WORKSPACE: workspace, KIWRAP_IT_REPO: repo, ELECTRON_ENABLE_LOGGING: '0' },
  });
}

main().catch((err) => {
  console.error('[it] failed:', err);
  process.exit(1);
});
