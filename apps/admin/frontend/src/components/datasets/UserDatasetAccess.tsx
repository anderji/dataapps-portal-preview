/**
 * "What this user sees" on the user page: the "Test as" result of this user on every dataset,
 * read-only and summarised (can they open it, rows visible, masked / hidden columns, warnings),
 * each linking to the dataset's editor. Runs without IdP groups: the admin app cannot know them.
 */
import { Link as RouterLink } from 'react-router';
import { Alert, Badge, Card, EmptyState, Skeleton, Table } from '@keboola/design';
import { errorMessage } from '../../api/client';
import { useDatasets, useDatasetTestsForUser } from '../../api/hooks';
import type { DatasetTest } from '../../api/types';
import { Code, Muted } from '../Code';
import { LoadError, PageSkeleton } from '../PageStates';
import { isMaskedTreatment } from './model';

function columnsWith(result: DatasetTest, pick: (treatment: DatasetTest['columns'][number]['treatment']) => boolean) {
  return result.columns.filter((c) => pick(c.treatment)).map((c) => c.name);
}

function ResultCells({ result }: { result: DatasetTest }) {
  const masked = columnsWith(result, isMaskedTreatment);
  const hidden = columnsWith(result, (t) => t === 'hidden');
  const warnings = result.rows.lines.filter((l) => l.applies && l.warning).map((l) => l.warning as string);
  const { visible, total } = result.rows;
  return (
    <>
      <Table.Cell>{result.accessible ? <Badge color="green">can open</Badge> : <Badge color="red">sees nothing</Badge>}</Table.Cell>
      <Table.Cell>
        {!result.accessible ? (
          '-'
        ) : visible === null || total === null ? (
          <Muted>no data to count</Muted>
        ) : (
          `${visible} of ${total}`
        )}
      </Table.Cell>
      {/* Column treatments only mean something for a dataset the user can open. */}
      <Table.Cell>{!result.accessible ? '-' : masked.length ? masked.join(', ') : <Muted>none</Muted>}</Table.Cell>
      <Table.Cell>{!result.accessible ? '-' : hidden.length ? hidden.join(', ') : <Muted>none</Muted>}</Table.Cell>
      <Table.Cell>
        {warnings.length === 0 ? (
          <Muted>none</Muted>
        ) : (
          <div className="tw:flex tw:flex-col tw:gap-1">
            {warnings.map((warning) => (
              <Alert key={warning} variant="warning">
                {warning}
              </Alert>
            ))}
          </div>
        )}
      </Table.Cell>
    </>
  );
}

export function UserDatasetAccess({ email }: { email: string }) {
  const datasets = useDatasets();
  const names = (datasets.data?.datasets ?? []).map((d) => d.name);
  const tests = useDatasetTestsForUser(email, names);
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">What this user sees</Card.Title>
        <Muted>
          The result of the saved rules for this user on every dataset, through their own lines and their local
          groups. Lines given to IdP groups are not included here; use Test as on a dataset to add IdP groups.
        </Muted>
      </Card.Header>
      <Card.Content>
        {datasets.isPending ? (
          <PageSkeleton />
        ) : datasets.isError ? (
          <LoadError error={datasets.error} onRetry={() => void datasets.refetch()} />
        ) : names.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No datasets.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="What this user sees">
            <Table.Header>
              <Table.Row>
                <Table.Head>Dataset</Table.Head>
                <Table.Head>Access</Table.Head>
                <Table.Head>Rows visible</Table.Head>
                <Table.Head>Masked columns</Table.Head>
                <Table.Head>Hidden columns</Table.Head>
                <Table.Head>Warnings</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {names.map((name, index) => {
                const test = tests[index];
                return (
                  <Table.Row key={name}>
                    <Table.Cell>
                      <RouterLink to={`/datasets/${encodeURIComponent(name)}`} aria-label={`Open ${name}`}>
                        <Code>{name}</Code>
                      </RouterLink>
                    </Table.Cell>
                    {test?.data ? (
                      <ResultCells result={test.data} />
                    ) : test?.isError ? (
                      <Table.Cell colSpan={5}>
                        <span className="tw:text-sm tw:text-destructive">{errorMessage(test.error)}</span>
                      </Table.Cell>
                    ) : (
                      <Table.Cell colSpan={5}>
                        <Skeleton className="tw:h-4 tw:w-40" />
                      </Table.Cell>
                    )}
                  </Table.Row>
                );
              })}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}
