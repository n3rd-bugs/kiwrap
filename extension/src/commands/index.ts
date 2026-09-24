/**
 * Command registrations: ERC / DRC, Open Live KiCad (delegates to `kiwrap.live.open` when the
 * live component registered it, else calls kw_live when the server publishes it), Select KiCad
 * install, Upgrade project file format, Configure Claude Code, server restart / output / refresh,
 * open the preview.
 *
 * Errors the consent flow already showed (consent.ts: the D21 upgrade modal, E_FORMAT_NEWER,
 * E_READONLY) are `handled` and not shown again here.
 */
import * as vscode from 'vscode';
import type { DiagnosticsManager } from '../diagnostics/manager';
import { PREVIEW_VIEW_TYPE } from '../editors/previewProvider';
import { findProjects, isBoard, isSchematic, projectForFile, type KicadProject } from '../project';
import type { KiwrapSession } from '../session';
import { formatLabel, proactiveUpgradeInfo } from '../consent';
import { kicadInstallSetting, locateServerProject, serverSpec, setKicadInstallSetting } from '../settings';
import { effectiveTarget, installFormats, installSpec, MATCH_PROJECT_LABEL, matchesSpec, matchingInstalls, selectionArg } from '../status/installModel';
import type { KiwrapTree } from '../status/tree';
import { KiwrapError, isHandledError, type LiveEditor } from '../types';
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
  if (isHandledError(err)) {
    session.log(`${what}: ${err instanceof Error ? err.message : String(err)} (already shown)`);
    return;
  }
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

  // Picks the kiwrap checkout and writes kiwrap.serverProject (User settings); the configuration
  // change restarts the server (extension.ts onDidChangeConfiguration).
  reg('kiwrap.locateServer', async () => {
    const dir = await locateServerProject();
    if (dir) {
      void vscode.window.showInformationMessage(`kiwrap: using the checkout ${dir}.`);
    }
    return dir;
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
    // a string (programmatic) or the tree's install node (the inline "Select" action and the
    // context menu pass the element) selects directly; anything else opens the picker
    let version: string | undefined = selectionArg(arg);
    if (version === undefined) {
      const st = session.projectStatus;
      const effective = effectiveTarget(st);
      const matching = matchingInstalls(rows, st?.epochs);
      const items = [
        {
          label: `$(symbol-file) ${MATCH_PROJECT_LABEL}`,
          description: effective
            ? `KiCad ${effective} for ${session.project?.name ?? 'the project'}`
            : 'kiwrap picks the KiCad whose file format equals the project files (D21)',
          detail: undefined as string | undefined,
          version: '',
        },
        ...rows.map((r) => {
          const formats = Object.entries(installFormats(r));
          const caps = r.capabilities ? Object.entries(r.capabilities).filter(([, v]) => v === true).map(([k]) => k).join(' ') : '';
          return {
            label: `$(circuit-board) KiCad ${r.version}`,
            description: `${matching.includes(r) ? "writes the project's format - " : ''}${r.source} - ${r.root}`,
            detail: [formats.length ? `formats ${formats.map(([k, v]) => `${k} ${v}`).join(', ')}` : '', caps].filter(Boolean).join(' | ') || undefined,
            version: installSpec(r, rows),
          };
        }),
      ];
      const pick = await vscode.window.showQuickPick(items, { placeHolder: 'KiCad install kiwrap uses (kiwrap.kicadInstall)' });
      if (!pick) {
        return;
      }
      version = pick.version;
    }
    await setKicadInstallSetting(version || undefined);
    session.log(`kicadInstall = ${version || "(match the project's file format)"}`);
    session.fire();
    // An existing project can also be pinned to the explicit choice: kw_project retarget (contract
    // (c)) writes the pin without touching the files (this used to hand-edit .kiwrap/state.json).
    // Not awaited: the question must not block the selection.
    const p = session.pickWorkspaceProject();
    if (p && version && session.projectStatus) {
      const pinned = version;
      void vscode.window
        .showInformationMessage(
          `kiwrap: also pin project ${p.name} to KiCad ${pinned}? Its files are not changed; kiwrap still asks before saving them in a newer format.`,
          'Pin project',
          'Keep',
        )
        .then(async (choice) => {
          if (choice !== 'Pin project') {
            return;
          }
          try {
            const server = await session.ready();
            await server.projectRetarget(p.pro, pinned);
            session.log(`pinned ${p.name} -> ${pinned}`);
          } catch (err) {
            showError(session, 'pin (kw_project retarget) failed', err);
          }
          void session.refreshProject();
        });
    }
    void session.refreshProject();
  });

  // D21: the proactive form of the consent modal (the tree's "file format" row offers it when no
  // installed KiCad writes the project's format); the reactive form is consent.ts on any call.
  reg('kiwrap.upgradeProject', async (arg) => {
    const p = await resolveProject(session, arg instanceof vscode.Uri ? arg : undefined);
    if (!p) {
      return;
    }
    let server;
    try {
      server = await session.ready();
    } catch (err) {
      showError(session, 'upgrade', err);
      return;
    }
    const st = session.projectStatus ?? (await session.refreshProject());
    const rows = session.env?.installs ?? [];
    const upgradeTo = kicadInstallSetting() ?? session.env?.default_install ?? undefined;
    // the server's project_kicad / install_hint / file_epochs / candidates (fixer finding 3), and the
    // installs that already write the files' format (a 9.0 project with 9.0.9 installed): the modal
    // says so instead of claiming none can; upgrading to one of them is a no-op
    const { info, writers } = proactiveUpgradeInfo(p.pro, st, rows, upgradeTo);
    const target = upgradeTo ? rows.find((r) => matchesSpec(r, upgradeTo)) : undefined;
    if (target && writers.includes(target.version)) {
      void vscode.window.showInformationMessage(`kiwrap: ${p.name} is already in the KiCad ${target.version} file format.`);
      return;
    }
    session.log(`upgrade requested for ${p.name} (${formatLabel(info)} -> ${upgradeTo ?? 'server default'})`);
    try {
      await session.consent.decide(info, (n, a, o) => server.client.call(n, a, o));
    } catch (err) {
      showError(session, 'upgrade', err);
    }
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
      const r = await server.live({ action: 'launch', project: p.pro, editor, mode: 'view', target_kicad: kicadInstallSetting() });
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
