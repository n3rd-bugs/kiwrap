/** Violation -> diagnostic mapping (pure). */
import * as assert from 'node:assert/strict';
import { countBySeverity, mapViolations, messageOf, UuidIndex } from '../../src/diagnostics/mapping';
import type { Violation } from '../../src/types';
import { FAKE_DRC_VIOLATIONS, FAKE_ERC_VIOLATIONS } from './fakeServer';

const SCH = 'C:/p/demo.kicad_sch';
const SUB = 'C:/p/sub.kicad_sch';
const PCB = 'C:/p/demo.kicad_pcb';

const schText = [
  '(kicad_sch (version 20260830) (generator "eeschema")',
  '  (symbol (lib_id "Device:R") (at 100.33 50.8 0)',
  '    (uuid "11111111-2222-3333-4444-555555555555")',
  '  )',
  '  (symbol (lib_id "power:GND")',
  '    (uuid "FFFFFFFF-0000-1111-2222-333333333333")',
  '  )',
  ')',
].join('\n');
const subText = ['(kicad_sch', '  (symbol (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))', ')'].join('\r\n');
const pcbText = [
  '(kicad_pcb (version 20260901)',
  '  (segment (start 1 1) (end 2 2) (layer "F.Cu") (net 1)',
  '    (uuid "9b19456e-3ad1-4dde-92c4-23451559e1ba"))',
  '  (footprint "R" (uuid "00000000-0000-0000-0000-000000000000")',
  '    (pad "1" (uuid "fa4e4619-2310-496a-9c63-4fb385cf27a0")))',
  ')',
].join('\n');

const texts = new Map([
  [SCH, schText],
  [SUB, subText],
  [PCB, pcbText],
]);

describe('UuidIndex', () => {
  it('finds uuids case-insensitively with 0-based lines, across files in priority order', () => {
    const idx = new UuidIndex(texts);
    assert.deepEqual(idx.find('11111111-2222-3333-4444-555555555555', [SCH, SUB]), { file: SCH, line: 2 });
    assert.deepEqual(idx.find('ffffffff-0000-1111-2222-333333333333', [SCH, SUB]), { file: SCH, line: 5 });
    assert.deepEqual(idx.find('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE', [SCH, SUB]), { file: SUB, line: 1 });
    assert.equal(idx.find('12345678-1234-1234-1234-123456789abc', [SCH, SUB]), null);
  });
});

describe('mapViolations (ERC)', () => {
  const mapped = mapViolations(FAKE_ERC_VIOLATIONS as Violation[], { kind: 'erc', files: [SCH, SUB], texts });

  it('anchors each violation on its first item uuid line in the right sheet file', () => {
    assert.equal(mapped.length, 2);
    assert.equal(mapped[0].file, SCH);
    assert.equal(mapped[0].line, 2);
    assert.equal(mapped[0].severity, 'error');
    assert.equal(mapped[0].code, 'pin_not_connected');
    assert.equal(mapped[0].source, 'kiwrap ERC');
    assert.equal(mapped[0].uuid, '11111111-2222-3333-4444-555555555555');
    assert.deepEqual(mapped[0].posMm, { x: 100.33, y: 50.8 });
    assert.equal(mapped[1].file, SUB);
    assert.equal(mapped[1].line, 1);
    assert.equal(mapped[1].severity, 'warning');
  });

  it('puts the second item into related information, located by its own uuid', () => {
    assert.equal(mapped[0].related.length, 0);
    assert.equal(mapped[1].related.length, 1);
    assert.equal(mapped[1].related[0].file, SCH);
    assert.equal(mapped[1].related[0].line, 5);
    assert.match(mapped[1].related[0].message, /#PWR01 Pin 1 at \(12\.700, 30\.480\) mm/);
  });

  it('message carries type, severity and the mm position', () => {
    assert.equal(
      messageOf(FAKE_ERC_VIOLATIONS[0] as Violation, 'erc'),
      'Pin not connected [pin_not_connected, error]: Symbol R1 Pin 1 at (100.330, 50.800) mm',
    );
  });

  it('falls back to line 1 of the first file when the uuid is not in any file', () => {
    const v: Violation = { ...(FAKE_ERC_VIOLATIONS[0] as Violation), items: [{ uuid: '99999999-9999-9999-9999-999999999999', description: 'x', pos: null }], pos: null };
    const [d] = mapViolations([v], { kind: 'erc', files: [SCH, SUB], texts });
    assert.equal(d.file, SCH);
    assert.equal(d.line, 0);
    assert.equal(d.uuid, null);
    assert.equal(d.posMm, null);
    assert.equal(d.message, 'Pin not connected [pin_not_connected, error]: x');
  });
});

describe('mapViolations (DRC)', () => {
  const mapped = mapViolations(FAKE_DRC_VIOLATIONS as Violation[], { kind: 'drc', files: [PCB], texts });

  it('anchors on the board, classes cosmetic as information, keeps the class in the message', () => {
    assert.equal(mapped[0].file, PCB);
    assert.equal(mapped[0].line, 2);
    assert.equal(mapped[0].severity, 'error');
    assert.match(mapped[0].message, /\[clearance, error, must_fix\]/);
    assert.equal(mapped[0].related[0].line, 4);
    assert.equal(mapped[1].severity, 'information');
    assert.equal(mapped[1].line, 0); // uuid not in the board text -> line 1
    assert.match(mapped[1].message, /\[silk_edge_clearance, warning, cosmetic\]/);
  });

  it('excluded markers become hints; a note is appended', () => {
    const v: Violation = { ...(FAKE_DRC_VIOLATIONS[0] as Violation), severity: 'exclusion', excluded: true, class: 'review', note: 'against a stale zone fill' };
    const [d] = mapViolations([v], { kind: 'drc', files: [PCB], texts });
    assert.equal(d.severity, 'hint');
    assert.match(d.message, /\(against a stale zone fill\)$/);
  });

  it('counts by severity', () => {
    assert.deepEqual(countBySeverity(mapped), { errors: 1, warnings: 0, infos: 1 });
  });
});
