/**
 * Editor model of one dataset's security: drafts of row lines and mask lines, the conversion from
 * the GET answer and to the PUT body, and the plain-language wording the pages share.
 *
 * The UI is written for an admin who does not know SQL: operators, treatments and value sources
 * are always shown through the labels here, never as their wire names.
 */
import { config } from '../../config';
import type {
  Condition,
  ConditionOperator,
  DatasetColumn,
  DatasetDetail,
  DatasetSecurityInput,
  LineSubject,
  LineSubjectType,
  MaskLine,
  RowFilter,
  RowLine,
  RowLineKind,
  Treatment,
} from '../../api/types';

// ---- column kinds and operators -------------------------------------------

export type ColumnKind = 'number' | 'date' | 'boolean' | 'text';

export function columnKind(type: string): ColumnKind {
  const kinds = config.datasets.columnKinds;
  if (kinds.number.test(type)) return 'number';
  if (kinds.date.test(type)) return 'date';
  if (kinds.boolean.test(type)) return 'boolean';
  return 'text';
}

export const COLUMN_KIND_LABEL: Record<ColumnKind, string> = {
  number: 'number',
  date: 'date',
  boolean: 'yes / no',
  text: 'text',
};

/** Operators offered per column kind, in menu order (the comparisons need a number or a date). */
const OPERATORS_BY_KIND: Record<ColumnKind, ConditionOperator[]> = {
  text: ['is', 'is_not', 'one_of', 'not_one_of', 'is_empty', 'is_not_empty'],
  number: ['is', 'is_not', 'one_of', 'not_one_of', 'gt', 'gte', 'lt', 'lte', 'is_empty', 'is_not_empty'],
  date: ['is', 'is_not', 'one_of', 'not_one_of', 'gt', 'gte', 'lt', 'lte', 'is_empty', 'is_not_empty'],
  boolean: ['is', 'is_not', 'is_empty', 'is_not_empty'],
};

export function operatorsFor(kind: ColumnKind): ConditionOperator[] {
  return OPERATORS_BY_KIND[kind];
}

const OPERATOR_LABEL: Record<ConditionOperator, string> = {
  is: 'is',
  is_not: 'is not',
  one_of: 'is one of',
  not_one_of: 'is not one of',
  gt: 'is greater than',
  gte: 'is at least',
  lt: 'is less than',
  lte: 'is at most',
  is_empty: 'is empty',
  is_not_empty: 'has a value',
};

/** Dates read as before / after rather than less / greater. */
const DATE_OPERATOR_LABEL: Partial<Record<ConditionOperator, string>> = {
  gt: 'is after',
  gte: 'is on or after',
  lt: 'is before',
  lte: 'is on or before',
};

export function operatorLabel(op: ConditionOperator, kind: ColumnKind = 'text'): string {
  return (kind === 'date' ? DATE_OPERATOR_LABEL[op] : undefined) ?? OPERATOR_LABEL[op];
}

export const isListOperator = (op: ConditionOperator) => op === 'one_of' || op === 'not_one_of';
export const isEmptinessOperator = (op: ConditionOperator) => op === 'is_empty' || op === 'is_not_empty';

// ---- subjects -------------------------------------------------------------

export const SUBJECT_TYPE_LABEL: Record<LineSubjectType, string> = {
  everyone: 'Everyone',
  user: 'A user',
  local_group: 'A local group',
  group: 'An IdP group',
};

/** How a subject reads in a sentence ("Everyone", "user alice@...", "local group Sales"). */
export function describeSubject(subject: { type: LineSubjectType; id: string; label?: string }): string {
  const name = subject.label || subject.id;
  switch (subject.type) {
    case 'everyone':
      return 'Everyone';
    case 'user':
      return `User ${name}`;
    case 'local_group':
      return `Local group ${name}`;
    case 'group':
      return `IdP group ${name}`;
  }
}

// ---- treatments -----------------------------------------------------------

export const TREATMENTS: Treatment[] = ['clear', 'mask_partial', 'mask_hash', 'hidden'];

export const TREATMENT_LABEL: Record<Treatment, string> = {
  clear: 'Sees the real value',
  mask_partial: 'Partially masked',
  mask_hash: 'Replaced by a code (hashed)',
  hidden: 'Column hidden',
};

