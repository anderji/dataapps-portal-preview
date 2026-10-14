/**
 * Datasets: every dataset of the policy catalogue with how many row lines and masked columns it
 * has, linking to its security editor.
 */
import { Link as RouterLink } from 'react-router';
import { Alert, Badge, Button, Card, EmptyState, PageHeader, PageLayout, Table } from '@keboola/design';
import { Pencil } from '@keboola/design/icons';
import { useDatasets } from '../api/hooks';
import type { DatasetSummary } from '../api/types';
import { Code, Muted } from '../components/Code';
import { LoadError, PageSkeleton } from '../components/PageStates';

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

function DatasetRow({ dataset }: { dataset: DatasetSummary }) {
  const href = `/datasets/${encodeURIComponent(dataset.name)}`;
  const noRows = dataset.show_lines === 0;
  return (
    <Table.Row>
      <Table.Cell>
        <div className="tw:flex tw:flex-col tw:gap-0.5">
          <RouterLink to={href}>
            <Code>{dataset.name}</Code>
          </RouterLink>
          {dataset.description && <span className="tw:text-sm tw:text-muted-foreground">{dataset.description}</span>}
        </div>
      </Table.Cell>
      <Table.Cell>{dataset.columns.length}</Table.Cell>
      <Table.Cell>
        <span className="tw:flex tw:flex-wrap tw:items-center tw:gap-2">
          {plural(dataset.show_lines, 'show line', 'show lines')}
          {noRows && <Badge color="amber">nobody sees rows</Badge>}
        </span>
      </Table.Cell>
      <Table.Cell>{plural(dataset.hide_lines, 'hide line', 'hide lines')}</Table.Cell>
      <Table.Cell>{plural(dataset.masked_columns.length, 'column', 'columns')}</Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:justify-end">
          <Button asChild variant="outline" size="small">
            <RouterLink to={href} aria-label={`Edit ${dataset.name}`}>
              <Pencil className="tw:size-4" /> Edit
            </RouterLink>
          </Button>
        </div>
      </Table.Cell>
    </Table.Row>
  );
}

function DatasetsContent({ datasets, dataAvailable }: { datasets: DatasetSummary[]; dataAvailable: boolean }) {
  return (
    <>
      {!dataAvailable && (
        <Alert variant="info" title="Data not readable right now">
          The admin app cannot read the datasets&apos; data, so the editors cannot suggest values and tests show no row
          counts or preview. Rules can still be edited.
        </Alert>
      )}
      <Card bordered padding="none">
        <Card.Header divider layout="stack">
          <Card.Title as="h2">Datasets</Card.Title>
          <Muted>
            Each dataset has its own rules: who sees which rows, and which columns are masked for whom. Open one to
            edit its rules and test what a user sees.
          </Muted>
        </Card.Header>
        <Card.Content>
          {datasets.length === 0 ? (
            <EmptyState variant="compact">
              <EmptyState.Title>No datasets.</EmptyState.Title>
              <EmptyState.Description>Datasets are declared in the policy file.</EmptyState.Description>
            </EmptyState>
          ) : (
            <Table aria-label="Datasets">
              <Table.Header>
                <Table.Row>
                  <Table.Head>Dataset</Table.Head>
                  <Table.Head>Columns</Table.Head>
                  <Table.Head>Who sees rows</Table.Head>
                  <Table.Head>Always hidden</Table.Head>
                  <Table.Head>Masked</Table.Head>
                  <Table.Head>
                    <span className="tw:sr-only">Actions</span>
                  </Table.Head>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {datasets.map((d) => (
                  <DatasetRow key={d.name} dataset={d} />
                ))}
              </Table.Body>
            </Table>
          )}
        </Card.Content>
      </Card>
    </>
  );
}

export function DatasetsPage() {
  const datasets = useDatasets();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Datasets" />
      </PageLayout.Header>
      <PageLayout.Content>
        {datasets.isPending ? (
          <PageSkeleton />
        ) : datasets.isError ? (
          <LoadError error={datasets.error} onRetry={() => void datasets.refetch()} />
        ) : (
          <DatasetsContent datasets={datasets.data.datasets} dataAvailable={datasets.data.data_available} />
        )}
      </PageLayout.Content>
    </PageLayout>
  );
}
