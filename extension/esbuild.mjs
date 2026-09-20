// Bundles src/extension.ts (and everything it imports, including @modelcontextprotocol/sdk and
// zod) into dist/extension.js. `vscode` is provided by the host and stays external.
import * as esbuild from 'esbuild';

const watch = process.argv.includes('--watch');
const production = process.argv.includes('--production');

const ctx = await esbuild.context({
  entryPoints: ['src/extension.ts'],
  bundle: true,
  platform: 'node',
  target: 'node20',
  format: 'cjs',
  outfile: 'dist/extension.js',
  external: ['vscode'],
  sourcemap: !production,
  minify: production,
  logLevel: 'info',
  define: { 'process.env.NODE_ENV': production ? '"production"' : '"development"' },
});

if (watch) {
  await ctx.watch();
} else {
  await ctx.rebuild();
  await ctx.dispose();
}
