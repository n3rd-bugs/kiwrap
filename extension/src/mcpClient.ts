/**
 * A small MCP stdio client over @modelcontextprotocol/sdk, free of `vscode` imports so the unit
 * tests can drive it against a fake server (test/unit/fakeServer.ts).
 *
 * Responsibilities:
 *  - spawn the server command (the SDK's StdioClientTransport owns the child process),
 *    pipe its stderr to a sink (the extension's output channel),
 *  - `initialize` + `tools/list` once per connection, cached (`hasTool`),
 *  - `call()` turns a CallToolResult into a `Receipt` (structuredContent + the image block) or
 *    throws a `KiwrapError` (isError results carry the server's teaching error as
 *    structuredContent; transport failures get a client-side code `E_CLIENT`),
 *  - reconnect: when the transport closes (server crash, killed process) the next call restarts
 *    it, at most `maxRestarts` times in a row without an intervening success; `restart()` does
 *    it explicitly.
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { Stream } from 'node:stream';
import { KiwrapError, type KiwrapErrorShape, type Receipt } from './types';

export interface ServerSpec {
  command: string;
  args: string[];
  cwd?: string;
  env?: Record<string, string>;
}

export type ClientState = 'stopped' | 'starting' | 'ready' | 'failed';

/**
 * Where a bare command name resolves on PATH (Windows: with PATHEXT), or null. A command with a
 * directory part is checked as a file. Pure (fs + env), so the unit tests cover it.
 */
