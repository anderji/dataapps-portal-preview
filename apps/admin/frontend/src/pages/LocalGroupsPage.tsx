/**
 * Local groups: flat sets of registered users defined inside the suite, independent of the IdP.
 * Create (the ID is a slug in its own namespace) and a list with edit and delete.
 */
import { Link as RouterLink } from 'react-router';
import { z } from 'zod';
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
import { useCreateLocalGroup, useDeleteLocalGroup, useLocalGroups } from '../api/hooks';
import type { LocalGroup } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { Code, Muted } from '../components/Code';
import { ConfirmButton } from '../components/ConfirmButton';
import { LoadError, PageSkeleton } from '../components/PageStates';

// Same rule as the server: lowercase letters, digits, "-" and "_", 1-64 characters.
const SLUG_PATTERN = /^[a-z0-9_-]{1,64}$/;
const idSchema = z
  .string()
  .trim()
  .regex(SLUG_PATTERN, 'Use 1-64 lowercase letters, digits, "-" or "_".');
const nameSchema = z.string().trim().min(1, 'Enter a name.');

function CreateLocalGroupForm() {
  const create = useCreateLocalGroup();
  const form = useAppForm({
    defaultValues: { local_group_id: '', name: '', description: '' },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await create.mutateAsync({
          local_group_id: value.local_group_id.trim(),
          name: value.name.trim(),
          description: value.description.trim(),
        });
        toast.success(`Local group '${result.local_group_id}' created.`);
        formApi.reset();
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  return (
    <form.AppForm>
      <form.FormBody aria-label="Create a local group">
        <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-3">
          <form.AppField name="local_group_id" validators={{ onSubmit: idSchema }}>
            {(field) => <field.TextField label="Local group ID" required placeholder="sales-managers" />}
          </form.AppField>
          <form.AppField name="name" validators={{ onSubmit: nameSchema }}>
            {(field) => <field.TextField label="Name" required placeholder="Sales managers" />}
          </form.AppField>
          <form.AppField name="description">
            {(field) => <field.TextField label="Description" />}
          </form.AppField>
        </div>
        <form.SubmitErrorAlert />
        <div>
          <form.SubmitButton>Create local group</form.SubmitButton>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

function LocalGroupRow({ group }: { group: LocalGroup }) {
  const runAction = usePageAction();
  const remove = useDeleteLocalGroup();
  return (
    <Table.Row>
      <Table.Cell>{group.name}</Table.Cell>
      <Table.Cell>
        <Code muted>{group.local_group_id}</Code>
      </Table.Cell>
      <Table.Cell>{group.description}</Table.Cell>
      <Table.Cell>{group.member_count}</Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:justify-end tw:gap-2">
          <Button asChild variant="outline" size="small">
            <RouterLink
              to={`/local-groups/${encodeURIComponent(group.local_group_id)}`}
              aria-label={`Edit ${group.name}`}
            >
              <Pencil className="tw:size-4" /> Edit
            </RouterLink>
          </Button>
          <ConfirmButton
            label="Delete"
            ariaLabel={`Delete ${group.name}`}
            title={`Delete local group ${group.name}?`}
            confirmLabel="Delete local group"
            onConfirm={() => runAction(() => remove.mutateAsync(group.local_group_id))}
          >
            Deletes the group, its memberships and its grants. This cannot be undone.
          </ConfirmButton>
        </div>
      </Table.Cell>
    </Table.Row>
  );
}

function LocalGroupsContent({ groups }: { groups: LocalGroup[] }) {
  return (
    <>
      <Card bordered>
        <Card.Header layout="stack">
          <Card.Title as="h2">Create a local group</Card.Title>
          <Muted>
            A local group is a flat set of registered users, defined here and independent of the IdP. It can be
            granted app access and named in dataset rules, next to users and IdP groups.
          </Muted>
        </Card.Header>
        <Card.Content>
          <CreateLocalGroupForm />
        </Card.Content>
      </Card>
      <Card bordered padding="none">
        <Card.Content>
          {groups.length === 0 ? (
            <EmptyState variant="compact">
              <EmptyState.Title>No local groups.</EmptyState.Title>
            </EmptyState>
          ) : (
            <Table aria-label="Local groups">
              <Table.Header>
                <Table.Row>
                  <Table.Head>Name</Table.Head>
                  <Table.Head>Local group ID</Table.Head>
                  <Table.Head>Description</Table.Head>
                  <Table.Head>Members</Table.Head>
                  <Table.Head>
                    <span className="tw:sr-only">Actions</span>
                  </Table.Head>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {groups.map((g) => (
                  <LocalGroupRow key={g.local_group_id} group={g} />
                ))}
              </Table.Body>
            </Table>
          )}
        </Card.Content>
      </Card>
    </>
  );
}

export function LocalGroupsPage() {
  const groups = useLocalGroups();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Local groups" />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          {groups.isPending ? (
            <PageSkeleton />
          ) : groups.isError ? (
            <LoadError error={groups.error} onRetry={() => void groups.refetch()} />
          ) : (
            <LocalGroupsContent groups={groups.data} />
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
