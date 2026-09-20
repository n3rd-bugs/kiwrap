/**
 * `kiwrap.*` settings and the launch spec derived from them.
 *
 *  kiwrap.uvPath         `uv` executable (default "uv" on PATH)
 *  kiwrap.serverProject  the kiwrap checkout (uv project) providing `kiwrap serve`; empty = auto:
 *                        a workspace folder whose pyproject.toml names the `kiwrap` project, then
 *                        the KIWRAP_REPO environment variable
 *  kiwrap.storageHome    kiwrap's state directory (`--storage-home`); empty = kiwrap's own default
 *                        (KIWRAP_HOME / %LOCALAPPDATA%\kiwrap), shared with the agent's server
 *  kiwrap.kicadInstall   the KiCad to target for new projects (a version spec kw_env lists); empty = newest
 *  kiwrap.checkOnSave    re-run ERC / DRC when a .kicad_sch / .kicad_pcb is saved (default true)
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { buildServerSpec, type LaunchSettings } from './kiwrapServer';
import type { ServerSpec } from './mcpClient';

export const SECTION = 'kiwrap';

export function cfg<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration(SECTION).get<T>(key, fallback);
}

function pyprojectNamesKiwrap(dir: string): boolean {
  try {
    const text = fs.readFileSync(path.join(dir, 'pyproject.toml'), 'utf8');
    return /^\s*name\s*=\s*"kiwrap"/m.test(text) && fs.existsSync(path.join(dir, 'src', 'kiwrap'));
  } catch {
    return false;
  }
}

/** The kiwrap checkout to run the server from, or undefined when nothing is configured/found. */
export function resolveServerProject(): string | undefined {
  const configured = cfg<string>('serverProject', '').trim();
  if (configured) {
    return path.resolve(configured);
  }
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    if (pyprojectNamesKiwrap(folder.uri.fsPath)) {
      return folder.uri.fsPath;
    }
  }
  const env = process.env.KIWRAP_REPO;
  if (env && pyprojectNamesKiwrap(env)) {
    return path.resolve(env);
  }
  return undefined;
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

export function kicadInstallSetting(): string | undefined {
  const v = cfg<string>('kicadInstall', '').trim();
  return v || undefined;
}

export async function setKicadInstallSetting(value: string | undefined): Promise<void> {
  await vscode.workspace
    .getConfiguration(SECTION)
    .update('kicadInstall', value ?? '', vscode.ConfigurationTarget.Workspace);
}
