/**
 * "Who sees which rows": the ordered show lines (each adds rows for its people), then the "Always
 * hide rows where" lines (each removes rows for its people). Controlled: the page owns the drafts
 * and the Save button.
 *
 * Lines are numbered across both lists in the order they are saved, which is how the server names
 * a line in an error ("Row line 3: ...").
 */
import { Badge, Button, EmptyState, Select } from '@keboola/design';
import { ArrowDown, ArrowUp, Plus, Trash2 } from '@keboola/design/icons';
import type { DatasetColumn, RowLineKind } from '../../api/types';
import { Muted } from '../Code';
import { ConditionEditor } from './ConditionEditor';
import { emptyCondition, move, newRowLine, type DraftRowLine, type NewKey } from './model';
import { SubjectPicker } from './SubjectPicker';

const ALL_ROWS = 'all';
const SOME_ROWS = 'some';

type Shared = {
  dataset: string;
  columns: DatasetColumn[];
  attributes: string[];
  dataAvailable: boolean;
  newKey: NewKey;
};

type LineProps = Shared & {
  line: DraftRowLine;
  /** 1-based number across show and hide lines. */
  number: number;
  isFirst: boolean;
  isLast: boolean;
  onChange: (line: DraftRowLine) => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
};

function RowLineEditor({ line, number, isFirst, isLast, onChange, onMove, onRemove, newKey, ...ctx }: LineProps) {
  const label = `Row line ${number}`;
  const rowsItems =
    line.kind === 'show'
      ? [
          { value: ALL_ROWS, label: 'sees all rows' },
          { value: SOME_ROWS, label: 'sees only rows where' },
        ]
      : [
          { value: SOME_ROWS, label: 'never sees rows where' },
          { value: ALL_ROWS, label: 'never sees any row' },
        ];

  const setAll = (value: string) => {
    const all = value === ALL_ROWS;
    const conditions = !all && line.conditions.length === 0 ? [emptyCondition(newKey(), ctx.columns[0])] : line.conditions;
    onChange({ ...line, all, conditions });
  };

  return (
    <li
      aria-label={label}
      className="tw:flex tw:flex-col tw:gap-3 tw:rounded-md tw:border tw:border-border tw:bg-background tw:p-4"
    >
      <div className="tw:flex tw:flex-wrap tw:items-center tw:justify-between tw:gap-2">
        <span className="tw:flex tw:items-center tw:gap-2 tw:text-sm tw:font-medium">
          Line {number}
          {line.kind === 'show' ? <Badge color="green">adds rows</Badge> : <Badge color="orange">removes rows</Badge>}
        </span>
        <div className="tw:flex tw:gap-1">
          <Button variant="outline" size="icon-sm" aria-label={`Move ${label.toLowerCase()} up`} disabled={isFirst} onClick={() => onMove(-1)}>
            <ArrowUp className="tw:size-4" />
          </Button>
          <Button variant="outline" size="icon-sm" aria-label={`Move ${label.toLowerCase()} down`} disabled={isLast} onClick={() => onMove(1)}>
            <ArrowDown className="tw:size-4" />
          </Button>
          <Button variant="outline" size="icon-sm" aria-label={`Delete ${label.toLowerCase()}`} onClick={onRemove}>
            <Trash2 className="tw:size-4" />
          </Button>
        </div>
      </div>
      <div className="tw:grid tw:items-start tw:gap-2 tw:lg:grid-cols-[minmax(0,1fr)_14rem]">
        <SubjectPicker label={label} subject={line.subject} onChange={(subject) => onChange({ ...line, subject })} />
        <Select
          aria-label={`${label}: which rows`}
          value={line.all ? ALL_ROWS : SOME_ROWS}
          items={rowsItems}
          onValueChange={setAll}
        />
      </div>
      {!line.all && (
        <div className="tw:flex tw:flex-col tw:gap-2 tw:border-l-2 tw:border-border tw:pl-3">
          {line.conditions.map((condition, index) => (
            <div key={condition.key} className="tw:flex tw:flex-col tw:gap-2">
              {index > 0 && <span className="tw:text-xs tw:font-medium tw:text-muted-foreground">and</span>}
              <ConditionEditor
                {...ctx}
                label={`${label}, condition ${index + 1}`}
                condition={condition}
                onChange={(next) =>
                  onChange({ ...line, conditions: line.conditions.map((c) => (c.key === condition.key ? next : c)) })
                }
                onRemove={() => onChange({ ...line, conditions: line.conditions.filter((c) => c.key !== condition.key) })}
              />
            </div>
          ))}
          {line.conditions.length === 0 && <Muted>No condition yet: add one, or choose all rows.</Muted>}
          <div>
            <Button
              variant="invisible"
              size="small"
              onClick={() => onChange({ ...line, conditions: [...line.conditions, emptyCondition(newKey(), ctx.columns[0])] })}
            >
              <Plus className="tw:size-4" /> Add condition
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

type ListProps = Shared & {
  kind: RowLineKind;
  lines: DraftRowLine[];
  /** Number of the first line of this list (hide lines continue after the show lines). */
  firstNumber: number;
  onChange: (update: (lines: DraftRowLine[]) => DraftRowLine[]) => void;
};

function RowLineList({ kind, lines, firstNumber, onChange, ...shared }: ListProps) {
  const title = kind === 'show' ? 'Show lines' : 'Always hide rows where';
  return (
    <section aria-label={title} className="tw:flex tw:flex-col tw:gap-3">
      <div className="tw:flex tw:flex-col tw:gap-1">
        <h3 className="tw:text-base tw:font-semibold">{title}</h3>
        <Muted>
          {kind === 'show'
            ? 'Each line gives its people rows. A person sees the rows of every line that names them; with no line naming them, they see nothing.'
            : 'Each line takes rows away from its people, whatever the lines above give them. Rows where the compared value is missing are hidden too. If a line compares with a user attribute the person does not have, they see no rows at all.'}
        </Muted>
      </div>
      {lines.length === 0 ? (
        <EmptyState variant="compact">
          <EmptyState.Title>{kind === 'show' ? 'Nobody sees any row yet.' : 'Nothing is always hidden.'}</EmptyState.Title>
        </EmptyState>
      ) : (
        <ol aria-label={title} className="tw:flex tw:flex-col tw:gap-3">
          {lines.map((line, index) => (
            <RowLineEditor
              key={line.key}
              {...shared}
              line={line}
              number={firstNumber + index}
              isFirst={index === 0}
              isLast={index === lines.length - 1}
              onChange={(next) => onChange((current) => current.map((l) => (l.key === line.key ? next : l)))}
              onMove={(delta) => onChange((current) => move(current, current.findIndex((l) => l.key === line.key), delta))}
              onRemove={() => onChange((current) => current.filter((l) => l.key !== line.key))}
            />
          ))}
        </ol>
      )}
      <div>
        <Button
          variant="secondary"
          size="small"
          onClick={() => onChange((current) => [...current, newRowLine(kind, shared.newKey, shared.columns[0])])}
        >
          <Plus className="tw:size-4" /> {kind === 'show' ? 'Add show line' : 'Add hide line'}
        </Button>
      </div>
    </section>
  );
}

type Props = Shared & {
  show: DraftRowLine[];
  hide: DraftRowLine[];
  onShowChange: (update: (lines: DraftRowLine[]) => DraftRowLine[]) => void;
  onHideChange: (update: (lines: DraftRowLine[]) => DraftRowLine[]) => void;
};

export function RowLinesEditor({ show, hide, onShowChange, onHideChange, ...shared }: Props) {
  return (
    <div className="tw:flex tw:flex-col tw:gap-6">
      <RowLineList {...shared} kind="show" lines={show} firstNumber={1} onChange={onShowChange} />
      <RowLineList {...shared} kind="hide" lines={hide} firstNumber={show.length + 1} onChange={onHideChange} />
    </div>
  );
}
