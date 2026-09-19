import * as esbuild from 'esbuild';
const watch = process.argv.includes('--watch');
const ctx = await esbuild.context({
  entryPoints: ['src/extension.ts'], bundle: true, platform: 'node', target: 'node20',
  format: 'cjs', outfile: 'dist/extension.js', external: ['vscode','bufferutil','utf-8-validate'], sourcemap: true, logLevel: 'info',
});
if (watch) await ctx.watch(); else { await ctx.rebuild(); await ctx.dispose(); }
