/**
 * Grants: a user may open an app if granted directly, through an IdP group or through a local group. Grant form
 * (app, subject type, then the group or user), and one table of grants per app with revoke.
 */
import { z } from 'zod';
import {
  Card,
  ComboboxField,
  EmptyState,
  PageHeader,
  PageLayout,
  setSubmitError,
  Table,
  toast,
  useAppForm,
} from '@keboola/design';
import { errorMessage } from '../api/client';
import { useCreateGrant, useGrants, useRevokeGrant } from '../api/hooks';
import type { App, Grant, GrantsResponse, Group, LocalGroupRef, SubjectType } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { InactiveBadge } from '../components/Badges';
import { Code, Muted } from '../components/Code';
import { ConfirmButton } from '../components/ConfirmButton';
import { LoadError, PageSkeleton } from '../components/PageStates';

const SUBJECT_TYPES: { value: SubjectType; label: string }[] = [
  { value: 'group', label: 'IdP group' },
  { value: 'local_group', label: 'local group' },
  { value: 'user', label: 'user' },
];

const appSchema = z.string().min(1, 'Pick an app.');
const subjectSchema = z.string().min(1, 'Pick who gets access.');

function GrantForm({ data }: { data: GrantsResponse }) {
  const create = useCreateGrant();
  const appsById = new Map(data.apps.map((a) => [a.app_id, a]));
  const groupsById = new Map(data.groups.map((g) => [g.group_id, g]));
  const localGroupsById = new Map(data.local_groups.map((g) => [g.local_group_id, g]));
  const form = useAppForm({
    defaultValues: { app_id: '', subject_type: 'group' as SubjectType, subject_id: '' },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await create.mutateAsync(value);
        toast.success(result.message);
        formApi.reset({ ...value, subject_id: '' });
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });

  return (
    <form.AppForm>
      <form.FormBody aria-label="Grant access">
        <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-3">
          <form.AppField name="app_id" validators={{ onSubmit: appSchema }}>
            {() => (
              <ComboboxField
                label="App"
                required
                items={data.apps.map((a) => a.app_id)}
                itemToStringLabel={(id) => {
                  const app = appsById.get(id);
                  return app ? `${app.name} (${app.app_id})` : id;
                }}
                placeholder="Pick an app"
                empty="No matching app"
              />
            )}
          </form.AppField>
          <form.AppField
            name="subject_type"
            listeners={{ onChange: () => form.setFieldValue('subject_id', '') }}
          >
            {(field) => <field.SelectField label="Subject type" required items={SUBJECT_TYPES} />}
          </form.AppField>
          <form.Subscribe selector={(state) => state.values.subject_type}>
            {(subjectType) => (
              <form.AppField name="subject_id" validators={{ onSubmit: subjectSchema }}>
                {() =>
                  subjectType === 'group' ? (
                    <ComboboxField
                      key="group"
                      label="Group"
                      required
                      items={data.groups.map((g) => g.group_id)}
                      itemToStringLabel={(id) => {
                        const group = groupsById.get(id);
                        return group ? `${group.name} (${group.provider})` : id;
                      }}
                      placeholder="Pick a group"
                      empty="No matching group"
                    />
                  ) : subjectType === 'local_group' ? (
                    <ComboboxField
                      key="local_group"
                      label="Local group"
                      required
                      items={data.local_groups.map((g) => g.local_group_id)}
                      itemToStringLabel={(id) => localGroupsById.get(id)?.name ?? id}
                      placeholder="Pick a local group"
                      empty="No matching local group"
                    />
                  ) : (
                    <ComboboxField
                      key="user"
                      label="User"
                      required
                      items={data.users.map((u) => u.email)}
                      placeholder="Pick a user"
                      empty="No matching user"
                    />
                  )
                }
              </form.AppField>
            )}
          </form.Subscribe>
        </div>
        <form.SubmitErrorAlert />
        <div>
          <form.SubmitButton>Grant</form.SubmitButton>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

type SubjectNames = { groups: Map<string, Group>; localGroups: Map<string, LocalGroupRef> };

/** Display name of a grant's subject; users are shown by email, groups by name. */
function subjectNameOf(grant: Grant, names: SubjectNames): string {
  if (grant.subject_type === 'group') return names.groups.get(grant.subject_id)?.name ?? grant.subject_id;
  if (grant.subject_type === 'local_group') {
    return names.localGroups.get(grant.subject_id)?.name ?? grant.subject_id;
  }
  return grant.subject_id;
}

function GrantRow({ grant, names }: { grant: Grant; names: SubjectNames }) {
  const runAction = usePageAction();
  const revoke = useRevokeGrant();
  const subjectName = subjectNameOf(grant, names);
  const subjectLabel = grant.subject_type === 'local_group' ? 'local group' : grant.subject_type;
  const input = { app_id: grant.app_id, subject_type: grant.subject_type, subject_id: grant.subject_id };
  return (
    <Table.Row>
      <Table.Cell>
        <div className="tw:flex tw:flex-col tw:gap-0.5">
          <span>
            {subjectLabel} <b>{subjectName}</b>
          </span>
          <Code muted>
            {grant.subject_type}:{grant.subject_id}
          </Code>
        </div>
      </Table.Cell>
      <Table.Cell>{grant.granted_by}</Table.Cell>
      <Table.Cell>{grant.granted_at}</Table.Cell>
      <Table.Cell className="tw:text-right">
        <ConfirmButton
          label="Revoke"
          ariaLabel={`Revoke ${subjectLabel} ${subjectName} on ${grant.app_id}`}
          title={`Revoke ${subjectLabel} ${subjectName}?`}
          confirmLabel="Revoke grant"
          onConfirm={() => runAction(() => revoke.mutateAsync(input))}
        >
          The {subjectLabel} loses this grant on {grant.app_id}.
        </ConfirmButton>
      </Table.Cell>
    </Table.Row>
  );
}

function AppGrants({ app, grants, names }: { app: App; grants: Grant[]; names: SubjectNames }) {
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="inline">
        <Card.Title as="h2">{app.name}</Card.Title>
        <Code muted>{app.app_id}</Code>
        <InactiveBadge active={app.active} />
      </Card.Header>
      <Card.Content>
        {grants.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No grants: nobody can open this app.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label={`Grants on ${app.app_id}`}>
            <Table.Header>
              <Table.Row>
                <Table.Head>Subject</Table.Head>
                <Table.Head>Granted by</Table.Head>
                <Table.Head>Granted at</Table.Head>
                <Table.Head>
                  <span className="tw:sr-only">Actions</span>
                </Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {grants.map((g) => (
                <GrantRow key={`${g.subject_type}:${g.subject_id}`} grant={g} names={names} />
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}

function GrantsContent({ data }: { data: GrantsResponse }) {
  const names: SubjectNames = {
    groups: new Map(data.groups.map((g) => [g.group_id, g])),
    localGroups: new Map(data.local_groups.map((g) => [g.local_group_id, g])),
  };
  const byApp = new Map<string, Grant[]>(data.apps.map((a) => [a.app_id, []]));
  for (const grant of data.grants) byApp.get(grant.app_id)?.push(grant);
  return (
    <>
      <Card bordered>
        <Card.Header layout="stack">
          <Card.Title as="h2">Grant access</Card.Title>
          <Muted>A user may open an app if they are granted directly, through an IdP group or through a local group.</Muted>
        </Card.Header>
        <Card.Content>
          <GrantForm data={data} />
        </Card.Content>
      </Card>
      {data.apps.length === 0 ? (
        <Card bordered>
          <EmptyState variant="compact">
            <EmptyState.Title>No apps registered.</EmptyState.Title>
          </EmptyState>
        </Card>
      ) : (
        data.apps.map((app) => (
          <AppGrants key={app.app_id} app={app} grants={byApp.get(app.app_id) ?? []} names={names} />
        ))
      )}
    </>
  );
}

export function GrantsPage() {
  const grants = useGrants();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Grants" />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          {grants.isPending ? (
            <PageSkeleton />
          ) : grants.isError ? (
            <LoadError error={grants.error} onRetry={() => void grants.refetch()} />
          ) : (
            <GrantsContent data={grants.data} />
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
