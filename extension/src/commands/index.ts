/**
 * Command registrations: ERC / DRC, Open Live KiCad (delegates to `kiwrap.live.open` when the
 * live component registered it, else calls kw_live when the server publishes it), Select KiCad
 * install, Configure Claude Code, server restart / output / refresh, open the preview.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { DiagnosticsManager } from '../diagnostics/manager';
import { PREVIEW_VIEW_TYPE } from '../editors/previewProvider';
import { findProjects, isBoard, isSchematic, projectForFile, type KicadProject } from '../project';
import type { KiwrapSession } from '../session';
import { kicadInstallSetting, serverSpec, setKicadInstallSetting } from '../settings';
import type { KiwrapTree } from '../status/tree';
import { KiwrapError, type LiveEditor } from '../types';
import { configureClaudeCode, ProposedContentProvider, PROPOSED_SCHEME } from './configureClaude';

export const LIVE_OPEN_COMMAND = 'kiwrap.live.open';
export const LIVE_CLOSE_COMMAND = 'kiwrap.live.close';

interface Deps {
  context: vscode.ExtensionContext;
  session: KiwrapSession;
  diagnostics: DiagnosticsManager;
  tree: KiwrapTree;
}

function showError(session: KiwrapSession, what: string, err: unknown): void {
  const text = err instanceof KiwrapError ? `${err.message}${err.fix ? ` - ${err.fix}` : ''}` : String(err);
  session.log(`${what}: ${text}`);
  void vscode.window.showErrorMessage(`kiwrap: ${what}: ${text}`, 'Show output').then((c) => {
    if (c) {
      session.output.show(true);
    }
  });
}

/** The project a command applies to: the argument's file, the active editor's file, else the workspace's. */
async function resolveProject(session: KiwrapSession, arg?: unknown): Promise<KicadProject | undefined> {
  const uri = arg instanceof vscode.Uri ? arg : vscode.window.activeTextEditor?.document.uri;
  if (uri && uri.scheme === 'file') {
    const p = projectForFile(uri.fsPath);
    if (p) {
      return p;
    }
  }
  const current = session.pickWorkspaceProject();
  if (current) {
    return current;
  }
  const folders = vscode.workspace.workspaceFolders ?? [];
  const all = folders.flatMap((f) => findProjects(f.uri.fsPath, 3));
  if (!all.length) {
    void vscode.window.showWarningMessage('kiwrap: no .kicad_pro found in the workspace.');
    return undefined;
  }
  const pick = await vscode.window.showQuickPick(
    all.map((p) => ({ label: p.name, description: p.pro, project: p })),
    { placeHolder: 'KiCad project' },
  );
  if (pick) {
    session.setProject(pick.project);
  }
  return pick?.project;
}

