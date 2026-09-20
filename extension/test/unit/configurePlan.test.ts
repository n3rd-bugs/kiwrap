/** .mcp.json writer, CLAUDE.md snippet and the configure plan (pure; fs stubbed through `read`). */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  agentSnippet,
  applyEdits,
  BEGIN_MARK,
  END_MARK,
  mcpServerEntry,
  mergeMcpJson,
  planConfigure,
  upsertSnippet,
} from '../../src/commands/configurePlan';

describe('mergeMcpJson', () => {
  const entry = mcpServerEntry({ workspace: 'C:/work/board', serverProject: 'C:\\src\\kiwrap' });

  it('creates the file with the kiwrap entry', () => {
    const doc = JSON.parse(mergeMcpJson(null, entry));
    assert.deepEqual(doc, {
      mcpServers: { kiwrap: { command: 'uv', args: ['run', '--project', 'C:/src/kiwrap', 'kiwrap', 'serve'], env: {} } },
    });
  });

  it('keeps other servers and other top-level keys, replaces kiwrap', () => {
    const existing = JSON.stringify({ other: 1, mcpServers: { foo: { command: 'x' }, kiwrap: { command: 'old' } } });
    const doc = JSON.parse(mergeMcpJson(existing, entry));
    assert.equal(doc.other, 1);
    assert.deepEqual(doc.mcpServers.foo, { command: 'x' });
    assert.equal(doc.mcpServers.kiwrap.command, 'uv');
  });

  it('replaces invalid JSON', () => {
    const doc = JSON.parse(mergeMcpJson('{not json', entry));
    assert.ok(doc.mcpServers.kiwrap);
  });

  it('honours kiwrap.uvPath (finding 22): the same uv the extension runs, POSIX slashes', () => {
    assert.equal(mcpServerEntry({ workspace: 'C:/work/board', serverProject: 'C:/src/kiwrap' }).command, 'uv');
    assert.equal(mcpServerEntry({ workspace: 'C:/work/board', serverProject: 'C:/src/kiwrap', uvPath: 'uv' }).command, 'uv');
    assert.equal(mcpServerEntry({ workspace: 'C:/work/board', serverProject: 'C:/src/kiwrap', uvPath: ' UV.exe ' }).command, 'uv');
    assert.equal(mcpServerEntry({ workspace: 'C:/work/board', serverProject: 'C:/src/kiwrap', uvPath: 'D:\\tools\\uv.exe' }).command, 'D:/tools/uv.exe');
  });

  it('uses ${CLAUDE_PROJECT_DIR:-.} when the workspace is the kiwrap checkout itself', () => {
    const e = mcpServerEntry({ workspace: 'C:/src/kiwrap', serverProject: 'c:\\src\\kiwrap\\' });
    assert.deepEqual(e.args, ['run', '--project', '${CLAUDE_PROJECT_DIR:-.}', 'kiwrap', 'serve']);
  });
});

describe('upsertSnippet', () => {
  it('creates a CLAUDE.md with the block', () => {
    const text = upsertSnippet(null);
    assert.ok(text.startsWith('# Project notes for agents'));
    assert.ok(text.includes(BEGIN_MARK) && text.includes(END_MARK));
    assert.ok(text.includes('kw_sch_check'));
  });

  it('appends to an existing file once and replaces the block on the second run', () => {
    const once = upsertSnippet('# Mine\n\nkeep this\n');
    assert.ok(once.startsWith('# Mine\n\nkeep this\n'));
    assert.equal(once.split(BEGIN_MARK).length, 2);
    const twice = upsertSnippet(once.replace('kw_sch_check', 'STALE'), agentSnippet());
    assert.equal(twice.split(BEGIN_MARK).length, 2);
    assert.ok(!twice.includes('STALE'));
    assert.equal(twice, once);
  });
});

describe('planConfigure', () => {
  const ws = 'C:/work/board';
  const kw = 'C:/src/kiwrap';
  const skill = path.join(kw, '.claude', 'skills', 'kiwrap-flow', 'SKILL.md');

  it('plans .mcp.json, CLAUDE.md, AGENTS.md (when present) and the skill copy', () => {
    const files = new Map<string, string>([
      [path.join(ws, 'AGENTS.md'), '# agents\n'],
      [skill, '---\nname: kiwrap-flow\n---\nflow\n'],
    ]);
    const edits = planConfigure({ workspace: ws, serverProject: kw, env: { KIWRAP_HOME: 'C:/home' }, read: (f) => files.get(f) ?? null });
    const names = edits.map((e) => path.relative(ws, e.file).replace(/\\/g, '/'));
    assert.deepEqual(names, ['.mcp.json', 'CLAUDE.md', 'AGENTS.md', '.claude/skills/kiwrap-flow/SKILL.md']);
    assert.equal(edits[0].before, null);
    assert.equal(JSON.parse(edits[0].after).mcpServers.kiwrap.env.KIWRAP_HOME, 'C:/home');
    assert.equal(edits[2].before, '# agents\n');
    assert.equal(edits[3].after, files.get(skill));
  });

  it('is a no-op when everything is already in place', () => {
    const first = planConfigure({ workspace: ws, serverProject: kw, read: () => null });
    const files = new Map(first.map((e) => [e.file, e.after]));
    const again = planConfigure({ workspace: ws, serverProject: kw, read: (f) => files.get(f) ?? null });
    assert.deepEqual(again, []);
  });

  it('skips the skill copy inside the kiwrap checkout', () => {
    const edits = planConfigure({ workspace: kw, serverProject: kw, read: () => null });
    assert.ok(!edits.some((e) => e.file.includes('SKILL.md')));
  });

  it('applyEdits writes files and creates directories', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kiwrap-cfg-'));
    try {
      const edits = planConfigure({ workspace: tmp, serverProject: kw, read: () => null });
      applyEdits(edits);
      const mcp = JSON.parse(fs.readFileSync(path.join(tmp, '.mcp.json'), 'utf8'));
      assert.equal(mcp.mcpServers.kiwrap.command, 'uv');
      assert.ok(fs.readFileSync(path.join(tmp, 'CLAUDE.md'), 'utf8').includes(BEGIN_MARK));
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });
});
