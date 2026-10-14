/**
 * One condition of a row line, built from dropdowns: column, operator (only those that make sense
 * for the column's type), what to compare with (a value, or the signed-in user's attribute or
 * email) and the value itself. Values are suggested from the real data when the admin app can read
 * it; any value can still be typed.
 */
import { useRef, useState } from 'react';
import { Button, Combobox, MultiCombobox, Select } from '@keboola/design';
import { X } from '@keboola/design/icons';
import { useDatasetValues } from '../../api/hooks';
import type { ConditionOperator, DatasetColumn } from '../../api/types';
import {
  attributeSource,
  COLUMN_KIND_LABEL,
  columnKind,
  EMAIL_SOURCE,
  FIXED_VALUE,
  isEmptinessOperator,
  isListOperator,
  operatorLabel,
  operatorsFor,
  sourceLabel,
  type ColumnKind,
  type DraftCondition,
} from './model';

const BOOLEAN_VALUES = ['true', 'false'];

type DatasetContext = {
  dataset: string;
  columns: DatasetColumn[];
  attributes: string[];
  dataAvailable: boolean;
};

/**
 * Distinct values of the column. The first list comes unfiltered; when the server says it was
 * cut short, what the admin types narrows it server-side.
 */
function useSuggestions(ctx: DatasetContext, column: string, kind: ColumnKind) {
  const [query, setQuery] = useState('');
  const base = useDatasetValues(ctx.dataset, column, '', ctx.dataAvailable);
  const narrowed = useDatasetValues(ctx.dataset, column, query, ctx.dataAvailable && !!base.data?.truncated && !!query);
  const source = narrowed.data && query ? narrowed : base;
  const fromData = (source.data?.values ?? []).filter((v) => v !== null).map(String);
  const values = kind === 'boolean' ? [...new Set([...BOOLEAN_VALUES, ...fromData])] : fromData;
  return { values, isLoading: base.isFetching || narrowed.isFetching, setQuery };
}

function SingleValueInput({
  label,
  value,
  onChange,
  suggestions,
  isLoading,
  onQueryChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  suggestions: string[];
  isLoading: boolean;
  onQueryChange: (query: string) => void;
}) {
  // The last typed text: leaving the field commits it, so a value never needs the create row.
  const typed = useRef('');
  const items = value && !suggestions.includes(value) ? [value, ...suggestions] : suggestions;
  return (
    <Combobox
      aria-label={label}
      value={value || null}
      items={items}
      allowCreate
      queryToCreateValue={(query) => query}
      queryToCreateLabel={(query) => `Use "${query}"`}
      onValueChange={(next) => onChange(next ?? '')}
      onQueryChange={(query) => {
        typed.current = query;
        onQueryChange(query);
      }}
      onBlur={() => {
        if (typed.current && typed.current !== value) onChange(typed.current);
      }}
      placeholder="Type or pick a value"
      empty="No suggestions - type any value"
      isLoading={isLoading}
    />
  );
}

type Props = DatasetContext & {
  condition: DraftCondition;
  /** Accessible name prefix, e.g. "Row line 1, condition 2". */
  label: string;
  onChange: (condition: DraftCondition) => void;
  onRemove: () => void;
};

export function ConditionEditor({ condition, label, onChange, onRemove, ...ctx }: Props) {
  const column = ctx.columns.find((c) => c.name === condition.column);
  const kind = columnKind(column?.type ?? '');
  const operators = operatorsFor(kind);
  const suggestions = useSuggestions(ctx, condition.column, kind);
  const needsValue = !isEmptinessOperator(condition.op);
  const isList = isListOperator(condition.op);

  const columnItems = ctx.columns.map((c) => ({
    value: c.name,
    label: c.name,
    hint: COLUMN_KIND_LABEL[columnKind(c.type)],
  }));
  if (condition.column && !column) columnItems.push({ value: condition.column, label: condition.column, hint: 'unknown' });

  const sources = [FIXED_VALUE, ...ctx.attributes.map(attributeSource), EMAIL_SOURCE];
  if (!sources.includes(condition.source)) sources.push(condition.source);
  const sourceItems = sources.map((s) => ({ value: s, label: s === FIXED_VALUE ? 'a value' : sourceLabel(s) }));

  const changeColumn = (name: string) => {
    const nextKind = columnKind(ctx.columns.find((c) => c.name === name)?.type ?? '');
    const op = operatorsFor(nextKind).includes(condition.op) ? condition.op : 'is';
    onChange({ ...condition, column: name, op, value: '', values: [] });
  };

  const changeOperator = (op: ConditionOperator) => {
    // A list takes literals only (not a user attribute); keep what carries over.
    const values = isListOperator(op) && !condition.values.length && condition.value ? [condition.value] : condition.values;
    const source = isListOperator(op) ? FIXED_VALUE : condition.source;
    onChange({ ...condition, op, values, source });
  };

  // Wraps instead of squeezing: on a narrow card the value block moves to its own line.
  return (
    <div
      role="group"
      aria-label={label}
      className="tw:flex tw:flex-wrap tw:items-start tw:gap-2"
    >
      <Select
        aria-label={`${label}: column`}
        className="tw:w-40"
        value={condition.column}
        placeholder="Column"
        items={columnItems}
        onValueChange={changeColumn}
      />
      <Select
        aria-label={`${label}: operator`}
        className="tw:w-40"
        value={condition.op}
        items={operators.map((op) => ({ value: op, label: operatorLabel(op, kind) }))}
        onValueChange={(op) => changeOperator(op as ConditionOperator)}
      />
      {needsValue ? (
        <div className="tw:flex tw:min-w-72 tw:flex-1 tw:flex-col tw:gap-2 tw:sm:flex-row">
          {!isList && (
            <Select
              aria-label={`${label}: compare with`}
              className="tw:sm:w-48 tw:sm:shrink-0"
              value={condition.source}
              items={sourceItems}
              onValueChange={(source) => onChange({ ...condition, source })}
            />
          )}
          {isList ? (
            <MultiCombobox
              aria-label={`${label}: values`}
              className="tw:min-w-0 tw:flex-1"
              value={condition.values}
              items={[...new Set([...condition.values, ...suggestions.values])]}
              allowCreate
              queryToCreateValue={(query) => query}
              queryToCreateLabel={(query) => `Add "${query}"`}
              onValueChange={(values) => onChange({ ...condition, values })}
              onQueryChange={suggestions.setQuery}
              placeholder="Type or pick values"
              empty="No suggestions - type any value"
              isLoading={suggestions.isLoading}
            />
          ) : condition.source === FIXED_VALUE ? (
            <div className="tw:min-w-32 tw:flex-1">
              <SingleValueInput
                label={`${label}: value`}
                value={condition.value}
                onChange={(value) => onChange({ ...condition, value })}
                suggestions={suggestions.values}
                isLoading={suggestions.isLoading}
                onQueryChange={suggestions.setQuery}
              />
            </div>
          ) : null}
        </div>
      ) : (
        <span aria-hidden className="tw:flex-1" />
      )}
      <Button variant="outline" size="icon" aria-label={`Remove ${label.toLowerCase()}`} onClick={onRemove}>
        <X className="tw:size-4" />
      </Button>
    </div>
  );
}