export function registerCommands(deps: Deps): void {
  const { context, session, diagnostics, tree } = deps;
  const proposed = new ProposedContentProvider();
  const reg = (id: string, fn: (...args: unknown[]) => unknown) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));

  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(PROPOSED_SCHEME, proposed));

  reg('kiwrap.runErc', async (arg) => {
    const p = await resolveProject(session, arg);
    if (p) {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `kiwrap ERC ${p.name}` },
        () => diagnostics.runErc(p),
      );
    }
  });

  reg('kiwrap.runDrc', async (arg) => {
    const p = await resolveProject(session, arg);
    if (p) {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `kiwrap DRC ${p.name}` },
        () => diagnostics.runDrc(p),
      );
    }
  });

  reg('kiwrap.runChecks', async (arg) => {
    const p = await resolveProject(session, arg);
    if (p) {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `kiwrap ERC + DRC ${p.name}` },
        async () => {
          await diagnostics.runErc(p);
          await diagnostics.runDrc(p);
        },
      );
    }
  });

  reg('kiwrap.configureClaude', (arg) =>
    configureClaudeCode(session, proposed, arg && typeof arg === 'object' ? (arg as { apply?: boolean }) : {}),
  );

  reg('kiwrap.selectInstall', async (arg) => {
    if (!session.env) {
      await session.refreshEnv();
    }
    const rows = session.env?.installs ?? [];
    if (!rows.length) {
      void vscode.window.showWarningMessage(`kiwrap: no KiCad installs known${session.envError ? ` (${session.envError})` : ''}.`);
      return;
    }
    let version: string | undefined = typeof arg === 'string' ? arg : undefined;
    if (!version) {
      const items = [
        { label: '$(history) newest (kiwrap default)', description: session.env?.default_install ?? '', version: '' },
        ...rows.map((r) => ({
          label: `$(circuit-board) KiCad ${r.version}`,
          description: `${r.source} - ${r.root}`,
          detail: r.capabilities ? Object.entries(r.capabilities).filter(([, v]) => v === true).map(([k]) => k).join(' ') : undefined,
          version: r.version,
        })),
      ];
      const pick = await vscode.window.showQuickPick(items, { placeHolder: 'KiCad install to target (kiwrap.kicadInstall)' });
      if (!pick) {
        return;
      }
      version = pick.version;
    }
    await setKicadInstallSetting(version || undefined);
    session.log(`kicadInstall = ${version || '(newest)'}`);
    // an existing project remembers its own target in .kiwrap/state.json (kiwrap.core.state.project);
    // offer to retarget it - there is no kw_project action for this yet (reported as a deviation)
    const p = session.pickWorkspaceProject();
    const stateFile = p ? path.join(p.dir, '.kiwrap', 'state.json') : undefined;
    if (p && stateFile && fs.existsSync(stateFile) && version) {
      const choice = await vscode.window.showInformationMessage(
        `kiwrap: also retarget project ${p.name} (its .kiwrap/state.json) to KiCad ${version}? Stages re-validate on the next check.`,
        'Retarget',
        'Keep',
      );
      if (choice === 'Retarget') {
        try {
          const raw = JSON.parse(fs.readFileSync(stateFile, 'utf8')) as Record<string, unknown>;
          raw.target_kicad = version;
          fs.writeFileSync(stateFile, JSON.stringify(raw, null, 1) + '\n', 'utf8');
          session.log(`retargeted ${p.name} -> ${version}`);
        } catch (err) {
          showError(session, 'retarget failed', err);
        }
      }
    }
    void session.refreshProject();
    session.fire();
  });

  reg('kiwrap.openLive', async (arg) => {
    const uri = arg instanceof vscode.Uri ? arg : vscode.window.activeTextEditor?.document.uri;
    const p = await resolveProject(session, arg);
    if (!p) {
      return;
    }
    const editor: LiveEditor = uri && isBoard(uri.fsPath) ? 'pcb' : uri && isSchematic(uri.fsPath) ? 'sch' : 'project';
    const commands = await vscode.commands.getCommands(true);
    if (commands.includes(LIVE_OPEN_COMMAND)) {
      // the live component owns the helper + view; hand it the project, the editor kind and the
      // install the user selected (kiwrap.kicadInstall; kw_live defaults to the project's target)
      // the project DIRECTORY, the shape the live tab's Start button sends too (one identity per
      // project: reviewer finding 7 - a .kicad_pro path here and the directory there restarted KiCad)
      await vscode.commands.executeCommand(LIVE_OPEN_COMMAND, {
        project: p.dir,
        editor,
        file: uri?.fsPath,
        target_kicad: kicadInstallSetting(),
      });
      return;
    }
    let server;
    try {
      server = await session.ready();
    } catch (err) {
      showError(session, 'Open Live KiCad', err);
      return;
    }
    if (!server.liveAvailable) {
      session.live = { kind: 'idle' };
      void vscode.window.showInformationMessage(
        'kiwrap: Live KiCad is not available yet - the live component (kiwrap.live.open) is not installed and this kiwrap server does not publish kw_live.',
      );
      return;
    }
    session.live = { kind: 'launching' };
    session.fire();
    try {
      const r = await server.live({ action: 'launch', project: p.pro, editor, mode: 'auto', target_kicad: kicadInstallSetting() });
      session.live = { kind: 'running', session: r.data };
      session.log(`kw_live launch: session ${r.data.session_id} pid ${r.data.pid} (${r.data.install})`);
      void vscode.window.showInformationMessage(
        `kiwrap: live KiCad ${r.data.install} started (pid ${r.data.pid}); the capture view needs the live component.`,
      );
    } catch (err) {
      session.live = { kind: 'error', message: err instanceof KiwrapError ? err.shortMessage : String(err) };
      showError(session, 'kw_live launch', err);
    }
    session.fire();
  });

  reg('kiwrap.closeLive', async () => {
    const commands = await vscode.commands.getCommands(true);
    if (commands.includes(LIVE_CLOSE_COMMAND)) {
      await vscode.commands.executeCommand(LIVE_CLOSE_COMMAND);
      if (session.live.kind === 'running') {
        session.live = { kind: 'idle' };
        session.fire();
      }
      return;
    }
    if (session.live.kind !== 'running' || !session.server) {
      return;
    }
    try {
      await session.server.live({ action: 'detach', session_id: session.live.session.session_id });
      session.live = { kind: 'idle' };
    } catch (err) {
      showError(session, 'kw_live detach', err);
    }
    session.fire();
  });

  reg('kiwrap.restartServer', async () => {
    session.attachServer(serverSpec(), undefined);
    try {
      await session.ready();
      await session.refreshEnv();
      await session.refreshProject();
    } catch (err) {
      showError(session, 'server start', err);
    }
  });

  reg('kiwrap.refresh', async () => {
    await session.refreshEnv(true);
    await session.refreshProject();
    tree.refresh();
  });

  // loose coupling for the live module (src/shared/types.ts CORE_CALL_TOOL_COMMAND): any kw_* tool
  // through this extension's MCP client; returns the Receipt, throws KiwrapError
  reg('kiwrap.core.callTool', async (name, args) => {
    const server = await session.ready();
    return server.client.call(String(name), (args as Record<string, unknown>) ?? {});
  });

  reg('kiwrap.showOutput', () => session.output.show(true));
  reg('kiwrap.showProblems', () => vscode.commands.executeCommand('workbench.actions.view.problems'));

  reg('kiwrap.openPreview', async (arg) => {
    const uri = arg instanceof vscode.Uri ? arg : vscode.window.activeTextEditor?.document.uri;
    if (!uri) {
      return;
    }
    await vscode.commands.executeCommand('vscode.openWith', uri, PREVIEW_VIEW_TYPE, vscode.ViewColumn.Beside);
  });

  reg('kiwrap.pickProject', async () => {
    const folders = vscode.workspace.workspaceFolders ?? [];
    const all = folders.flatMap((f) => findProjects(f.uri.fsPath, 3));
    const pick = await vscode.window.showQuickPick(
      all.map((p) => ({ label: p.name, description: p.pro, project: p })),
      { placeHolder: 'KiCad project to track' },
    );
    if (pick) {
      session.setProject(pick.project);
      diagnostics.clear();
      await session.refreshProject();
    }
  });
}
