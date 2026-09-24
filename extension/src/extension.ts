/**
 * KiCad (kiwrap) - VS Code extension core (Phase 2, docs/ARCHITECTURE.md §9, D18: VS Code is
 * the one user surface; D13: Claude Code is the agent client).
 *
 * The extension is an MCP client of `uv run kiwrap serve` (kiwrapServer.ts) and calls the kw_*
 * tools like an agent would: kw_env for the tree, kw_project for the stage, kw_sch_check /
 * kw_pcb_drc for the Problems panel, kw_*_render for the previews, kw_live for the live session
 * (the capture view itself is the live component; it registers `kiwrap.live.open`).
 */
import * as vscode from 'vscode';
import { registerCommands } from './commands';
import { DiagnosticsManager } from './diagnostics/manager';
import { PreviewProvider } from './editors/previewProvider';
import { activateLive, type LiveApi } from './live';
import { KiwrapSession } from './session';
import { NOT_FOUND_MESSAGE } from './serverLocator';
import { initServerLocator, kicadInstallSetting, resolveServerCheckout, serverSpec } from './settings';
import { KiwrapStatusBar } from './status/statusBar';
import { KiwrapTree } from './status/tree';
import type { LiveSessionData } from './types';

/** What `extension.exports` offers other components and the integration tests. */
export interface KiwrapApi {
  session: KiwrapSession;
  tree: KiwrapTree;
  diagnostics: DiagnosticsManager;
  /** The Live KiCad module (custom editor `kiwrap.live`, helper + kw_live session). */
  live: LiveApi;
  /** The `kiwrap.preview` custom editor provider (`renders` / `onDidRender` are the test hooks). */
  preview: PreviewProvider;
  /** Resolves when the server answered kw_env (or failed to start). */
  whenReady(): Promise<void>;
}

let session: KiwrapSession | undefined;

export function activate(context: vscode.ExtensionContext): KiwrapApi {
  initServerLocator(context);
  session = new KiwrapSession(context);
  context.subscriptions.push(session);
  const diagnostics = new DiagnosticsManager(session);
  const tree = new KiwrapTree(session);
  const statusBar = new KiwrapStatusBar(session, diagnostics);
  context.subscriptions.push(
    diagnostics,
    tree,
    statusBar,
    vscode.window.createTreeView('kiwrap.status', { treeDataProvider: tree, showCollapseAll: true }),
    PreviewProvider.register(context, session),
  );
  const preview = PreviewProvider.current!;
  registerCommands({ context, session, diagnostics, tree });

  // The Live KiCad module: kw_live through this extension's MCP client, the helper process, the
  // kiwrap.live custom editor. Its session state is mirrored into the core model for the tree.
  const live = activateLive(context, {
    client: { callTool: (name, args) => session!.ready().then((s) => s.client.call(name, args)) },
    output: session.output,
    // D21: empty kiwrap.kicadInstall = kw_live picks the project's effective target
    targetKicad: () => kicadInstallSetting(),
  });
  context.subscriptions.push(
    live.sessions.onDidChange((state) => {
      const s = session;
      if (!s) {
        return;
      }
      if (!state) {
        s.live = s.live.kind === 'error' ? s.live : { kind: 'idle' };
      } else {
        const kw = state.session;
        const data: LiveSessionData = {
          ok: true,
          session_id: kw?.session_id ?? `pid-${state.pid}`,
          pid: state.pid,
          install: typeof kw?.install === 'string' ? kw.install : (kw?.install?.version ?? 'attached'),
          api_socket: kw?.api_socket ?? null,
          token: null,
          editors: live.sessions.surfaces.all.map((x) => x.kind),
          alive: true,
          mode: live.sessions.mode,
          mode_used: kw?.mode_used,
          cloaked: kw?.cloaked,
          project: state.project,
        };
        s.live = { kind: 'running', session: data };
      }
      s.fire();
    }),
    live.sessions.surfaces.onChange(() => {
      const s = session;
      if (s && s.live.kind === 'running') {
        s.live.session.editors = live.sessions.surfaces.all.map((x) => `${x.kind}${x.cloaked ? ' (cloaked)' : ''}`);
        s.fire();
      }
    }),
  );

  let notFoundShown = false;
  const configure = () => {
    // Resolve (and log) before building the spec: the first resolve remembers a build-origin /
    // workspace hit, so a second one would report it as 'remembered'.
    const where = resolveServerCheckout();
    const spec = serverSpec();
    if (where) {
      session?.output.appendLine(`kiwrap server: checkout ${where.dir} (${where.source})`);
    }
    session?.attachServer(spec, spec ? undefined : NOT_FOUND_MESSAGE);
    if (!spec && !notFoundShown) {
      // Once per window: the one action that fixes it, instead of a dead-end error on every tool call.
      notFoundShown = true;
      void vscode.window
        .showWarningMessage(`kiwrap: ${NOT_FOUND_MESSAGE}.`, 'Locate kiwrap checkout...')
        .then((choice) => (choice ? vscode.commands.executeCommand('kiwrap.locateServer') : undefined));
    }
  };
  configure();
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (
        e.affectsConfiguration('kiwrap.uvPath') ||
        e.affectsConfiguration('kiwrap.serverProject') ||
        e.affectsConfiguration('kiwrap.storageHome')
      ) {
        configure();
        void ready;
        ready = bootstrap();
      } else if (e.affectsConfiguration('kiwrap.kicadInstall')) {
        session?.fire();
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      session?.setProject(undefined);
      void session?.refreshProject();
    }),
  );

  const bootstrap = async () => {
    const s = session;
    if (!s) {
      return;
    }
    await s.refreshEnv();
    await s.refreshProject();
  };
  let ready = bootstrap();

  return {
    session,
    tree,
    diagnostics,
    live,
    preview,
    whenReady: () => ready,
  };
}

export function deactivate(): Thenable<void> | undefined {
  const s = session;
  session = undefined;
  return s?.server?.stop();
}
