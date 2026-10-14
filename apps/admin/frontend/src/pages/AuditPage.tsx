/**
 * Audit log: newest first, the last `tail_lines` lines, filterable by event type. Lines are the
 * raw audit JSON objects, so every field is read defensively - the details column differs per
 * event (admin: target; gate: reason, path, matched grants, hostile headers; query: reason,
 * datasets, row count).
 */
import { useSearchParams } from 'react-router';
import { Badge, Button, ButtonGroup, Card, EmptyState, PageHeader, PageLayout, Table } from '@keboola/design';
import { useAudit } from '../api/hooks';
import type { AuditLine } from '../api/types';
import { DecisionBadge } from '../components/Badges';
import { Code } from '../components/Code';
import { LoadError, PageSkeleton } from '../components/PageStates';

const EVENT_PARAM = 'event';

const text = (value: unknown): string => (value === undefined || value === null ? '' : String(value));
const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : []);

function Details({ line }: { line: AuditLine }) {
  if (line.event === 'admin') return <Code>{text(line.target)}</Code>;
  if (line.event === 'gate') {
    const hostile = list(line.hostile_headers);
    return (
      <div className="tw:flex tw:flex-wrap tw:items-center tw:gap-1">
        <Code>{text(line.reason)}</Code> <span>{text(line.path)}</span>
        {list(line.matched_grants).map((m) => (
          <Code key={m}>{m}</Code>
        ))}
        {hostile.length > 0 && <Badge color="red">hostile: {hostile.join(', ')}</Badge>}
      </div>
    );
  }
  if (line.event === 'query') {
    const rowCount = line.row_count;
    return (
      <div className="tw:flex tw:flex-wrap tw:items-center tw:gap-1">
        <Code>{text(line.reason)}</Code> <span>{list(line.datasets).join(', ')}</span>
        {rowCount !== undefined && rowCount !== null && <span>· {text(rowCount)} rows</span>}
      </div>
    );
  }
  return null;
}

function AuditRow({ line }: { line: AuditLine }) {
  return (
    <Table.Row>
      <Table.Cell>
        <Code>{text(line.ts)}</Code>
      </Table.Cell>
      <Table.Cell>{text(line.event)}</Table.Cell>
      <Table.Cell>{text(line.email) || '-'}</Table.Cell>
      <Table.Cell>
        <Code>{line.event === 'admin' ? text(line.action) : text(line.app_id)}</Code>
      </Table.Cell>
      <Table.Cell>
        {typeof line.allowed === 'boolean' ? (
          <DecisionBadge allowed={line.allowed} />
        ) : (
          <span className="tw:text-muted-foreground">-</span>
        )}
      </Table.Cell>
      <Table.Cell>
        <Details line={line} />
      </Table.Cell>
    </Table.Row>
  );
}

export function AuditPage() {
  const [params, setParams] = useSearchParams();
  const event = params.get(EVENT_PARAM) ?? '';
  const audit = useAudit(event);
  const events = audit.data?.events ?? [];
  const select = (next: string) => setParams(next ? { [EVENT_PARAM]: next } : {});

  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index
          title="Audit log"
          description={audit.data ? `Newest first, last ${audit.data.tail_lines} lines.` : undefined}
          headerNav={
            <ButtonGroup role="group" aria-label="Filter by event">
              {['', ...events].map((e) => (
                <Button
                  key={e || 'all'}
                  size="small"
                  variant={e === event ? 'primary' : 'outline'}
                  aria-pressed={e === event}
                  onClick={() => select(e)}
                >
                  {e || 'all'}
                </Button>
              ))}
            </ButtonGroup>
          }
        />
      </PageLayout.Header>
      <PageLayout.Content>
        {audit.isPending ? (
          <PageSkeleton />
        ) : audit.isError ? (
          <LoadError error={audit.error} onRetry={() => void audit.refetch()} />
        ) : (
          <Card bordered padding="none">
            <Card.Content>
              {audit.data.lines.length === 0 ? (
                <EmptyState variant="compact">
                  <EmptyState.Title>No audit lines{event ? ` of type ${event}` : ''}.</EmptyState.Title>
                </EmptyState>
              ) : (
                <Table aria-label="Audit log">
                  <Table.Header>
                    <Table.Row>
                      <Table.Head>Time</Table.Head>
                      <Table.Head>Event</Table.Head>
                      <Table.Head>Who</Table.Head>
                      <Table.Head>App / action</Table.Head>
                      <Table.Head>Decision</Table.Head>
                      <Table.Head>Details</Table.Head>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {audit.data.lines.map((line, index) => (
                      // Lines have no ID; their order is stable within one response.
                      <AuditRow key={`${text(line.ts)}-${index}`} line={line} />
                    ))}
                  </Table.Body>
                </Table>
              )}
            </Card.Content>
          </Card>
        )}
      </PageLayout.Content>
    </PageLayout>
  );
}
