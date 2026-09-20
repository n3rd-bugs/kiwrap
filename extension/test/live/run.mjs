#!/usr/bin/env node
// One entry point for the Live KiCad tests. From extension/:  node test/live/run.mjs [unit|integration|all]
//   unit         gen_contributes --check, tsc (test/live/tsconfig.json), node --test (webview protocol), mocha (extension host parts)
//   integration  the dev-host suite against the fake helper (needs the installed VS Code; ~30 s; a window appears)
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const ext = path.resolve(here, '..', '..');
const what = process.argv[2] ?? 'unit';
const bin = (n) => path.join(ext, 'node_modules', '.bin', process.platform === 'win32' ? `${n}.cmd` : n);
const run = (cmd, args, label) => {
  console.log(`\n== ${label}`);
  const r = spawnSync(cmd, args, { stdio: 'inherit', cwd: ext, shell: process.platform === 'win32' && cmd.endsWith('.cmd') });
  if (r.status !== 0) { console.error(`${label} failed (${r.status})`); process.exit(r.status ?? 1); }
};
if (what === 'unit' || what === 'all') {
  run(process.execPath, [path.join(ext, 'src', 'live', 'tools', 'gen_contributes.mjs'), '--check'], 'contributes.live.json up to date');
  run(bin('tsc'), ['-p', path.join(ext, 'test', 'live', 'tsconfig.json')], 'tsc');
  run(process.execPath, ['--test', path.join(ext, 'test', 'live', 'unit', 'protocol.test.mjs')], 'node --test (webview protocol)');
  run(bin('mocha'), ['--no-config', '--timeout', '30000', path.join(ext, 'test', 'live', '.out', 'test', 'live', 'unit', 'live.test.js')], 'mocha (extension host parts)');
}
if (what === 'integration' || what === 'all') {
  run(process.execPath, [path.join(ext, 'test', 'live', '.out', 'test', 'live', 'integration', 'runTest.js')], 'integration (dev host + fake helper)');
}
console.log('\nlive tests: ok');
