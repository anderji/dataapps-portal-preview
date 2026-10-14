/**
 * Overview: entity counts, the access simulator, and two matrices - what direct user grants allow
 * (each user checked with no groups, since IdP groups are not stored) and which apps each group is
 * granted.
 */
import { Fragment } from 'react';
import {
  Card,
  EmptyState,
  MultiComboboxField,
  PageHeader,
  PageLayout,
  setSubmitError,
  StatCard,
  Table,
  useAppForm,
} from '@keboola/design';
import { errorMessage } from '../api/client';
import { useOverview, useSimulate } from '../api/hooks';
import type { Group, Overview, Simulation } from '../api/types';
import { DecisionBadge, GrantedBadge, InactiveBadge, UnknownGroupBadge } from '../components/Badges';
import { Code, Muted, NameWithId } from '../components/Code';
import { LoadError, PageSkeleton } from '../components/PageStates';

const COUNT_LABELS: { key: keyof Overview['counts']; label: string }[] = [
  { key: 'apps', label: 'Apps' },
  { key: 'users', label: 'Users' },
  { key: 'groups', label: 'IdP groups' },
  { key: 'local_groups', label: 'Local groups' },
  { key: 'grants', label: 'Grants' },
  { key: 'attributes', label: 'Attributes' },
];

function Counts({ counts }: { counts: Overview['counts'] }) {
  return (
    <div className="tw:grid tw:grid-cols-2 tw:gap-4 tw:md:grid-cols-6">
      {COUNT_LABELS.map(({ key, label }) => (
        <StatCard key={key} bordered>
          <StatCard.Label>{label}</StatCard.Label>
          <StatCard.Value>{counts[key]}</StatCard.Value>
        </StatCard>
      ))}
    </div>
  );
}

