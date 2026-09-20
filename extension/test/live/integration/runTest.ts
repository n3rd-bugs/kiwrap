/*
 * Outside runner for the Live KiCad integration suite:
 *   1. builds the harness extension (test/live/harness/build.mjs) into <scratch>/harness,
 *   2. prepares a scratch workspace with a minimal KiCad project and an isolated --user-data-dir whose
 *      settings point kiwrap.live.helperPath at the fake helper,
 *   3. launches the INSTALLED VS Code (Code.exe; KIWRAP_VSCODE_EXE overrides, else @vscode/test-electron
 *      downloads stable) as an Extension Development Host running suite.ts,
 *   4. answers the suite's screenshot requests with capture_window.ps1 (PNG under <scratch>/shots),
 *   5. prints results.json and exits non-zero on failure.
 * Nothing touches the user's VS Code profile or %APPDATA%. Run: node test/live/.out/test/live/integration/runTest.js
 */
import { runTests } from '@vscode/test-electron';
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const EXT = path.resolve(__dirname, '..', '..', '..', '..', '..', '..');   // test/live/.out/test/live/integration -> extension/
const SCRATCH = process.env.KIWRAP_LIVE_SCRATCH ?? path.join(os.tmpdir(), 'kiwrap-live-it');
const NODE = process.env.KIWRAP_NODE ?? 'node';

function findCode(): string | undefined {
  const cands = [process.env.KIWRAP_VSCODE_EXE, path.join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Microsoft VS Code', 'Code.exe'), 'C:\\Program Files\\Microsoft VS Code\\Code.exe'].filter((c): c is string => !!c);
  return cands.find((c) => { try { return fs.statSync(c).isFile(); } catch { return false; } });
}

function writeProject(ws: string): void {
  const dir = path.join(ws, 'ecc83-pp');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'ecc83-pp.kicad_pro'), JSON.stringify({ meta: { filename: 'ecc83-pp.kicad_pro', version: 1 } }, null, 2));
  fs.writeFileSync(path.join(dir, 'ecc83-pp.kicad_pcb'), '(kicad_pcb (version 20240108) (generator "kiwrap-live-test") (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))\n');
  fs.writeFileSync(path.join(dir, 'ecc83-pp.kicad_sch'), '(kicad_sch (version 20231120) (generator "kiwrap-live-test") (uuid "00000000-0000-0000-0000-000000000001") (paper "A4"))\n');
}

async function main(): Promise<number> {
  fs.rmSync(SCRATCH, { recursive: true, force: true });
  fs.mkdirSync(SCRATCH, { recursive: true });
  const harness = path.join(SCRATCH, 'harness');
  const build = spawnSync(NODE, [path.join(EXT, 'test', 'live', 'harness', 'build.mjs'), harness], { stdio: 'inherit', shell: process.platform === 'win32' });
  if (build.status !== 0) { throw new Error('harness build failed'); }

  const ws = path.join(SCRATCH, 'kiwrap-live-ws'); writeProject(ws);
  const udd = path.join(SCRATCH, 'udd'), extDir = path.join(SCRATCH, 'exts'), shots = path.join(SCRATCH, 'shots');
  fs.mkdirSync(path.join(udd, 'User'), { recursive: true }); fs.mkdirSync(extDir, { recursive: true }); fs.mkdirSync(shots, { recursive: true });
  const fakeHelper = path.join(EXT, 'test', 'live', 'fake_helper', 'fake_helper.mjs');
  fs.writeFileSync(path.join(udd, 'User', 'settings.json'), JSON.stringify({
    'kiwrap.live.helperPath': fakeHelper, 'kiwrap.live.mode': 'auto', 'kiwrap.live.hiddenCloseDelayMs': 500, 'kiwrap.live.hud': true,
    'workbench.startupEditor': 'none', 'update.mode': 'none', 'telemetry.telemetryLevel': 'off', 'security.workspace.trust.enabled': false, 'window.zoomLevel': 0, 'extensions.autoUpdate': false,
  }, null, 2));
  const log = path.join(SCRATCH, 'fake-helper.jsonl');

  // screenshot service for the suite
  const capture = path.join(EXT, 'test', 'live', 'integration', 'capture_window.ps1');
  const watcher = setInterval(() => {
    const req = path.join(SCRATCH, 'shot-request.json');
    if (!fs.existsSync(req)) { return; }
    let name = 'shot';
    try { name = (JSON.parse(fs.readFileSync(req, 'utf8')) as { name?: string }).name ?? name; } catch { /* keep default */ }
    fs.rmSync(req, { force: true });
    const out = path.join(shots, `${name}.png`);
    const r = spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', capture, '-Out', out, '-Udd', udd], { encoding: 'utf8' });
    console.log(`[shot] ${r.stdout.trim()} ${r.stderr.trim()}`);
    fs.writeFileSync(path.join(SCRATCH, 'shot-done'), out);
  }, 500);

  const vscodeExecutablePath = findCode();
  console.log(`[runner] vscode: ${vscodeExecutablePath ?? '(download stable)'}  scratch: ${SCRATCH}`);
  let code = 0;
  try {
    code = await runTests({
      vscodeExecutablePath, version: vscodeExecutablePath ? undefined : 'stable',
      extensionDevelopmentPath: harness, extensionTestsPath: path.join(harness, 'suite.js'),
      launchArgs: [ws, '--user-data-dir', udd, '--extensions-dir', extDir, '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--disable-updates', '--new-window'],
      extensionTestsEnv: { KIWRAP_LIVE_SCRATCH: SCRATCH, KIWRAP_FAKE_HELPER_LOG: log, KIWRAP_LIVE_WS: ws, KIWRAP_FAKE_3D_DELAY_MS: '1500', KIWRAP_FAKE_ECHO_CLIPBOARD: 'from-kicad-1', KIWRAP_FAKE_ACTIVE_MS: '4000' },
    });
  } catch (e) { console.error(`[runner] ${String(e)}`); code = 1; }
  clearInterval(watcher);
  const results = path.join(SCRATCH, 'results.json');
  if (fs.existsSync(results)) {
    const rs = JSON.parse(fs.readFileSync(results, 'utf8')) as Array<{ name: string; ok: boolean; ms: number; error?: string }>;
    for (const r of rs) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name} (${r.ms} ms)${r.error ? '\n      ' + r.error.split('\n')[0] : ''}`); }
    console.log(`[runner] ${rs.filter((r) => r.ok).length}/${rs.length} passed; shots: ${fs.readdirSync(shots).join(', ') || 'none'}`);
    if (rs.some((r) => !r.ok)) { code = 1; }
  } else { console.error('[runner] no results.json: the suite did not run'); code = code || 1; }
  return code;
}

main().then((c) => process.exit(c), (e) => { console.error(e); process.exit(1); });
