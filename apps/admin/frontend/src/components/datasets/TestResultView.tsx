/**
 * What one user gets from one dataset, as POST /datasets/{name}/test answers it: rows visible of
 * total, why each line applies or not (warnings highlighted), what happens to every column and a
 * preview of the real rows they would see. The compiled SQL filter stays folded away - the admin
 * does not need to read it.
 */
import { Alert, Badge, Button, Collapsible, Table } from '@keboola/design';
import { ChevronRight } from '@keboola/design/icons';
import type { DatasetTest, Treatment } from '../../api/types';
import { Code, Muted } from '../Code';
import { cellText, isMaskedTreatment, refLabel, TREATMENT_LABEL } from './model';

/** How the editor names a line in the explanation; unknown ids (unsaved, deleted) fall back to the id. */
export type LineInfo = { title: string; description: string };

const TREATMENT_COLOR: Record<Treatment, 'green' | 'amber' | 'orange' | 'red'> = {
  clear: 'green',
  mask_partial: 'amber',
  mask_hash: 'orange',
  hidden: 'red',
};

export function TreatmentBadge({ treatment }: { treatment: Treatment }) {
  return <Badge color={TREATMENT_COLOR[treatment]}>{TREATMENT_LABEL[treatment]}</Badge>;
}

export function DataUnavailableNote() {
  return (
    <Alert variant="info" title="No data to count">
      The admin app cannot read this dataset&apos;s data right now, so row counts and the preview are not shown. Which
      lines apply is still worked out from the rules.
    </Alert>
  );
}

function RowsSummary({ result }: { result: DatasetTest }) {
  if (!result.accessible) {
    return (
      <Alert variant="warning" title="Sees nothing">
        No show line names this user, so the dataset is hidden from them.
      </Alert>
    );
  }
  const { visible, total } = result.rows;
  if (visible === null || total === null) return <p className="tw:text-sm">Can open this dataset.</p>;
  return (
    <p className="tw:text-base">
      Sees <b>{visible}</b> of <b>{total}</b> rows.
    </p>
  );
}

function Principal({ result }: { result: DatasetTest }) {
  const { principal } = result;
  const attributes = Object.entries(principal.attributes);
  return (
    <dl className="tw:grid tw:gap-x-4 tw:gap-y-1 tw:text-sm tw:sm:grid-cols-[10rem_1fr]">
      <dt className="tw:text-muted-foreground">Tested as</dt>
      <dd>{principal.email}</dd>
      <dt className="tw:text-muted-foreground">Local groups</dt>
      <dd>{principal.local_groups.length ? principal.local_groups.map(refLabel).join(', ') : 'none'}</dd>
      <dt className="tw:text-muted-foreground">IdP groups</dt>
      <dd>{principal.idp_groups.length ? principal.idp_groups.map(refLabel).join(', ') : 'none'}</dd>
      <dt className="tw:text-muted-foreground">Attributes</dt>
      <dd>{attributes.length ? attributes.map(([k, v]) => `${k} = ${v}`).join(', ') : 'none'}</dd>
    </dl>
  );
}

function LineExplanation({ result, lineInfo }: { result: DatasetTest; lineInfo: (ruleId: string) => LineInfo | undefined }) {
  if (result.rows.lines.length === 0) return <Muted>This dataset has no row lines.</Muted>;
  return (
    <Table aria-label="Why each line applies">
      <Table.Header>
        <Table.Row>
          <Table.Head>Line</Table.Head>
          <Table.Head>Applies</Table.Head>
          <Table.Head>Why</Table.Head>
          {result.data_available && <Table.Head>Matching rows</Table.Head>}
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {result.rows.lines.map((line) => {
          const info = lineInfo(line.rule_id);
          return (
            <Table.Row key={line.rule_id}>
              <Table.Cell>
                <div className="tw:flex tw:flex-col tw:gap-0.5">
                  <span className="tw:flex tw:items-center tw:gap-2">
                    {info?.title ?? <Code muted>{line.rule_id}</Code>}
                    {line.kind === 'show' ? <Badge color="green">adds rows</Badge> : <Badge color="orange">removes rows</Badge>}
                  </span>
                  {info && <span className="tw:text-xs tw:text-muted-foreground">{info.description}</span>}
                </div>
              </Table.Cell>
              <Table.Cell>{line.applies ? <Badge color="green">yes</Badge> : <Badge color="gray">no</Badge>}</Table.Cell>
              <Table.Cell>
                <div className="tw:flex tw:flex-col tw:gap-2">
                  <span>{line.reason}</span>
                  {line.warning && (
                    <Alert variant="warning" title="Check this line">
                      {line.warning}
                    </Alert>
                  )}
                </div>
              </Table.Cell>
              {result.data_available && <Table.Cell>{line.matched_rows ?? '-'}</Table.Cell>}
            </Table.Row>
          );
        })}
      </Table.Body>
    </Table>
  );
}

