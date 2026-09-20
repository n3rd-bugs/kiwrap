/** Self-write guard of the on-save checks (finding 1: a DRC refill save must not trigger the next DRC). */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { SelfWriteGuard, statStamp, type FileStamp } from '../../src/diagnostics/selfWrite';

describe('SelfWriteGuard', () => {
  it('ignores the stamp our own run left and notices a later change', () => {
    const disk = new Map<string, FileStamp>();
    // a fake fs: like the real one, case / separator insensitive on Windows
    const norm = (f: string) => f.replace(/\\/g, '/').toLowerCase();
    const guard = new SelfWriteGuard((f) => disk.get(norm(f)) ?? null);
    const board = 'c:/p/demo.kicad_pcb';
    assert.equal(guard.isSelfWrite(board), false, 'nothing recorded yet');
    // kicad-cli --save-board rewrote the board during our run; we record right after it
    disk.set(board, { mtimeMs: 1000, size: 500 });
    guard.record([board]);
    assert.equal(guard.isSelfWrite(board), true, 'the watcher event for our own rewrite');
    assert.equal(guard.isSelfWrite('c:\\p\\DEMO.kicad_pcb'), true, 'case / separator insensitive');
    // an agent (or the user) writes the file: a different stamp -> a real change
    disk.set(board, { mtimeMs: 2000, size: 512 });
    assert.equal(guard.isSelfWrite(board), false);
    disk.set(board, { mtimeMs: 1000, size: 501 });
    assert.equal(guard.isSelfWrite(board), false, 'size differs');
    // the next run re-records
    guard.record([board]);
    assert.equal(guard.isSelfWrite(board), true);
    guard.forget(board);
    assert.equal(guard.isSelfWrite(board), false);
    // a file that vanished is dropped from the record
    disk.delete(board);
    guard.record([board]);
    assert.equal(guard.size, 0);
  });

  it('statStamp reads mtime and size from disk, null when missing', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kiwrap-selfwrite-'));
    try {
      const f = path.join(dir, 'x.kicad_pcb');
      fs.writeFileSync(f, '(kicad_pcb)');
      const st = statStamp(f);
      assert.ok(st && st.size === 11 && st.mtimeMs > 0);
      assert.equal(statStamp(path.join(dir, 'missing')), null);
      const guard = new SelfWriteGuard();
      guard.record([f]);
      assert.equal(guard.isSelfWrite(f), true);
      fs.writeFileSync(f, '(kicad_pcb (net 1))');
      assert.equal(guard.isSelfWrite(f), false);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
