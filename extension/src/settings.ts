/**
 * `kiwrap.*` settings and the launch spec derived from them.
 *
 *  kiwrap.uvPath         `uv` executable (default "uv" on PATH)
 *  kiwrap.serverProject  the kiwrap checkout (uv project) providing `kiwrap serve`; empty = auto
 *                        (serverLocator.ts): an open folder that is the checkout, KIWRAP_REPO, the
 *                        extension's own parent when run from source, the checkout remembered from
 *                        any earlier workspace, the checkout the VSIX was built from
 *  kiwrap.storageHome    kiwrap's state directory (`--storage-home`); empty = kiwrap's own default
 *                        (KIWRAP_HOME / %LOCALAPPDATA%\kiwrap), shared with the agent's server
 *  kiwrap.kicadInstall   the KiCad to use (a version spec kw_env lists, or an install root); empty or
 *                        "project" = let kiwrap pick the KiCad matching the project's file format
 *                        (D21, review item 1 fix layer 2: it used to mean "newest", which re-stamped
 *                        a 10.0 project as 10.99 through the live launch)
 *  kiwrap.checkOnSave    re-run ERC / DRC when a .kicad_sch / .kicad_pcb is saved (default true)
 */
import * as path from 'node:path';
import * as vscode from 'vscode';
import { buildServerSpec, type LaunchSettings } from './kiwrapServer';
import type { ServerSpec } from './mcpClient';
import { type Candidate, type Checkout, firstCheckout, isKiwrapCheckout, readBuildOrigin } from './serverLocator';
import { isProjectInstallSpec } from './status/installModel';

export const SECTION = 'kiwrap';

export function cfg<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration(SECTION).get<T>(key, fallback);
}

/** Global-state key of the checkout remembered across workspaces. */
const REMEMBERED_KEY = 'kiwrap.serverProject.remembered';

let globalState: vscode.Memento | undefined;
let extensionPath: string | undefined;

/** Called once from activate(): gives the resolver its global state and the extension's install dir. */
export function initServerLocator(context: vscode.ExtensionContext): void {
  globalState = context.globalState;
  extensionPath = context.extensionPath;
}

function candidates(): Candidate[] {
  const out: Candidate[] = [{ source: 'setting', dir: cfg<string>('serverProject', '') }];
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    out.push({ source: 'workspace', dir: folder.uri.fsPath });
  }
  out.push({ source: 'env', dir: process.env.KIWRAP_REPO });
  out.push({ source: 'extension-dev', dir: extensionPath ? path.dirname(extensionPath) : undefined });
  out.push({ source: 'remembered', dir: globalState?.get<string>(REMEMBERED_KEY) });
  out.push({ source: 'build-origin', dir: readBuildOrigin(extensionPath) });
  return out;
}

/** The kiwrap checkout to run the server from and where it came from, or undefined. */
export function resolveServerCheckout(): Checkout | undefined {
  const hit = firstCheckout(candidates());
  // Remember every valid checkout found, so a workspace without one still finds it next time.
  if (hit && hit.source !== 'remembered' && isKiwrapCheckout(hit.dir)) {
    if (globalState && globalState.get<string>(REMEMBERED_KEY) !== hit.dir) {
      void globalState.update(REMEMBERED_KEY, hit.dir);
    }
  }
  return hit;
}

/** The kiwrap checkout to run the server from, or undefined when nothing is configured/found. */
export function resolveServerProject(): string | undefined {
  return resolveServerCheckout()?.dir;
}

/**
 * Folder picker for the checkout: validates the pick, writes kiwrap.serverProject to User settings
 * (the setting is machine-scoped, so a workspace value would be ignored) and remembers it.
 * Returns the chosen checkout, or undefined when cancelled / not a checkout.
 */
export async function locateServerProject(): Promise<string | undefined> {
  const picked = await vscode.window.showOpenDialog({
    canSelectFolders: true,
    canSelectFiles: false,
    canSelectMany: false,
    openLabel: 'Use this kiwrap checkout',
    title: 'kiwrap: select the kiwrap checkout (the folder with pyproject.toml and src/kiwrap)',
  });
  const dir = picked?.[0]?.fsPath;
  if (!dir) {
    return undefined;
  }
  if (!isKiwrapCheckout(dir)) {
    void vscode.window.showErrorMessage(
      `kiwrap: ${dir} is not a kiwrap checkout (it needs a pyproject.toml naming "kiwrap" and a src/kiwrap folder).`,
    );
    return undefined;
  }
  await vscode.workspace.getConfiguration(SECTION).update('serverProject', dir, vscode.ConfigurationTarget.Global);
  await globalState?.update(REMEMBERED_KEY, dir);
  return dir;
}

/** The configured storage home, or '' for kiwrap's own default (one home shared with the agent). */
export function resolveStorageHome(): string {
  const configured = cfg<string>('storageHome', '').trim();
  return configured ? path.resolve(configured) : '';
}

export function launchSettings(): LaunchSettings | undefined {
  const serverProject = resolveServerProject();
  if (!serverProject) {
    return undefined;
  }
  return {
    uvPath: cfg<string>('uvPath', 'uv').trim() || 'uv',
    serverProject,
    storageHome: resolveStorageHome(),
  };
}

export function serverSpec(): ServerSpec | undefined {
  const s = launchSettings();
  return s ? buildServerSpec(s) : undefined;
}

/** The explicit install spec, or undefined when kiwrap should match the project's file format. */
export function kicadInstallSetting(): string | undefined {
  const v = cfg<string>('kicadInstall', '').trim();
  return isProjectInstallSpec(v) ? undefined : v;
}

export async function setKicadInstallSetting(value: string | undefined): Promise<void> {
  await vscode.workspace
    .getConfiguration(SECTION)
    .update('kicadInstall', value ?? '', vscode.ConfigurationTarget.Workspace);
}
