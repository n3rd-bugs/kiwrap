/*
 * Test-harness extension: activates the Live KiCad module alone (no ext-core) with a FAKE kw_live client
 * so the custom editors, panels, helper ownership and the webview client can be exercised in a real
 * Extension Development Host against test/live/fake_helper. Built by build.mjs into a scratch folder.
 */
import * as vscode from 'vscode';
import { activateLive, type LiveApi } from '../../../src/live';
import type { KiwrapClient } from '../../../src/shared/types';

export interface HarnessExports {
  api: LiveApi;
  calls: Array<{ name: string; args: Record<string, unknown> }>;
  vscode: typeof vscode;
}

export function activate(context: vscode.ExtensionContext): HarnessExports {
  const calls: HarnessExports['calls'] = [];
  const patched = process.env.KIWRAP_HARNESS_PATCHED !== '0';
  const client: KiwrapClient = {
    async callTool(name, args) {
      calls.push({ name, args });
      if (name !== 'kw_live') { throw new Error(`fake client: unexpected tool ${name}`); }
      const a = args as { action: string; project?: string };
      switch (a.action) {
        case 'launch':
          // shaped like ext-core's Receipt so unwrapToolResult's Receipt path is exercised end-to-end
          return {
            ok: true, changed: { added: [], updated: [], removed: [] }, validators: [], provider_used: 'fake', degraded: [], snapshot: null, stage: null, next_suggested: null, warnings: [], image_info: null,
            data: { session_id: 'fake-session-1', pid: process.pid, install: { name: 'fake/kiwrap', capabilities: { 'gui.input_from_events': patched } }, api_socket: 'ipc://fake', token: 'fake-api-token', editors: ['pcb', 'sch'], project: a.project },
          };
        case 'status': return { session_id: 'fake-session-1', pid: process.pid, install: 'fake', api_socket: '', token: '', editors: [], alive: true };
        case 'detach': case 'raise': return { ok: true, data: { done: a.action } };
        case 'screenshot': return { png_base64: '', note: 'fake' };
        default: throw new Error(`fake client: unknown action ${a.action}`);
      }
    },
  };
  const output = vscode.window.createOutputChannel('kiwrap live (harness)');
  context.subscriptions.push(output);
  const api = activateLive(context, { client, output });
  const exports: HarnessExports = { api, calls, vscode };
  // Manual runs with the real helper (test/live/manual/run_real_helper.ps1): attach to a KiCad pid at startup.
  const autoPid = Number(process.env.KIWRAP_HARNESS_ATTACH_PID ?? 0);
  if (autoPid > 0) {
    const inject = (process.env.KIWRAP_HARNESS_INJECT ?? 'none') as 'none' | 'mirror' | 'events' | 'hybrid';
    const file = process.env.KIWRAP_HARNESS_OPEN_FILE ?? '';
    setTimeout(async () => {
      try {
        const st = await api.attach({ pid: autoPid, inject, project: file ? require('path').dirname(file) : undefined, allowAnyProcess: process.env.KIWRAP_HARNESS_ANY_PROCESS === '1' });
        output.appendLine(`[harness] auto-attached to pid ${autoPid}: ${JSON.stringify({ helper: st.helper, surfaces: st.surfaces.map((s) => s.kind + ':' + s.title) })}`);
        if (file) { await vscode.commands.executeCommand('vscode.openWith', vscode.Uri.file(file), 'kiwrap.live'); }
      } catch (e) { output.appendLine(`[harness] auto-attach failed: ${String(e)}`); void vscode.window.showErrorMessage(`auto-attach failed: ${String(e)}`); }
    }, 500);
  }
  (globalThis as unknown as { __kiwrapLiveHarness?: HarnessExports }).__kiwrapLiveHarness = exports;
  return exports;
}

export function deactivate(): void { /* the module's disposables are on context.subscriptions */ }
