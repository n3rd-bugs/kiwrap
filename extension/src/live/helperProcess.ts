/*
 * Owner of one `kiwrap-live.exe` process (the Rust WGC capture helper).
 *
 *   kiwrap-live.exe --pid <kicad pid> --listen 127.0.0.1:0 [--inject none|mirror|events|hybrid] [--cloak-expected]
 *                   [--fit-to-view] [--allow-any-process]
 *
 * prints one JSON line `{port, token, version}` on stdout, then serves /control and /stream on that
 * loopback port. The helper mints its own token (18 random bytes) and hands it over in that line:
 * it is never passed on the command line, which every local process can read (reviewer finding
 * 11; `--token` stays available through `extraArgs` for tests). This class spawns it, parses that
 * line, restarts it (bounded backoff) when it exits while the session is still wanted, and kills
 * it on dispose. No `vscode` import: unit-testable.
 */
import { spawn, type ChildProcess } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import type { HelperHello, InjectMode } from '../shared/types';

export interface HelperLaunchSpec {
  /** Path of kiwrap-live.exe, or of a .js/.mjs script that speaks the same protocol (tests). */
  helperPath: string;
  pid: number;
  inject: InjectMode;
  cloakExpected: boolean;
  /** Size the captured frame to the tab's viewport (the helper's --fit-to-view). */
  fitToView?: boolean;
  /** Attach to a pid that is not a KiCad editor (tests attach to the extension host itself). */
  allowAnyProcess?: boolean;
  /** For .js helpers: the node executable; defaults to `process.execPath` with ELECTRON_RUN_AS_NODE=1. */
  nodePath?: string;
  extraArgs?: string[];
  env?: NodeJS.ProcessEnv;
}

export interface HelperEvents {
  onHello?(hello: HelperHello): void;
  onExit?(code: number | null, signal: NodeJS.Signals | null, willRestart: boolean): void;
  onLog?(line: string): void;
}

/** Parse the first stdout line into a HelperHello; throws on anything else. */
export function parseHelloLine(line: string): HelperHello {
  let m: unknown;
  try { m = JSON.parse(line); } catch { throw new Error(`helper stdout is not JSON: ${line.slice(0, 200)}`); }
  if (!m || typeof m !== 'object') { throw new Error('helper hello is not an object'); }
  const o = m as Record<string, unknown>;
  const port = Number(o.port);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) { throw new Error(`helper hello has no valid port: ${line}`); }
  if (typeof o.token !== 'string' || !o.token) { throw new Error('helper hello has no token'); }
  return { port, token: o.token, version: typeof o.version === 'string' ? o.version : String(o.version ?? '') };
}

/** Command-line for the helper, mirroring the contract exactly (order matters for a golden test). */
export function helperArgs(spec: Pick<HelperLaunchSpec, 'pid' | 'inject' | 'cloakExpected' | 'fitToView' | 'allowAnyProcess' | 'extraArgs'>): string[] {
  const a = ['--pid', String(spec.pid), '--listen', '127.0.0.1:0', '--inject', spec.inject];
  if (spec.cloakExpected) { a.push('--cloak-expected'); }
  if (spec.fitToView) { a.push('--fit-to-view'); }
  if (spec.allowAnyProcess) { a.push('--allow-any-process'); }
  if (spec.extraArgs?.length) { a.push(...spec.extraArgs); }
  return a;
}

export interface HelperResolution {
  path: string | null;
  /** A configured kiwrap.live.helperPath that is not a file (the bundled helper is used instead). */
  ignoredSetting?: string;
}

/** Resolve the helper binary: explicit setting > <extension>/bin/kiwrap-live.exe > repo dev build. */
export function resolveHelper(configured: string | undefined, extensionRoot: string): HelperResolution {
  const isFile = (c: string) => { try { return fs.statSync(c).isFile(); } catch { return false; } };
  const out: HelperResolution = { path: null };
  const setting = configured?.trim() ?? '';
  if (setting) {
    if (isFile(setting)) { return { path: setting }; }
    out.ignoredSetting = setting;
  }
  for (const c of [
    path.join(extensionRoot, 'bin', 'kiwrap-live.exe'),
    path.join(extensionRoot, '..', 'helper', 'target', 'release', 'kiwrap-live.exe'),
    path.join(extensionRoot, '..', 'helper', 'target', 'debug', 'kiwrap-live.exe'),
  ]) {
    if (isFile(c)) { out.path = c; break; }
  }
  return out;
}

/** The resolved helper path only (kept for callers that do not report the ignored setting). */
export function resolveHelperPath(configured: string | undefined, extensionRoot: string): string | null {
  return resolveHelper(configured, extensionRoot).path;
}

export class HelperProcess {
  private child: ChildProcess | null = null;
  private stopped = false;
  private helloResolved = false;
  private stdoutBuf = '';
  private restartTimer: NodeJS.Timeout | null = null;
  public restarts = 0;
  public hello: HelperHello | null = null;
  public readonly maxRestarts = 5;

  constructor(private readonly spec: HelperLaunchSpec, private readonly events: HelperEvents = {}) {}

