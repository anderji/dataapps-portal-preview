/**
 * "Column masking": one entry per column. A column without lines is clear for everyone; adding an
 * exception opens its ordered list of "who -> treatment" lines, where the first line naming a
 * person decides what they get and nobody named sees the real value. The last line can be
 * "everyone else".
 */
import { Badge, Button, Select } from '@keboola/design';
import { ArrowDown, ArrowUp, Plus, Trash2 } from '@keboola/design/icons';
import type { DatasetColumn, Treatment } from '../../api/types';
import { Code, Muted } from '../Code';
import {
  COLUMN_KIND_LABEL,
  columnKind,
  move,
  newMaskLine,
  TREATMENT_HINT,
  TREATMENT_LABEL,
  TREATMENTS,
  type DraftMaskLine,
  type NewKey,
} from './model';
import { SubjectPicker } from './SubjectPicker';

const TREATMENT_ITEMS = TREATMENTS.map((t) => ({ value: t, label: TREATMENT_LABEL[t], hint: TREATMENT_HINT[t] }));

function MaskLineEditor({
  column,
  line,
  number,
  isFirst,
  isLast,
  onChange,
  onMove,
  onRemove,
}: {
  column: string;
  line: DraftMaskLine;
  number: number;
  isFirst: boolean;
  isLast: boolean;
  onChange: (line: DraftMaskLine) => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
}) {
  const label = `Mask on ${column}, line ${number}`;
  return (
    <li
      aria-label={label}
      className="tw:grid tw:items-start tw:gap-2 tw:lg:grid-cols-[2rem_minmax(0,1fr)_14rem_auto]"
    >
      <span className="tw:pt-2 tw:text-sm tw:text-muted-foreground">{number}.</span>
      <SubjectPicker
        label={label}
        subject={line.subject}
        everyoneLabel="Everyone else"
        onChange={(subject) => onChange({ ...line, subject })}
      />
      <Select
        aria-label={`${label}: treatment`}
        value={line.treatment}
        items={TREATMENT_ITEMS}
        onValueChange={(treatment) => onChange({ ...line, treatment: treatment as Treatment })}
      />
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
    </li>
  );
}

function ColumnMasks({
  column,
  type,
  lines,
  newKey,
  onChange,
}: {
  column: string;
  type: string | undefined;
  lines: DraftMaskLine[];
  newKey: NewKey;
  onChange: (update: (lines: DraftMaskLine[]) => DraftMaskLine[]) => void;
}) {
  const endsWithEveryone = lines.at(-1)?.subject.type === 'everyone';
  return (
    <section
      aria-label={`Masking of ${column}`}
      className="tw:flex tw:flex-col tw:gap-3 tw:border-b tw:border-border tw:px-4 tw:py-3 tw:last:border-b-0"
    >
      <div className="tw:flex tw:flex-wrap tw:items-center tw:justify-between tw:gap-2">
        <span className="tw:flex tw:flex-wrap tw:items-center tw:gap-2">
          <Code>{column}</Code>
          {type !== undefined ? (
            <span className="tw:text-xs tw:text-muted-foreground">{COLUMN_KIND_LABEL[columnKind(type)]}</span>
          ) : (
            <Badge color="amber">no longer in the dataset</Badge>
          )}
        </span>
        {lines.length === 0 ? (
          <span className="tw:flex tw:items-center tw:gap-2">
            <span className="tw:text-sm tw:text-muted-foreground">Clear for everyone</span>
            <Button variant="outline" size="small" aria-label={`Add exception on ${column}`} onClick={() => onChange(() => [newMaskLine(newKey)])}>
              <Plus className="tw:size-4" /> Add exception
            </Button>
          </span>
        ) : (
          <Badge color="blue">
            {lines.length} {lines.length === 1 ? 'line' : 'lines'}
          </Badge>
        )}
      </div>
      {lines.length > 0 && (
        <>
          <Muted>First matching line wins. Anyone no line names sees the real value.</Muted>
          <ol aria-label={`Mask lines of ${column}`} className="tw:flex tw:flex-col tw:gap-2">
            {lines.map((line, index) => (
              <MaskLineEditor
                key={line.key}
                column={column}
                line={line}
                number={index + 1}
                isFirst={index === 0}
                isLast={index === lines.length - 1}
                onChange={(next) => onChange((current) => current.map((l) => (l.key === line.key ? next : l)))}
                onMove={(delta) => onChange((current) => move(current, current.findIndex((l) => l.key === line.key), delta))}
                onRemove={() => onChange((current) => current.filter((l) => l.key !== line.key))}
              />
            ))}
          </ol>
          <div className="tw:flex tw:flex-wrap tw:gap-2">
            <Button variant="invisible" size="small" aria-label={`Add line on ${column}`} onClick={() => onChange((current) => [...current, newMaskLine(newKey)])}>
              <Plus className="tw:size-4" /> Add line
            </Button>
            {!endsWithEveryone && (
              <Button
                variant="invisible"
                size="small"
                aria-label={`Add everyone else on ${column}`}
                onClick={() => onChange((current) => [...current, newMaskLine(newKey, 'everyone')])}
              >
                <Plus className="tw:size-4" /> Add &quot;everyone else&quot;
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}

type Props = {
  columns: DatasetColumn[];
  masks: Record<string, DraftMaskLine[]>;
  newKey: NewKey;
  onChange: (column: string, update: (lines: DraftMaskLine[]) => DraftMaskLine[]) => void;
};

export function MaskEditor({ columns, masks, newKey, onChange }: Props) {
  const types = new Map(columns.map((c) => [c.name, c.type]));
  // Dataset columns first; a column the catalogue dropped keeps its lines visible so they can be removed.
  const names = [...columns.map((c) => c.name), ...Object.keys(masks).filter((name) => !types.has(name))];
  return (
    <div className="tw:flex tw:flex-col">
      {names.map((name) => (
        <ColumnMasks
          key={name}
          column={name}
          type={types.get(name)}
          lines={masks[name] ?? []}
          newKey={newKey}
          onChange={(update) => onChange(name, update)}
        />
      ))}
    </div>
  );
}
