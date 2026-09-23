/** Finding the kiwrap checkout from any workspace (the extension opened outside the kiwrap repo). */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { firstCheckout, isKiwrapCheckout, ORIGIN_FILE, readBuildOrigin } from '../../src/serverLocator';

function makeCheckout(root: string, name = 'kiwrap'): string {
  fs.mkdirSync(path.join(root, 'src', 'kiwrap'), { recursive: true });
  fs.writeFileSync(path.join(root, 'pyproject.toml'), `[project]\nname = "${name}"\nversion = "0.0.1"\n`);
  return root;
}

describe('serverLocator', () => {
  let tmp: string;
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kiwrap-locator-'));
  });
  afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

  it('recognises a checkout only with pyproject name "kiwrap" and src/kiwrap', () => {
    const good = makeCheckout(path.join(tmp, 'good'));
    const other = makeCheckout(path.join(tmp, 'other'), 'something-else');
    const noSrc = path.join(tmp, 'nosrc');
    fs.mkdirSync(noSrc);
    fs.writeFileSync(path.join(noSrc, 'pyproject.toml'), '[project]\nname = "kiwrap"\n');
    assert.equal(isKiwrapCheckout(good), true);
    assert.equal(isKiwrapCheckout(other), false);
    assert.equal(isKiwrapCheckout(noSrc), false);
    assert.equal(isKiwrapCheckout(path.join(tmp, 'missing')), false);
    assert.equal(isKiwrapCheckout(undefined), false);
  });

  it('another workspace: skips non-checkout folders and falls back to the remembered checkout', () => {
    const repo = makeCheckout(path.join(tmp, 'repo'));
    const userProject = path.join(tmp, 'my-board');
    fs.mkdirSync(userProject);
    const hit = firstCheckout([
      { source: 'setting', dir: '' },
      { source: 'workspace', dir: userProject },
      { source: 'env', dir: undefined },
      { source: 'extension-dev', dir: path.join(tmp, 'extensions') },
      { source: 'remembered', dir: repo },
      { source: 'build-origin', dir: undefined },
    ]);
    assert.deepEqual(hit, { source: 'remembered', dir: path.resolve(repo) });
  });

  it('uses the build origin when nothing was remembered', () => {
    const repo = makeCheckout(path.join(tmp, 'repo'));
    const ext = path.join(tmp, 'ext');
    fs.mkdirSync(ext);
    fs.writeFileSync(path.join(ext, ORIGIN_FILE), JSON.stringify({ checkout: repo, built: 'x' }));
    assert.equal(readBuildOrigin(ext), repo);
    const hit = firstCheckout([
      { source: 'setting', dir: '' },
      { source: 'remembered', dir: undefined },
      { source: 'build-origin', dir: readBuildOrigin(ext) },
    ]);
    assert.deepEqual(hit, { source: 'build-origin', dir: path.resolve(repo) });
  });

  it('a stale remembered / origin path that is no longer a checkout is skipped', () => {
    const moved = path.join(tmp, 'moved-away');
    assert.equal(firstCheckout([{ source: 'remembered', dir: moved }, { source: 'build-origin', dir: moved }]), undefined);
  });

  it('an explicit setting wins as-is (even when wrong) so the failure names that path', () => {
    const repo = makeCheckout(path.join(tmp, 'repo'));
    const wrong = path.join(tmp, 'typo');
    const hit = firstCheckout([
      { source: 'setting', dir: `  ${wrong}  ` },
      { source: 'remembered', dir: repo },
    ]);
    assert.deepEqual(hit, { source: 'setting', dir: path.resolve(wrong) });
  });

  it('the workspace checkout wins over remembered and origin', () => {
    const open = makeCheckout(path.join(tmp, 'open'));
    const older = makeCheckout(path.join(tmp, 'older'));
    const hit = firstCheckout([
      { source: 'setting', dir: '' },
      { source: 'workspace', dir: open },
      { source: 'remembered', dir: older },
    ]);
    assert.deepEqual(hit, { source: 'workspace', dir: path.resolve(open) });
  });

  it('readBuildOrigin tolerates a missing or malformed file', () => {
    assert.equal(readBuildOrigin(undefined), undefined);
    assert.equal(readBuildOrigin(path.join(tmp, 'none')), undefined);
    fs.writeFileSync(path.join(tmp, ORIGIN_FILE), '{not json');
    assert.equal(readBuildOrigin(tmp), undefined);
    fs.writeFileSync(path.join(tmp, ORIGIN_FILE), JSON.stringify({ checkout: 42 }));
    assert.equal(readBuildOrigin(tmp), undefined);
  });
});
