/**
 * "kiwrap: Configure Claude Code": plan the edits (configurePlan.ts), show each one as a diff
 * against a virtual "proposed" document, then apply on confirmation.
 */
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { KiwrapSession } from '../session';
import { cfg, resolveServerProject, resolveStorageHome } from '../settings';
import { applyEdits, planConfigure, uvCommand, type PlannedEdit } from './configurePlan';

export const PROPOSED_SCHEME = 'kiwrap-proposed';

/** Serves the "after" text of planned edits for `vscode.diff`. */
export class ProposedContentProvider implements vscode.TextDocumentContentProvider {
  private readonly docs = new Map<string, string>();
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changed.event;

  set(file: string, text: string): vscode.Uri {
    const uri = vscode.Uri.from({ scheme: PROPOSED_SCHEME, path: '/' + file.replace(/\\/g, '/'), query: String(Date.now()) });
    this.docs.set(uri.toString(), text);
    return uri;
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.docs.get(uri.toString()) ?? '';
  }
}

export interface ConfigureOptions {
  /** Apply without the diff preview and the modal confirmation (scripted setups, tests). */
  apply?: boolean;
}

export async function configureClaudeCode(
  session: KiwrapSession,
  proposed: ProposedContentProvider,
  options: ConfigureOptions = {},
): Promise<PlannedEdit[] | undefined> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (!folders.length) {
    void vscode.window.showWarningMessage('kiwrap: open a workspace folder first (the .mcp.json goes there).');
    return undefined;
  }
  const workspace =
    folders.length === 1
      ? folders[0].uri.fsPath
      : (await vscode.window.showWorkspaceFolderPick({ placeHolder: 'Workspace folder to configure' }))?.uri.fsPath;
  if (!workspace) {
    return undefined;
  }
  const serverProject = resolveServerProject();
  if (!serverProject) {
    const choice = await vscode.window.showErrorMessage(
      'kiwrap: the kiwrap checkout is unknown. Set kiwrap.serverProject to the folder holding pyproject.toml and src/kiwrap.',
      'Open settings',
    );
    if (choice) {
      void vscode.commands.executeCommand('workbench.action.openSettings', 'kiwrap.serverProject');
    }
    return undefined;
  }
  // the same storage home for the agent's server, only when the setting names one (docs/EXTENSION.md section 6)
  const home = resolveStorageHome();
  const uvPath = cfg<string>('uvPath', 'uv');
  const edits = planConfigure({
    workspace,
    serverProject,
    uvPath,
    env: home ? { KIWRAP_HOME: home.replace(/\\/g, '/') } : {},
  });
  if (!edits.length) {
    void vscode.window.showInformationMessage('kiwrap: Claude Code is already configured for this workspace.');
    return [];
  }
  if (options.apply) {
    applyEdits(edits);
    session.log(`configure Claude Code: wrote ${edits.map((e) => e.file).join(', ')} (no preview: apply=true)`);
    return edits;
  }
  // preview: one diff editor per edit (existing file vs proposed), then a modal confirmation
  for (const e of edits) {
    const left = e.before === null ? proposed.set(e.file + ' (new)', '') : vscode.Uri.file(e.file);
    const right = proposed.set(e.file, e.after);
    const title = `${path.relative(workspace, e.file)} - ${e.reason}`;
    await vscode.commands.executeCommand('vscode.diff', left, right, title, { preview: false });
  }
  const summary = edits.map((e) => `${e.before === null ? 'create' : 'update'} ${path.relative(workspace, e.file)}`).join('\n');
  const choice = await vscode.window.showInformationMessage(
    `kiwrap: apply ${edits.length} change(s) for Claude Code?`,
    { modal: true, detail: summary },
    'Apply',
  );
  if (choice !== 'Apply') {
    session.log('configure Claude Code: cancelled');
    return undefined;
  }
  applyEdits(edits);
  session.log(`configure Claude Code: wrote ${edits.map((e) => e.file).join(', ')}`);
  await closeProposedDiffs();
  const uvNote = uvCommand(uvPath) === 'uv' ? ' Claude Code runs the server with `uv` from its own PATH.' : ` The server runs with ${uvCommand(uvPath)} (kiwrap.uvPath).`;
  void vscode.window.showInformationMessage(
    `kiwrap: Claude Code configured (${edits.length} file(s)). Reload Claude Code / VS Code so it picks up .mcp.json.${uvNote}`,
  );
  return edits;
}

/** Close the diff tabs whose modified side is one of our proposed documents. */
export async function closeProposedDiffs(): Promise<void> {
  const tabs = vscode.window.tabGroups.all
    .flatMap((g) => g.tabs)
    .filter((t) => t.input instanceof vscode.TabInputTextDiff && t.input.modified.scheme === PROPOSED_SCHEME);
  if (tabs.length) {
    await vscode.window.tabGroups.close(tabs, true).then(undefined, () => undefined);
  }
}