  get pid(): number | null { return this.child?.pid ?? null; }
  get running(): boolean { return !!this.child && this.child.exitCode === null && !this.child.killed; }

  /** Spawn and wait for the hello line (rejects if the process exits or stays silent for `timeoutMs`). */
  start(timeoutMs = 10_000): Promise<HelperHello> {
    this.stopped = false;
    return this.spawnOnce(timeoutMs);
  }

  private spawnOnce(timeoutMs: number): Promise<HelperHello> {
    return new Promise<HelperHello>((resolve, reject) => {
      const { cmd, args } = this.command();
      let child: ChildProcess;
      try {
        child = spawn(cmd, args, {
          stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
          env: { ...process.env, ...(this.spec.env ?? {}), ...(this.isScript() ? { ELECTRON_RUN_AS_NODE: '1' } : {}) },
        });
      } catch (e) { reject(e instanceof Error ? e : new Error(String(e))); return; }
      this.child = child;
      this.helloResolved = false;
      this.stdoutBuf = '';
      const timer = setTimeout(() => { if (!this.helloResolved) { this.helloResolved = true; reject(new Error(`helper printed no hello line within ${timeoutMs} ms`)); this.kill(); } }, timeoutMs);
      child.stdout?.setEncoding('utf8');
      child.stdout?.on('data', (chunk: string) => {
        this.stdoutBuf += chunk;
        let nl: number;
        while ((nl = this.stdoutBuf.indexOf('\n')) >= 0) {
          const line = this.stdoutBuf.slice(0, nl).trim();
          this.stdoutBuf = this.stdoutBuf.slice(nl + 1);
          if (!line) { continue; }
          if (!this.helloResolved) {
            try {
              const hello = parseHelloLine(line);
              this.hello = hello; this.helloResolved = true; clearTimeout(timer);
              this.events.onHello?.(hello);
              resolve(hello);
            } catch (e) { this.events.onLog?.(`[stdout] ${line}`); if (line.startsWith('{')) { this.helloResolved = true; clearTimeout(timer); reject(e as Error); this.kill(); } }
          } else { this.events.onLog?.(`[stdout] ${line}`); }
        }
      });
      child.stderr?.setEncoding('utf8');
      child.stderr?.on('data', (chunk: string) => { for (const l of chunk.split(/\r?\n/)) { if (l.trim()) { this.events.onLog?.(`[stderr] ${l}`); } } });
      child.on('error', (err) => { if (!this.helloResolved) { this.helloResolved = true; clearTimeout(timer); reject(err); } else { this.events.onLog?.(`[error] ${String(err)}`); } });
      child.on('exit', (code, signal) => {
        if (this.child === child) { this.child = null; }
        const willRestart = !this.stopped && this.helloResolved && this.hello !== null && this.restarts < this.maxRestarts;
        if (!this.helloResolved) { this.helloResolved = true; clearTimeout(timer); reject(new Error(`helper exited before hello (code ${code}, signal ${signal})`)); }
        this.events.onExit?.(code, signal, willRestart);
        if (willRestart) {
          const delay = Math.min(5000, 500 * 2 ** this.restarts);
          this.restarts++;
          this.restartTimer = setTimeout(() => {
            this.restartTimer = null;
            if (this.stopped) { return; }
            this.spawnOnce(timeoutMs).catch((e) => this.events.onLog?.(`[restart failed] ${String(e)}`));
          }, delay);
        }
      });
    });
  }

  private isScript(): boolean { return /\.(c|m)?js$/i.test(this.spec.helperPath); }

  private command(): { cmd: string; args: string[] } {
    const args = helperArgs(this.spec);
    if (this.isScript()) { return { cmd: this.spec.nodePath ?? process.execPath, args: [this.spec.helperPath, ...args] }; }
    return { cmd: this.spec.helperPath, args };
  }

  /** Stop and never restart. */
  dispose(): void {
    this.stopped = true;
    if (this.restartTimer) { clearTimeout(this.restartTimer); this.restartTimer = null; }
    this.kill();
  }

  /**
   * Graceful first: closing the helper's stdin is its shutdown signal (kiwrap-live watches for
   * EOF on a piped stdin and stops its captures and hooks in order: main.rs `watch_stdin_eof`);
   * TerminateProcess after `graceMs` if it is still there, taskkill /T last.
   */
  private kill(graceMs = 1200): void {
    const c = this.child;
    if (!c) { return; }
    this.child = null;
    try { c.stdin?.end(); } catch { /* ignore */ }
    setTimeout(() => { if (c.exitCode === null && c.signalCode === null) { try { c.kill(); } catch { /* ignore */ } } }, graceMs).unref();
    // Windows: `kill()` is TerminateProcess; make sure a wedged helper still dies with its children.
    if (process.platform === 'win32' && c.pid) {
      setTimeout(() => { if (c.exitCode === null && c.signalCode === null) { try { spawn('taskkill', ['/PID', String(c.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); } catch { /* ignore */ } } }, graceMs + 1500).unref();
    }
  }
}