function SimulationResult({ result }: { result: Simulation }) {
  return (
    <div className="tw:flex tw:flex-col tw:gap-3">
      <h3 className="tw:text-base tw:font-semibold">
        Result for <Code>{result.email}</Code>
      </h3>
      <p className="tw:text-sm tw:text-muted-foreground" data-testid="simulation-groups">
        Groups:{' '}
        {result.groups.length === 0
          ? 'none'
          : result.groups.map((g, index) => (
              <Fragment key={g.external_id}>
                <Code>{g.external_id}</Code> {g.name !== null ? `(${g.name})` : <UnknownGroupBadge />}
                {index < result.groups.length - 1 ? ', ' : ''}
              </Fragment>
            ))}
      </p>
      {result.results.length === 0 ? (
        <EmptyState variant="compact">
          <EmptyState.Title>No apps registered.</EmptyState.Title>
        </EmptyState>
      ) : (
        <Table bordered>
          <Table.Header>
            <Table.Row>
              <Table.Head>App</Table.Head>
              <Table.Head>Decision</Table.Head>
              <Table.Head>Reason</Table.Head>
              <Table.Head>Matched grants</Table.Head>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {result.results.map(({ app, decision }) => (
              <Table.Row key={app.app_id}>
                <Table.Cell>
                  <NameWithId name={app.name} id={app.app_id} />
                </Table.Cell>
                <Table.Cell>
                  <DecisionBadge allowed={decision.allowed} />
                </Table.Cell>
                <Table.Cell>
                  <Code>{decision.reason}</Code>
                </Table.Cell>
                <Table.Cell>
                  {decision.matched.length ? (
                    <div className="tw:flex tw:flex-col tw:gap-1">
                      {decision.matched.map((m) => (
                        <Code key={m}>{m}</Code>
                      ))}
                    </div>
                  ) : (
                    <span className="tw:text-muted-foreground">none</span>
                  )}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table>
      )}
    </div>
  );
}

function Simulator({ groups }: { groups: Group[] }) {
  const simulate = useSimulate();
  // The simulator matches the IdP's identifiers, so the value is the external ID; the label is
  // the stored name, which is also what the search matches.
  const byExternal = new Map(groups.map((g) => [g.external_id, g]));
  const externalIds = [...byExternal.keys()];
  const form = useAppForm({
    defaultValues: { email: '', groups: [] as string[], extra: '' },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        await simulate.mutateAsync(value);
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  const groupLabel = (externalId: string) => {
    const group = byExternal.get(externalId);
    return group ? `${group.name} (${group.provider})` : externalId;
  };

  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2" id="simulator">
          Access simulator
        </Card.Title>
        <Muted>
          IdP groups arrive in the request headers and are not stored, so check a principal here: enter an
          email and the groups the IdP would send, and see the gate decision for every app.
        </Muted>
      </Card.Header>
      <Card.Content padded>
        <div className="tw:flex tw:flex-col tw:gap-6">
          <form.AppForm>
            <form.FormBody aria-label="Access simulator">
              <div className="tw:grid tw:gap-4 tw:md:grid-cols-3">
                <form.AppField name="email">
                  {(field) => (
                    <field.TextField label="Email" type="email" required placeholder="name@company.example" />
                  )}
                </form.AppField>
                <form.AppField name="groups">
                  {() => (
                    <MultiComboboxField
                      label="Groups"
                      items={externalIds}
                      itemToStringLabel={groupLabel}
                      placeholder={externalIds.length ? 'Pick known groups' : 'No groups stored'}
                      empty="No matching group"
                    />
                  )}
                </form.AppField>
                <form.AppField name="extra">
                  {(field) => (
                    <field.TextField
                      label="Other group IDs"
                      help="Comma separated; identifiers not in the groups table."
                      placeholder="not in the groups table"
                    />
                  )}
                </form.AppField>
              </div>
              <form.SubmitErrorAlert />
              <div>
                <form.SubmitButton>Check access</form.SubmitButton>
              </div>
            </form.FormBody>
          </form.AppForm>
          {simulate.data && <SimulationResult result={simulate.data} />}
        </div>
      </Card.Content>
    </Card>
  );
}

function UserMatrix({ data }: { data: Overview }) {
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">User grants</Card.Title>
        <Muted>
          Each user checked with no groups: shows what direct user grants allow. Group grants are below.
        </Muted>
      </Card.Header>
      <Card.Content>
        {data.users.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No users.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="User grants">
            <Table.Header>
              <Table.Row>
                <Table.Head>User</Table.Head>
                {data.apps.map((a) => (
                  <Table.Head key={a.app_id}>{a.name}</Table.Head>
                ))}
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {data.users.map((u) => (
                <Table.Row key={u.email}>
                  <Table.Cell>
                    <NameWithId name={u.display_name} id={u.email} extra={<InactiveBadge active={u.active} />} />
                  </Table.Cell>
                  {data.apps.map((a) => {
                    const decision = data.user_matrix[u.email]?.[a.app_id];
                    return (
                      <Table.Cell key={a.app_id}>
                        {decision ? (
                          <div className="tw:flex tw:flex-col tw:items-start tw:gap-1">
                            <DecisionBadge allowed={decision.allowed} />
                            {decision.allowed ? (
                              decision.matched.map((m) => <Code key={m}>{m}</Code>)
                            ) : (
                              <Code muted>{decision.reason}</Code>
                            )}
                          </div>
                        ) : (
                          <span className="tw:text-muted-foreground">-</span>
                        )}
                      </Table.Cell>
                    );
                  })}
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}

function GroupMatrix({ data }: { data: Overview }) {
  return (
    <Card bordered padding="none">
      <Card.Header divider>
        <Card.Title as="h2">Group grants</Card.Title>
      </Card.Header>
      <Card.Content>
        {data.groups.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No groups.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Group grants">
            <Table.Header>
              <Table.Row>
                <Table.Head>Group</Table.Head>
                {data.apps.map((a) => (
                  <Table.Head key={a.app_id}>{a.name}</Table.Head>
                ))}
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {data.groups.map((g) => {
                const granted = new Set(data.group_grants[g.group_id] ?? []);
                return (
                  <Table.Row key={g.group_id}>
                    <Table.Cell>
                      <NameWithId name={g.name} id={`${g.provider}:${g.external_id}`} />
                    </Table.Cell>
                    {data.apps.map((a) => (
                      <Table.Cell key={a.app_id}>
                        {granted.has(a.app_id) ? (
                          <GrantedBadge />
                        ) : (
                          <span className="tw:text-muted-foreground" aria-label="not granted">
                            ·
                          </span>
                        )}
                      </Table.Cell>
                    ))}
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

export function OverviewPage() {
  const overview = useOverview();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Overview" />
      </PageLayout.Header>
      <PageLayout.Content>
        {overview.isPending ? (
          <PageSkeleton />
        ) : overview.isError ? (
          <LoadError error={overview.error} onRetry={() => void overview.refetch()} />
        ) : (
          <div className="tw:flex tw:flex-col tw:gap-6">
            <Counts counts={overview.data.counts} />
            <Simulator groups={overview.data.groups} />
            <UserMatrix data={overview.data} />
            <GroupMatrix data={overview.data} />
          </div>
        )}
      </PageLayout.Content>
    </PageLayout>
  );
}