export function findExecutable(command: string, env: Record<string, string | undefined> = process.env): string | null {
  const isFile = (p: string) => { try { return fs.statSync(p).isFile(); } catch { return false; } };
  if (command.includes('/') || command.includes('\\')) {
    return isFile(command) ? command : null;
  }
  const dirs = (env.PATH ?? env.Path ?? '').split(path.delimiter).filter(Boolean);
  const exts = process.platform === 'win32' ? (env.PATHEXT ?? '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean) : [''];
  for (const dir of dirs) {
    for (const ext of [''].concat(exts)) {
      const candidate = path.join(dir, command + ext);
      if (isFile(candidate)) { return candidate; }
    }
  }
  return null;
}

/**
 * The launch problems the SDK would otherwise report as "MCP error -32000: Connection closed"
 * (reviewer finding 19): a uv executable that does not exist and a server project without a
 * pyproject.toml. Returns the message, or null when the spec looks launchable.
 */
export function preflightSpec(spec: ServerSpec, env: Record<string, string | undefined> = process.env): string | null {
  if (!findExecutable(spec.command, spec.env ?? env)) {
    return `${spec.command === 'uv' ? 'uv' : `"${spec.command}"`} not found${spec.command === 'uv' ? ' on PATH' : ''}: install uv (https://docs.astral.sh/uv/) or set kiwrap.uvPath to the executable`;
  }
  const idx = spec.args.indexOf('--project');
  const project = idx >= 0 ? spec.args[idx + 1] : spec.cwd;
  if (project && !fs.existsSync(path.join(project, 'pyproject.toml'))) {
    return `kiwrap.serverProject "${project}" has no pyproject.toml: point it at the kiwrap checkout (the folder with pyproject.toml and src/kiwrap)`;
  }
  return null;
}

export interface McpClientOptions {
  /** Called with every line the server writes to stderr. */
  onStderr?: (line: string) => void;
  /** Called on every state change. */
  onState?: (state: ClientState, detail?: string) => void;
  /** Client name/version announced in `initialize`. */
  clientInfo?: { name: string; version: string };
  /** Restarts attempted automatically after an unexpected close (default 3). */
  maxRestarts?: number;
  /** Per-call timeout in ms (default 10 minutes: DRC/route jobs are slow). */
  callTimeoutMs?: number;
}

interface ToolInfo {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

export class McpClient {
  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;
  private tools = new Map<string, ToolInfo>();
  private _state: ClientState = 'stopped';
  private starting: Promise<void> | null = null;
  private restartsInARow = 0;
  private stopRequested = false;
  private readonly opts: Required<Pick<McpClientOptions, 'maxRestarts' | 'callTimeoutMs' | 'clientInfo'>> &
    McpClientOptions;

  constructor(
    private spec: ServerSpec,
    opts: McpClientOptions = {},
  ) {
    this.opts = {
      maxRestarts: 3,
      callTimeoutMs: 10 * 60 * 1000,
      clientInfo: { name: 'kiwrap-vscode', version: '0.1.0' },
      ...opts,
    };
  }

  get state(): ClientState {
    return this._state;
  }

  get pid(): number | null {
    return this.transport?.pid ?? null;
  }

  get serverSpec(): ServerSpec {
    return this.spec;
  }

  /** Replace the launch spec; takes effect on the next (re)start. */
  setSpec(spec: ServerSpec): void {
    this.spec = spec;
  }

  toolNames(): string[] {
    return [...this.tools.keys()];
  }

  hasTool(name: string): boolean {
    return this.tools.has(name);
  }

  private setState(state: ClientState, detail?: string): void {
    this._state = state;
    this.opts.onState?.(state, detail);
  }

  /** Start (or wait for the in-flight start of) the server. */
  async start(): Promise<void> {
    this.stopRequested = false;
    if (this._state === 'ready' && this.client) {
      return;
    }
    if (this.starting) {
      return this.starting;
    }
    this.starting = this.doStart().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  private async doStart(): Promise<void> {
    this.setState('starting', `${this.spec.command} ${this.spec.args.join(' ')}`);
    const problem = preflightSpec(this.spec);
    if (problem) {
      this.setState('failed', problem);
      throw new KiwrapError({ code: 'E_CLIENT', message: `could not start the kiwrap server: ${problem}`, fix: 'fix the setting, then run "kiwrap: Restart server"' });
    }
    const spawnErrors: string[] = [];
    const transport = new StdioClientTransport({
      command: this.spec.command,
      args: this.spec.args,
      cwd: this.spec.cwd,
      env: this.spec.env ?? (process.env as Record<string, string>),
      stderr: 'pipe',
    });
    const client = new Client(this.opts.clientInfo, { capabilities: {} });
    const closedEarly = { value: false };
    client.onclose = () => {
      closedEarly.value = true;
      if (this.client === client) {
        this.client = null;
        this.transport = null;
        this.tools.clear();
        if (!this.stopRequested) {
          this.setState('failed', 'server process closed');
        } else {
          this.setState('stopped');
        }
      }
    };
    client.onerror = (err: Error) => {
      spawnErrors.push(err.message);
      this.opts.onStderr?.(`[client] ${err.message}`);
    };
    try {
      await client.connect(transport);
    } catch (err) {
      // the transport's own spawn error (ENOENT, EACCES) is what explains a generic "Connection closed"
      const detail = spawnErrors.length ? `${errorText(err)} (${spawnErrors.join('; ')})` : errorText(err);
      this.setState('failed', detail);
      throw new KiwrapError({
        code: 'E_CLIENT',
        message: `could not start the kiwrap server: ${detail}`,
        fix: 'check kiwrap.uvPath / kiwrap.serverProject and the "KiCad (kiwrap)" output channel',
      });
    }
    const stderr = transport.stderr;
    if (stderr) {
      pipeLines(stderr, (line) => this.opts.onStderr?.(line));
    }
    if (closedEarly.value) {
      throw new KiwrapError({ code: 'E_CLIENT', message: 'the kiwrap server exited during initialize' });
    }
    this.client = client;
    this.transport = transport;
    try {
      const listed = await client.listTools();
      this.tools = new Map(listed.tools.map((t) => [t.name, t as ToolInfo]));
    } catch (err) {
      await this.stop();
      throw new KiwrapError({ code: 'E_CLIENT', message: `tools/list failed: ${errorText(err)}` });
    }
    this.setState('ready', `pid ${transport.pid ?? '?'}, ${this.tools.size} tools`);
  }

  async stop(): Promise<void> {
    this.stopRequested = true;
    const client = this.client;
    this.client = null;
    this.transport = null;
    this.tools.clear();
    if (client) {
      try {
        await client.close();
      } catch {
        /* already gone */
      }
    }
    this.setState('stopped');
  }

  async restart(): Promise<void> {
    await this.stop();
    this.restartsInARow = 0;
    await this.start();
  }

  private async ensureConnected(): Promise<Client> {
    if (this.client && this._state === 'ready') {
      return this.client;
    }
    if (this.stopRequested && this._state === 'stopped') {
      // an explicit stop: the next call restarts on purpose
      this.restartsInARow = 0;
    } else if (this._state === 'failed') {
      if (this.restartsInARow >= this.opts.maxRestarts) {
        throw new KiwrapError({
          code: 'E_CLIENT',
          message: `the kiwrap server keeps exiting (${this.restartsInARow} restarts); not restarting again`,
          fix: 'read the "KiCad (kiwrap)" output channel, fix the cause, then run "kiwrap: Restart server"',
        });
      }
      this.restartsInARow += 1;
    }
    await this.start();
    if (!this.client) {
      throw new KiwrapError({ code: 'E_CLIENT', message: 'the kiwrap server is not connected' });
    }
    return this.client;
  }

  /**
   * Call a tool and return its Receipt. isError results throw `KiwrapError` with the server's
   * structured teaching error; a tool the server does not publish throws `E_NO_TOOL` without a
   * round trip.
   */
  async call<T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<Receipt<T>> {
    const client = await this.ensureConnected();
    if (!this.tools.has(name)) {
      throw new KiwrapError({
        code: 'E_NO_TOOL',
        message: `the kiwrap server does not publish ${name}`,
        fix: 'update kiwrap (uv sync) or wait for the component that adds this tool',
        candidates: this.toolNames(),
      });
    }
    let result: Awaited<ReturnType<Client['callTool']>>;
    try {
      result = await client.callTool({ name, arguments: args }, undefined, {
        timeout: this.opts.callTimeoutMs,
        resetTimeoutOnProgress: true,
      });
    } catch (err) {
      if (err instanceof KiwrapError) {
        throw err;
      }
      throw new KiwrapError({ code: 'E_CLIENT', message: `${name}: ${errorText(err)}` });
    }
    this.restartsInARow = 0;
    return parseResult<T>(name, result as RawResult);
  }
}

interface RawContent {
  type: string;
  text?: string;
  data?: string;
  mimeType?: string;
}
interface RawResult {
  content?: RawContent[];
  structuredContent?: unknown;
  isError?: boolean;
}

/** Turn a CallToolResult into a Receipt or throw the structured error (exported for tests). */
export function parseResult<T>(name: string, result: RawResult): Receipt<T> {
  const content = result.content ?? [];
  const text = content
    .filter((c) => c.type === 'text' && typeof c.text === 'string')
    .map((c) => c.text as string)
    .join('\n');
  if (result.isError) {
    const shape = asErrorShape(result.structuredContent) ?? errorFromText(text);
    throw new KiwrapError(shape);
  }
  let structured = result.structuredContent as Receipt<T> | undefined;
  if (!structured || typeof structured !== 'object') {
    // an old server without structuredContent: the text body is "<summary line>\n<json>"
    const nl = text.indexOf('\n');
    const body = nl >= 0 ? text.slice(nl + 1) : text;
    try {
      structured = JSON.parse(body) as Receipt<T>;
    } catch {
      throw new KiwrapError({ code: 'E_CLIENT', message: `${name}: result carries no structured content` });
    }
  }
  const receipt: Receipt<T> = {
    ok: structured.ok ?? true,
    changed: structured.changed ?? { added: [], updated: [], removed: [] },
    validators: structured.validators ?? [],
    provider_used: structured.provider_used ?? '',
    degraded: structured.degraded ?? [],
    snapshot: structured.snapshot ?? null,
    stage: structured.stage ?? null,
    next_suggested: structured.next_suggested ?? null,
    warnings: structured.warnings ?? [],
    image_info: structured.image_info ?? null,
    data: (structured.data ?? {}) as T,
  };
  const image = content.find((c) => c.type === 'image' && typeof c.data === 'string');
  if (image) {
    receipt.imageBase64 = image.data;
  }
  return receipt;
}

function asErrorShape(value: unknown): KiwrapErrorShape | null {
  if (value && typeof value === 'object' && 'code' in value && 'message' in value) {
    const v = value as Record<string, unknown>;
    return {
      code: String(v.code),
      message: String(v.message),
      why: typeof v.why === 'string' ? v.why : '',
      fix: typeof v.fix === 'string' ? v.fix : '',
      candidates: Array.isArray(v.candidates) ? v.candidates.map(String) : [],
      example: typeof v.example === 'string' ? v.example : null,
      data: (v.data as Record<string, unknown>) ?? {},
    };
  }
  return null;
}

function errorFromText(text: string): KiwrapErrorShape {
  // error_to_result puts the JSON payload on the last line
  const lines = text.trim().split('\n');
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const line = lines[i].trim();
    if (line.startsWith('{')) {
      try {
        const shape = asErrorShape(JSON.parse(line));
        if (shape) {
          return shape;
        }
      } catch {
        /* fall through */
      }
    }
  }
  const m = /^(E_[A-Z_]+):\s*(.*)$/.exec(lines[0] ?? '');
  return m ? { code: m[1], message: m[2] } : { code: 'E_UNKNOWN', message: text || 'tool failed' };
}

function errorText(err: unknown): string {
  if (err instanceof Error) {
    return err.message;
  }
  return String(err);
}

function pipeLines(stream: Stream, sink: (line: string) => void): void {
  let buffer = '';
  stream.on('data', (chunk: Buffer | string) => {
    buffer += chunk.toString();
    let idx = buffer.indexOf('\n');
    while (idx >= 0) {
      sink(buffer.slice(0, idx).replace(/\r$/, ''));
      buffer = buffer.slice(idx + 1);
      idx = buffer.indexOf('\n');
    }
  });
  stream.on('end', () => {
    if (buffer) {
      sink(buffer);
    }
  });
}
