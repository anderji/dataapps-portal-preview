/**
 * Read-only list of the dataset lines that name one local group or IdP group (row lines and mask
 * lines), each linking to the dataset's editor. Shown on the group pages; edits happen in the
 * dataset editor.
 */
import { Link as RouterLink } from 'react-router';
import { Card, EmptyState, Table } from '@keboola/design';
import { useDatasetDetails, useDatasets } from '../../api/hooks';
import type { DatasetDetail, LineSubjectType } from '../../api/types';
import { Code, Muted } from '../Code';
import { LoadError, PageSkeleton } from '../PageStates';
import { describeFilter, TREATMENT_LABEL } from './model';

type Mention = { key: string; dataset: string; where: string; what: string };

function mentionsIn(detail: DatasetDetail, type: LineSubjectType, id: string): Mention[] {
  const name = detail.dataset.name;
  const names = (s: { type: LineSubjectType; id: string }) => s.type === type && s.id === id;
  const ordered = [...detail.rows.filter((r) => r.kind === 'show'), ...detail.rows.filter((r) => r.kind === 'hide')];
  const mentions: Mention[] = [];
  ordered.forEach((line, index) => {
    if (!names(line.subject)) return;
    mentions.push({
      key: `${name}:row:${line.rule_id}`,
      dataset: name,
      where: `Rows, line ${index + 1} (${line.kind === 'show' ? 'adds rows' : 'removes rows'})`,
      what: describeFilter(line.filter, detail.dataset.columns),
    });
  });
  for (const [column, lines] of Object.entries(detail.masks)) {
    lines.forEach((line, index) => {
      if (!names(line.subject)) return;
      mentions.push({
        key: `${name}:mask:${line.mask_id}`,
        dataset: name,
        where: `Masking of ${column}, line ${index + 1}`,
        what: TREATMENT_LABEL[line.treatment],
      });
    });
  }
  return mentions;
}

export function SubjectDatasetLines({ type, id }: { type: Extract<LineSubjectType, 'local_group' | 'group'>; id: string }) {
  const datasets = useDatasets();
  const details = useDatasetDetails((datasets.data?.datasets ?? []).map((d) => d.name));
  const failed = details.find((d) => d.isError);
  const loading = datasets.isPending || details.some((d) => d.isPending);
  const mentions = details.flatMap((d) => (d.data ? mentionsIn(d.data, type, id) : []));
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">Dataset rules naming this group</Card.Title>
        <Muted>Lines in the dataset editors that give or take rows, or mask columns, for this group.</Muted>
      </Card.Header>
      <Card.Content>
        {datasets.isError ? (
          <LoadError error={datasets.error} onRetry={() => void datasets.refetch()} />
        ) : failed ? (
          <LoadError error={failed.error} onRetry={() => void failed.refetch()} />
        ) : loading ? (
          <PageSkeleton />
        ) : mentions.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No dataset rule names this group.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Dataset rules naming this group">
            <Table.Header>
              <Table.Row>
                <Table.Head>Dataset</Table.Head>
                <Table.Head>Where</Table.Head>
                <Table.Head>What</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {mentions.map((m) => (
                <Table.Row key={m.key}>
                  <Table.Cell>
                    <RouterLink to={`/datasets/${encodeURIComponent(m.dataset)}`}>
                      <Code>{m.dataset}</Code>
                    </RouterLink>
                  </Table.Cell>
                  <Table.Cell>{m.where}</Table.Cell>
                  <Table.Cell>{m.what}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}
