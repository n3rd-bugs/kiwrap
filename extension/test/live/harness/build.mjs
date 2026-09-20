#!/usr/bin/env node
// Builds the Live KiCad test harness extension into a scratch folder:
//   <out>/package.json     minimal manifest + contributes.live.json (custom editor, commands, settings, keybindings)
//   <out>/extension.js     esbuild bundle of harness/extension.ts (+ src/live/**), vscode external
//   <out>/suite.js         esbuild bundle of integration/suite.ts (the in-host test runner)
//   <out>/media/live/*     the webview client, copied verbatim
// Usage: node build.mjs <out-dir>
import * as esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const ext = path.resolve(here, '..', '..', '..');
const out = path.resolve(process.argv[2] ?? path.join(ext, 'test', 'live', '.out', 'harness'));
fs.mkdirSync(path.join(out, 'media', 'live'), { recursive: true });

const fragment = JSON.parse(fs.readFileSync(path.join(ext, 'src', 'live', 'contributes.live.json'), 'utf8'));
const pkg = {
  name: 'kiwrap-live-harness', displayName: 'kiwrap Live KiCad (test harness)', version: '0.0.1', publisher: 'kiwrap-private', private: true, license: 'MIT',
  engines: { vscode: '^1.101.0' }, main: './extension.js',
  activationEvents: ['onStartupFinished'],
  contributes: fragment,
};
fs.writeFileSync(path.join(out, 'package.json'), JSON.stringify(pkg, null, 2));
for (const f of fs.readdirSync(path.join(ext, 'media', 'live'))) { fs.copyFileSync(path.join(ext, 'media', 'live', f), path.join(out, 'media', 'live', f)); }

const common = { bundle: true, platform: 'node', target: 'node20', format: 'cjs', external: ['vscode'], sourcemap: 'inline', logLevel: 'warning', absWorkingDir: ext };
await esbuild.build({ ...common, entryPoints: [path.join(here, 'extension.ts')], outfile: path.join(out, 'extension.js') });
await esbuild.build({ ...common, entryPoints: [path.join(ext, 'test', 'live', 'integration', 'suite.ts')], outfile: path.join(out, 'suite.js') });
console.log(`harness built at ${out}`);
