/**
 * Groups: matched by the identifier the IdP puts in the roles header, not by name. Create (the
 * identifier is checked against the provider's template), provider templates, list with edit and
 * delete.
 */
import { Link as RouterLink } from 'react-router';
import {
  Button,
  Card,
  EmptyState,
  PageHeader,
  PageLayout,
  setSubmitError,
  Table,
  toast,
  useAppForm,
} from '@keboola/design';
import { Pencil } from '@keboola/design/icons';
import { errorMessage } from '../api/client';
import { useCreateGroup, useDeleteGroup, useGroups } from '../api/hooks';
import type { Group, GroupsResponse, Provider } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { Code, Muted } from '../components/Code';
import { ConfirmButton } from '../components/ConfirmButton';
import { GroupFields, type GroupFormValues } from '../components/GroupFields';
import { LoadError, PageSkeleton } from '../components/PageStates';

function CreateGroupForm({ providers }: { providers: Record<string, Provider> }) {
  const create = useCreateGroup();
  const firstProvider = Object.keys(providers)[0] ?? '';
  const form = useAppForm({
    defaultValues: { provider: firstProvider, external_id: '', name: '', app_ids: [] } as GroupFormValues,
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await create.mutateAsync({
          provider: value.provider,
          external_id: value.external_id.trim(),
          name: value.name.trim(),
        });
        toast.success(result.message);
        // Keep the provider: groups are usually added in batches from one IdP.
        formApi.reset({ provider: value.provider, external_id: '', name: '', app_ids: [] });
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  return (
    <form.AppForm>
      <form.FormBody aria-label="Create a group">
        <GroupFields form={form} providers={providers} />
        <form.SubmitErrorAlert />
        <div>
          <form.SubmitButton>Create group</form.SubmitButton>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

function ProviderTemplates({ providers }: { providers: Record<string, Provider> }) {
  return (
    <Table aria-label="Provider templates" bordered>
      <Table.Header>
        <Table.Row>
          <Table.Head>Provider</Table.Head>
          <Table.Head>Pattern</Table.Head>
          <Table.Head>Example</Table.Head>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {Object.entries(providers).map(([key, p]) => (
          <Table.Row key={key}>
            <Table.Cell>
              {p.label} <Code muted>{key}</Code>
            </Table.Cell>
            <Table.Cell>
              <Code>{p.group_id_pattern}</Code>
            </Table.Cell>
            <Table.Cell>
              <Code>{p.example}</Code>
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
}

function GroupRow({ group, providers }: { group: Group; providers: Record<string, Provider> }) {
  const runAction = usePageAction();
  const remove = useDeleteGroup();
  return (
    <Table.Row>
      <Table.Cell>{group.name}</Table.Cell>
      <Table.Cell>{providers[group.provider]?.label ?? group.provider}</Table.Cell>
      <Table.Cell>
        <Code>{group.external_id}</Code>
      </Table.Cell>
      <Table.Cell>
        <Code muted>{group.group_id}</Code>
      </Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:justify-end tw:gap-2">
          <Button asChild variant="outline" size="small">
            <RouterLink to={`/groups/${encodeURIComponent(group.group_id)}`} aria-label={`Edit ${group.name}`}>
              <Pencil className="tw:size-4" /> Edit
            </RouterLink>
          </Button>
          <ConfirmButton
            label="Delete"
            ariaLabel={`Delete ${group.name}`}
            title={`Delete group ${group.name}?`}
            confirmLabel="Delete group"
            onConfirm={() => runAction(() => remove.mutateAsync(group.group_id))}
          >
            Deletes the group and its grants. This cannot be undone.
          </ConfirmButton>
        </div>
      </Table.Cell>
    </Table.Row>
  );
}

function GroupsContent({ data }: { data: GroupsResponse }) {
  return (
    <>
      <Card bordered>
        <Card.Header layout="stack">
          <Card.Title as="h2">Create a group</Card.Title>
          <Muted>
            Groups are matched by the identifier the IdP puts into the roles header, not by name. The identifier is
            checked against the provider&apos;s template.
          </Muted>
        </Card.Header>
        <Card.Content>
          <div className="tw:flex tw:flex-col tw:gap-6">
            <CreateGroupForm providers={data.providers} />
            <div className="tw:flex tw:flex-col tw:gap-2">
              <h3 className="tw:text-base tw:font-semibold">Provider templates</h3>
              <ProviderTemplates providers={data.providers} />
            </div>
          </div>
        </Card.Content>
      </Card>
      <Card bordered padding="none">
        <Card.Content>
          {data.groups.length === 0 ? (
            <EmptyState variant="compact">
              <EmptyState.Title>No groups.</EmptyState.Title>
            </EmptyState>
          ) : (
            <Table aria-label="Groups">
              <Table.Header>
                <Table.Row>
                  <Table.Head>Name</Table.Head>
                  <Table.Head>Provider</Table.Head>
                  <Table.Head>External ID</Table.Head>
                  <Table.Head>Group ID</Table.Head>
                  <Table.Head>
                    <span className="tw:sr-only">Actions</span>
                  </Table.Head>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {data.groups.map((g) => (
                  <GroupRow key={g.group_id} group={g} providers={data.providers} />
                ))}
              </Table.Body>
            </Table>
          )}
        </Card.Content>
      </Card>
    </>
  );
}

export function GroupsPage() {
  const groups = useGroups();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="IdP groups" />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          {groups.isPending ? (
            <PageSkeleton />
          ) : groups.isError ? (
            <LoadError error={groups.error} onRetry={() => void groups.refetch()} />
          ) : (
            <GroupsContent data={groups.data} />
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
