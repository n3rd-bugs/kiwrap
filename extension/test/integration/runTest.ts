/**
 * @vscode/test-electron driver: prepares a scratch workspace with a copy of KiCad's ecc83 demo,
 * an isolated user-data-dir and a settings.json pointing the extension at this checkout, then
 * runs test/integration/suite inside an Extension Development Host. Uses the installed VS Code
 * when `code` is on PATH (KIWRAP_VSCODE_EXE overrides), else downloads a stable build.
 *
 *   npm run test:integration
 *
 * Environment: KIWRAP_REPO (default: the parent of extension/), KIWRAP_IT_SCRATCH (default: this
 * session's scratchpad or os.tmpdir()), KIWRAP_KICAD_DEMOS (the ecc83 demo directory itself, or a
 * demos directory holding ecc83/; default: the first install directory under C:/Program Files/KiCad,
 * in reverse lexicographic order - 9.0 before 10.99 on the bench - with share/kicad/demos/ecc83).
 *
 * KIWRAP_IT_SKIP_WITHOUT_KICAD=1 (set by .github/workflows/ci.yml, job `extension`): when no demo is
 * found the run is SKIPPED with a warning annotation and exit code 0 instead of failing. The hosted
 * Windows runner has no KiCad and the suite needs a 10.99 nightly next to 10.0.6 (CI run 36013216332
 * failed here, 2026-09-24). Without the variable a missing demo stays a failure, so a developer
 * machine never passes this suite silently; a demo that IS found always runs the suite, variable or
 * not; and an explicit KIWRAP_KICAD_DEMOS that holds no ecc83 is always an error.
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

/** Where the ecc83 demo comes from, or why the run is skipped. */
export type DemoResolution = { demo: string } | { skip: string };

const DEMO_PRO = 'ecc83-pp.kicad_pro';
export const SKIP_ENV = 'KIWRAP_IT_SKIP_WITHOUT_KICAD';

/**
 * The ecc83 demo directory for the scratch workspace (see the header for the rules). Pure apart from
 * reading the file system, so the CI skip decision can be checked without starting VS Code.
 */
export function resolveDemos(env: NodeJS.ProcessEnv, base = 'C:/Program Files/KiCad'): DemoResolution {
  const explicit = env.KIWRAP_KICAD_DEMOS;
  if (explicit) {
    for (const dir of [explicit, path.join(explicit, 'ecc83')]) {
      if (fs.existsSync(path.join(dir, DEMO_PRO))) {
        return { demo: dir };
      }
    }
    throw new Error(`KIWRAP_KICAD_DEMOS=${explicit} holds no ${DEMO_PRO} (checked it and its ecc83/ subdirectory)`);
  }
  const versions = fs.existsSync(base) ? fs.readdirSync(base).sort().reverse() : [];
  for (const v of versions) {
    const demo = path.join(base, v, 'share', 'kicad', 'demos', 'ecc83');
    if (fs.existsSync(path.join(demo, DEMO_PRO))) {
      return { demo };
    }
  }
  const where = versions.length ? `${base} (${versions.join(', ')})` : `${base} (absent)`;
  if (env[SKIP_ENV] === '1') {
    return {
      skip:
        `no KiCad demo under ${where} and KIWRAP_KICAD_DEMOS is unset; ${SKIP_ENV}=1 so the integration suite ` +
        'is SKIPPED, not passed (it needs the kiwrap server, a KiCad 10.99 nightly and 10.0.6: run ' +
        '`npm run test:integration` on a bench machine)',
    };
  }
  throw new Error(
    `no KiCad demos found under ${where}; set KIWRAP_KICAD_DEMOS to the ecc83 demo directory ` +
      `(or ${SKIP_ENV}=1 to skip the suite explicitly, as CI does on runners without KiCad)`,
  );
}

async function main(): Promise<void> {
  const resolved = resolveDemos(process.env);
  if ('skip' in resolved) {
    // a GitHub Actions warning annotation shows on the run summary; a plain line everywhere else
    const onActions = process.env.GITHUB_ACTIONS === 'true';
    console.log(onActions ? `::warning title=extension integration skipped::${resolved.skip}` : `[it] SKIPPED: ${resolved.skip}`);
    return;
  }
  console.log(`[it] demo: ${resolved.demo}`);
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
  fs.cpSync(resolved.demo, path.join(workspace, 'ecc83'), { recursive: true });
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

if (require.main === module) {
  main().catch((err) => {
    console.error('[it] failed:', err);
    process.exit(1);
  });
}
