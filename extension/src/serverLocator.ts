/**
 * Where the kiwrap checkout (the uv project providing `kiwrap serve`) is, independent of the
 * workspace the user has open. Pure Node (no vscode import) so the unit tests can drive it.
 *
 * Order, first hit wins:
 *   setting       kiwrap.serverProject (User settings; taken as-is so a wrong path is reported, not skipped)
 *   workspace     an open workspace folder that IS the checkout
 *   env           KIWRAP_REPO
 *   extension-dev the extension's own parent folder (running from source / an Extension Development Host)
 *   remembered    the last checkout any workspace resolved (global state), so a checkout found once
 *                 keeps working in every other workspace
 *   build-origin  the checkout scripts/build_extension.ps1 packaged the VSIX from (kiwrap-origin.json)
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export type CheckoutSource = 'setting' | 'workspace' | 'env' | 'extension-dev' | 'remembered' | 'build-origin';

export interface Candidate {
  source: CheckoutSource;
  dir: string | undefined;
}

export interface Checkout {
  source: CheckoutSource;
  dir: string;
}

/** The file scripts/build_extension.ps1 writes next to package.json before `vsce package`. */
export const ORIGIN_FILE = 'kiwrap-origin.json';

/** A kiwrap checkout: pyproject.toml naming the `kiwrap` project plus src/kiwrap. */
export function isKiwrapCheckout(dir: string | undefined): boolean {
  if (!dir) {
    return false;
  }
  try {
    const text = fs.readFileSync(path.join(dir, 'pyproject.toml'), 'utf8');
    return /^\s*name\s*=\s*"kiwrap"/m.test(text) && fs.existsSync(path.join(dir, 'src', 'kiwrap'));
  } catch {
    return false;
  }
}

/** The checkout recorded at packaging time, or undefined (no file, unreadable, not a checkout any more). */
export function readBuildOrigin(extensionPath: string | undefined): string | undefined {
  if (!extensionPath) {
    return undefined;
  }
  try {
    const data = JSON.parse(fs.readFileSync(path.join(extensionPath, ORIGIN_FILE), 'utf8')) as { checkout?: unknown };
    return typeof data.checkout === 'string' && data.checkout.trim() ? data.checkout.trim() : undefined;
  } catch {
    return undefined;
  }
}

/**
 * First usable candidate. An explicit setting wins even when it is not a valid checkout, so the
 * server start fails with a message naming that path instead of silently running another checkout;
 * every automatic source must pass isKiwrapCheckout.
 */
export function firstCheckout(candidates: readonly Candidate[]): Checkout | undefined {
  for (const c of candidates) {
    if (!c.dir || !c.dir.trim()) {
      continue;
    }
    const dir = path.resolve(c.dir.trim());
    if (c.source === 'setting' || isKiwrapCheckout(dir)) {
      return { source: c.source, dir };
    }
  }
  return undefined;
}

/** Human-readable reason shown when nothing resolves (output channel, notifications, teaching errors). */
export const NOT_FOUND_MESSAGE =
  'the kiwrap checkout was not found: kiwrap.serverProject is empty, no open folder is the checkout, ' +
  'KIWRAP_REPO is unset and no earlier session remembered one';

export const NOT_FOUND_FIX =
  'run "kiwrap: Locate kiwrap checkout..." and pick the folder with pyproject.toml and src/kiwrap ' +
  '(or set kiwrap.serverProject in User settings)';