export const TREATMENT_HINT: Record<Treatment, string> = {
  clear: 'unchanged',
  mask_partial: 'e.g. Jo*** - shows only the start',
  mask_hash: 'same value, same code; the real value cannot be read',
  hidden: 'the column is removed for them',
};

export const isMaskedTreatment = (t: Treatment) => t === 'mask_partial' || t === 'mask_hash';

// ---- value sources --------------------------------------------------------

/** Select value standing for "a fixed value typed or picked by the admin" (no `source`). */
export const FIXED_VALUE = 'fixed';
export const EMAIL_SOURCE = 'email';
const ATTRIBUTE_PREFIX = 'attribute:';

export const attributeSource = (key: string) => `${ATTRIBUTE_PREFIX}${key}`;

export function sourceLabel(source: string): string {
  if (source === EMAIL_SOURCE) return "the user's email";
  if (source.startsWith(ATTRIBUTE_PREFIX)) return `the user's ${source.slice(ATTRIBUTE_PREFIX.length)}`;
  return source;
}

// ---- drafts ---------------------------------------------------------------

export type DraftCondition = {
  /** Local React key; unrelated to anything on the server. */
  key: string;
  column: string;
  op: ConditionOperator;
  /** `FIXED_VALUE`, `email` or `attribute:<key>`. */
  source: string;
  value: string;
  values: string[];
};

export type DraftSubject = { type: LineSubjectType; id: string; label: string };

export type DraftRowLine = {
  key: string;
  rule_id: string | null;
  kind: RowLineKind;
  subject: DraftSubject;
  /** true: every row; false: the rows matching all `conditions`. */
  all: boolean;
  conditions: DraftCondition[];
};

export type DraftMaskLine = { key: string; mask_id: string | null; subject: DraftSubject; treatment: Treatment };

export type DraftSecurity = {
  show: DraftRowLine[];
  hide: DraftRowLine[];
  masks: Record<string, DraftMaskLine[]>;
};

export type NewKey = () => string;

const literalText = (value: string | number | boolean) => String(value);

export function emptyCondition(key: string, column: DatasetColumn | undefined): DraftCondition {
  return { key, column: column?.name ?? '', op: 'is', source: FIXED_VALUE, value: '', values: [] };
}

export function newRowLine(kind: RowLineKind, newKey: NewKey, firstColumn: DatasetColumn | undefined): DraftRowLine {
  return {
    key: newKey(),
    rule_id: null,
    kind,
    subject: { type: 'everyone', id: '', label: '' },
    // A hide line that hides every row is rarely meant, so it starts with a condition.
    all: kind === 'show',
    conditions: kind === 'show' ? [] : [emptyCondition(newKey(), firstColumn)],
  };
}

export function newMaskLine(newKey: NewKey, type: LineSubjectType = 'user'): DraftMaskLine {
  return { key: newKey(), mask_id: null, subject: { type, id: '', label: '' }, treatment: 'mask_partial' };
}

function conditionToDraft(condition: Condition, key: string): DraftCondition {
  return {
    key,
    column: condition.column,
    op: condition.op,
    source: condition.source ?? FIXED_VALUE,
    value: condition.value === undefined ? '' : literalText(condition.value),
    values: (condition.values ?? []).map(literalText),
  };
}

const subjectToDraft = (subject: LineSubject): DraftSubject => ({ ...subject });

function rowLineToDraft(line: RowLine, newKey: NewKey): DraftRowLine {
  const all = 'all' in line.filter;
  return {
    key: newKey(),
    rule_id: line.rule_id,
    kind: line.kind,
    subject: subjectToDraft(line.subject),
    all,
    conditions: 'conditions' in line.filter ? line.filter.conditions.map((c) => conditionToDraft(c, newKey())) : [],
  };
}

function maskLineToDraft(line: MaskLine, newKey: NewKey): DraftMaskLine {
  return { key: newKey(), mask_id: line.mask_id, subject: subjectToDraft(line.subject), treatment: line.treatment };
}