function ColumnTreatments({ result }: { result: DatasetTest }) {
  return (
    <Table aria-label="What happens to each column">
      <Table.Header>
        <Table.Row>
          <Table.Head>Column</Table.Head>
          <Table.Head>What they get</Table.Head>
          <Table.Head>Why</Table.Head>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {result.columns.map((column) => (
          <Table.Row key={column.name}>
            <Table.Cell>
              <Code>{column.name}</Code>
            </Table.Cell>
            <Table.Cell>
              <TreatmentBadge treatment={column.treatment} />
            </Table.Cell>
            <Table.Cell>{column.reason}</Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
}

function Preview({ result }: { result: DatasetTest }) {
  const preview = result.preview;
  if (!preview) return null;
  const treatments = new Map(result.columns.map((c) => [c.name, c.treatment]));
  const masked = preview.columns.map((name) => {
    const treatment = treatments.get(name);
    return treatment !== undefined && isMaskedTreatment(treatment);
  });
  return (
    <div className="tw:flex tw:flex-col tw:gap-2">
      <h4 className="tw:text-sm tw:font-semibold">Preview of what they see</h4>
      {preview.rows.length === 0 ? (
        <Muted>No row to show.</Muted>
      ) : (
        <div className="tw:overflow-x-auto">
          <Table aria-label="Preview">
            <Table.Header>
              <Table.Row>
                {preview.columns.map((name, index) => (
                  <Table.Head key={name}>
                    <span className="tw:flex tw:items-center tw:gap-1">
                      {name}
                      {masked[index] && <Badge color="amber">masked</Badge>}
                    </span>
                  </Table.Head>
                ))}
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {preview.rows.map((row, rowIndex) => (
                // Preview rows carry no identity; their position is the key.
                <Table.Row key={rowIndex}>
                  {row.map((value, index) => {
                    const text = cellText(value);
                    return (
                      <Table.Cell key={index}>
                        {text === '' ? (
                          <span className="tw:text-xs tw:text-muted-foreground">empty</span>
                        ) : masked[index] ? (
                          <span data-masked="true" className="tw:rounded-sm tw:bg-muted tw:px-1 tw:font-mono tw:text-xs tw:italic tw:text-muted-foreground">
                            {text}
                          </span>
                        ) : (
                          text
                        )}
                      </Table.Cell>
                    );
                  })}
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        </div>
      )}
      {preview.truncated && <Muted>Only the first rows are shown.</Muted>}
    </div>
  );
}

function TechnicalFilter({ filter }: { filter: string }) {
  return (
    <Collapsible>
      <Collapsible.Trigger asChild>
        <Button variant="invisible" size="small" className="tw:group">
          <ChevronRight className="tw:size-4 tw:transition-transform tw:group-data-[state=open]:rotate-90" />
          Technical details (SQL filter)
        </Button>
      </Collapsible.Trigger>
      <Collapsible.Content>
        <pre className="tw:mt-2 tw:overflow-x-auto tw:rounded-md tw:bg-muted tw:p-3 tw:font-mono tw:text-xs">{filter}</pre>
      </Collapsible.Content>
    </Collapsible>
  );
}

export function TestResultView({
  result,
  lineInfo = () => undefined,
}: {
  result: DatasetTest;
  lineInfo?: (ruleId: string) => LineInfo | undefined;
}) {
  return (
    <div aria-label="Test result" role="region" className="tw:flex tw:flex-col tw:gap-4">
      <RowsSummary result={result} />
      {!result.data_available && <DataUnavailableNote />}
      <Principal result={result} />
      <div className="tw:flex tw:flex-col tw:gap-2">
        <h4 className="tw:text-sm tw:font-semibold">Rows: which lines apply</h4>
        <LineExplanation result={result} lineInfo={lineInfo} />
      </div>
      {result.columns.length > 0 && (
        <div className="tw:flex tw:flex-col tw:gap-2">
          <h4 className="tw:text-sm tw:font-semibold">Columns</h4>
          <ColumnTreatments result={result} />
        </div>
      )}
      <Preview result={result} />
      {result.row_filter && <TechnicalFilter filter={result.row_filter} />}
    </div>
  );
}
