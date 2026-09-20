/** Mocha runner executed inside the Extension Development Host by @vscode/test-electron. */
import * as fs from 'node:fs';
import * as path from 'node:path';
import Mocha from 'mocha';

export function run(): Promise<void> {
  const mocha = new Mocha({ ui: 'bdd', color: true, timeout: 600000, reporter: 'spec' });
  const dir = __dirname;
  for (const f of fs.readdirSync(dir).filter((f) => f.endsWith('.test.js')).sort()) {
    mocha.addFile(path.join(dir, f));
  }
  return new Promise((resolve, reject) => {
    try {
      mocha.run((failures) => (failures > 0 ? reject(new Error(`${failures} tests failed.`)) : resolve()));
    } catch (err) {
      reject(err);
    }
  });
}
