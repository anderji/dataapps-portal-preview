import { describe, expect, it } from 'vitest';
import { DRUG_SALES } from '../../test/fixtures';
import { datasetDetailSchema } from '../../api/types';
import {
  cellText,
  columnKind,
  conditionToInput,
  describeFilter,
  move,
  operatorLabel,
  operatorsFor,
  refLabel,
  toDrafts,
  toInput,
  type DraftCondition,
} from './model';

const keys = () => {
  let n = 0;
  return () => `k${n++}`;
};

const condition = (patch: Partial<DraftCondition>): DraftCondition => ({
  key: 'c',
  column: 'company',
  op: 'is',
  source: 'fixed',
  value: '',
  values: [],
  ...patch,
});

describe('dataset security model', () => {
  it('reads column kinds from warehouse types', () => {
    expect(columnKind('NUMBER(38,2)')).toBe('number');
    expect(columnKind('BIGINT')).toBe('number');
    expect(columnKind('TIMESTAMP_NTZ')).toBe('date');
    expect(columnKind('BOOLEAN')).toBe('boolean');
    expect(columnKind('VARCHAR')).toBe('text');
    expect(columnKind('')).toBe('text');
  });

  it('limits operators by column kind and words them plainly', () => {
    expect(operatorsFor('text')).not.toContain('gt');
    expect(operatorsFor('boolean')).toEqual(['is', 'is_not', 'is_empty', 'is_not_empty']);
    expect(operatorsFor('date')).toContain('lte');
    expect(operatorLabel('gt', 'number')).toBe('is greater than');
    expect(operatorLabel('gt', 'date')).toBe('is after');
    expect(operatorLabel('is_not_empty')).toBe('has a value');
  });

  it('sends exactly one of value / values / source per condition', () => {
    expect(conditionToInput(condition({ value: 'A', values: ['x'] }))).toEqual({ column: 'company', op: 'is', value: 'A' });
    expect(conditionToInput(condition({ source: 'email', value: 'stale' }))).toEqual({
      column: 'company',
      op: 'is',
      source: 'email',
    });
    expect(conditionToInput(condition({ op: 'one_of', values: ['A', 'B'], source: 'email' }))).toEqual({
      column: 'company',
      op: 'one_of',
      values: ['A', 'B'],
    });
    expect(conditionToInput(condition({ op: 'is_empty', value: 'x' }))).toEqual({ column: 'company', op: 'is_empty' });
  });

  it('round-trips the GET answer into the PUT body (show lines first, labels and empty columns dropped)', () => {
    const detail = datasetDetailSchema.parse({
      ...DRUG_SALES,
      rows: [DRUG_SALES.rows[2], DRUG_SALES.rows[0], DRUG_SALES.rows[1]],
      masks: { ...DRUG_SALES.masks, drug: [] },
    });
    const body = toInput(toDrafts(detail, keys()));
    expect(body.rows.map((r) => r.rule_id)).toEqual(['aaaaaaaaaaa1', 'aaaaaaaaaaa2', 'aaaaaaaaaaa3']);
    expect(body.rows[1]).toEqual({
      rule_id: 'aaaaaaaaaaa2',
      kind: 'show',
      subject: { type: 'local_group', id: 'sales-managers' },
      filter: { all: true },
    });
    expect(Object.keys(body.masks)).toEqual(['revenue']);
    expect(body.masks.revenue[0]).toEqual({ mask_id: 'bbbbbbbbbbb1', subject: { type: 'group', id: 'g1' }, treatment: 'clear' });
  });

  it('keeps literal types readable as text in drafts', () => {
    const detail = datasetDetailSchema.parse({
      ...DRUG_SALES,
      rows: [
        {
          rule_id: 'r1',
          kind: 'show',
          subject: { type: 'everyone', id: '', label: '' },
          filter: { conditions: [{ column: 'revenue', op: 'gt', value: 100 }] },
        },
      ],
    });
    expect(toDrafts(detail, keys()).show[0].conditions[0].value).toBe('100');
  });

  it('moves list entries within bounds', () => {
    expect(move([1, 2, 3], 0, 1)).toEqual([2, 1, 3]);
    expect(move([1, 2, 3], 2, -1)).toEqual([1, 3, 2]);
    const same = [1, 2];
    expect(move(same, 0, -1)).toBe(same);
  });

  it('describes filters and references in plain words', () => {
    expect(describeFilter({ all: true })).toBe('All rows');
    expect(
      describeFilter(
        {
          conditions: [
            { column: 'company', op: 'is', source: 'attribute:company' },
            { column: 'drug', op: 'not_one_of', values: ['A', 'B'] },
            { column: 'sold_on', op: 'lt', value: '2026-01-01' },
          ],
        },
        [{ name: 'sold_on', type: 'DATE' }],
      ),
    ).toBe("company is the user's company and drug is not one of A, B and sold_on is before 2026-01-01");
    expect(refLabel('g1')).toBe('g1');
    expect(refLabel({ id: 'x', name: 'Sales' })).toBe('Sales');
    expect(cellText(null)).toBe('');
    expect(cellText(12)).toBe('12');
  });
});