/** Drafts from the GET / PUT answer; row lines are split by kind, keeping the server order. */
export function toDrafts(detail: Pick<DatasetDetail, 'rows' | 'masks'>, newKey: NewKey): DraftSecurity {
  const masks: Record<string, DraftMaskLine[]> = {};
  for (const [column, lines] of Object.entries(detail.masks)) {
    if (lines.length) masks[column] = lines.map((line) => maskLineToDraft(line, newKey));
  }
  return {
    show: detail.rows.filter((r) => r.kind === 'show').map((r) => rowLineToDraft(r, newKey)),
    hide: detail.rows.filter((r) => r.kind === 'hide').map((r) => rowLineToDraft(r, newKey)),
    masks,
  };
}

/** One condition as the filter JSON carries it: exactly one of value / values / source. */
export function conditionToInput(condition: DraftCondition): Condition {
  const base = { column: condition.column, op: condition.op };
  if (isEmptinessOperator(condition.op)) return base;
  if (isListOperator(condition.op)) return { ...base, values: condition.values };
  if (condition.source !== FIXED_VALUE) return { ...base, source: condition.source };
  return { ...base, value: condition.value };
}

export function filterToInput(line: Pick<DraftRowLine, 'all' | 'conditions'>): RowFilter {
  return line.all ? { all: true } : { conditions: line.conditions.map(conditionToInput) };
}

const subjectToInput = (subject: DraftSubject) => ({
  type: subject.type,
  id: subject.type === 'everyone' ? '' : subject.id,
});

/**
 * The PUT body: show lines then hide lines (each in editor order), and per column its mask lines.
 * Ids are sent only for lines that exist on the server; columns without lines are left out.
 */
export function toInput(draft: DraftSecurity): DatasetSecurityInput {
  const rows = [...draft.show, ...draft.hide].map((line) => ({
    ...(line.rule_id ? { rule_id: line.rule_id } : {}),
    kind: line.kind,
    subject: subjectToInput(line.subject),
    filter: filterToInput(line),
  }));
  const masks: DatasetSecurityInput['masks'] = {};
  for (const [column, lines] of Object.entries(draft.masks)) {
    if (!lines.length) continue;
    masks[column] = lines.map((line) => ({
      ...(line.mask_id ? { mask_id: line.mask_id } : {}),
      subject: subjectToInput(line.subject),
      treatment: line.treatment,
    }));
  }
  return { rows, masks };
}

// ---- list helpers ---------------------------------------------------------

/** `items` with the entry at `index` moved by `delta` (-1 up, +1 down); unchanged at the ends. */
export function move<T>(items: T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

// ---- read-only wording (user page, group pages, test panel) ---------------

function describeCondition(condition: Condition, kind: ColumnKind): string {
  const op = operatorLabel(condition.op, kind);
  if (isEmptinessOperator(condition.op)) return `${condition.column} ${op}`;
  if (isListOperator(condition.op)) return `${condition.column} ${op} ${(condition.values ?? []).map(literalText).join(', ')}`;
  if (condition.source) return `${condition.column} ${op} ${sourceLabel(condition.source)}`;
  return `${condition.column} ${op} ${condition.value === undefined ? '' : literalText(condition.value)}`;
}

/** "All rows" or "company is PHARMA_A and region is the user's region". */
export function describeFilter(filter: RowFilter, columns: DatasetColumn[] = []): string {
  if ('all' in filter) return 'All rows';
  if (filter.conditions.length === 0) return 'No condition';
  const kinds = new Map(columns.map((c) => [c.name, columnKind(c.type)]));
  return filter.conditions.map((c) => describeCondition(c, kinds.get(c.column) ?? 'text')).join(' and ');
}

/** A principal's group reference or a skipped example line, whatever shape the server sends. */
export function refLabel(ref: unknown): string {
  if (typeof ref === 'string') return ref;
  if (ref && typeof ref === 'object') {
    const record = ref as Record<string, unknown>;
    for (const key of ['label', 'name', 'reason', 'id', 'external_id', 'local_group_id', 'group_id']) {
      if (typeof record[key] === 'string' && record[key]) return record[key];
    }
    return JSON.stringify(ref);
  }
  return String(ref);
}

/** A preview cell as text; null reads as an explicit "empty". */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
