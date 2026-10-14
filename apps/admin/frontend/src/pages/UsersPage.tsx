/**
 * Users: create, and per user its attributes (set / remove), activate / deactivate, delete, and a
 * link to the full editor.
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
import { Pencil, Power, X } from '@keboola/design/icons';
import { errorMessage } from '../api/client';
import { useCreateUser, useDeleteUser, useRemoveAttribute, useToggleUser, useUsers } from '../api/hooks';
import type { UserWithAttributes } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { AttributeForm } from '../components/AttributeForm';
import { ActiveBadge } from '../components/Badges';
import { Code, Muted } from '../components/Code';
import { ConfirmButton } from '../components/ConfirmButton';
import { LoadError, PageSkeleton } from '../components/PageStates';

const emailSchema = z.email('Enter a valid email address.');

function CreateUserForm() {
  const create = useCreateUser();
  const form = useAppForm({
    defaultValues: { email: '', display_name: '' },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await create.mutateAsync({ email: value.email.trim(), display_name: value.display_name });
        toast.success(result.message);
        formApi.reset();
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  return (
    <Card bordered>
      <Card.Header>
        <Card.Title as="h2">Create a user</Card.Title>
      </Card.Header>
      <Card.Content>
        <form.AppForm>
          <form.FormBody aria-label="Create a user">
            <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-[1fr_1fr_auto]">
              <form.AppField name="email" validators={{ onBlur: emailSchema, onSubmit: emailSchema }}>
                {(field) => <field.TextField label="Email" type="email" required placeholder="name@company.example" />}
              </form.AppField>
              <form.AppField name="display_name">{(field) => <field.TextField label="Display name" />}</form.AppField>
              <div className="tw:md:pt-6">
                <form.SubmitButton>Create user</form.SubmitButton>
              </div>
            </div>
            <form.SubmitErrorAlert />
            <Muted>
              Attributes (for example <Code>company</Code>) parameterise row-level security in the dashboards.
            </Muted>
          </form.FormBody>
        </form.AppForm>
      </Card.Content>
    </Card>
  );
}

function UserCard({ user }: { user: UserWithAttributes }) {
  const runAction = usePageAction();
  const toggle = useToggleUser();
  const remove = useDeleteUser();
  const removeAttribute = useRemoveAttribute();
  const attributes = Object.entries(user.attributes).sort(([a], [b]) => a.localeCompare(b));

  return (
    <Card bordered padding="none" aria-label={user.email}>
      <Card.Header divider>
        <div className="tw:flex tw:flex-wrap tw:items-center tw:gap-2">
          <Card.Title as="h2">{user.display_name || user.email}</Card.Title>
          <Code muted>{user.email}</Code>
          <ActiveBadge active={user.active} />
        </div>
        <div className="tw:flex tw:flex-wrap tw:gap-2">
          <Button asChild variant="outline" size="small">
            <RouterLink to={`/users/${encodeURIComponent(user.email)}`} aria-label={`Edit ${user.email}`}>
              <Pencil className="tw:size-4" /> Edit
            </RouterLink>
          </Button>
          <Button
            variant="outline"
            size="small"
            isLoading={toggle.isPending}
            disabled={toggle.isPending}
            aria-label={`${user.active ? 'Deactivate' : 'Activate'} ${user.email}`}
            onClick={() => void runAction(() => toggle.mutateAsync(user.email))}
          >
            <Power className="tw:size-4" /> {user.active ? 'Deactivate' : 'Activate'}
          </Button>
          <ConfirmButton
            label="Delete"
            ariaLabel={`Delete ${user.email}`}
            title={`Delete ${user.email}?`}
            confirmLabel="Delete user"
            onConfirm={() => runAction(() => remove.mutateAsync(user.email))}
          >
            Deletes the user with their grants and attributes. This cannot be undone.
          </ConfirmButton>
        </div>
      </Card.Header>
      <Card.Content>
        {attributes.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No attributes.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label={`Attributes of ${user.email}`}>
            <Table.Header>
              <Table.Row>
                <Table.Head>Attribute</Table.Head>
                <Table.Head>Value</Table.Head>
                <Table.Head>
                  <span className="tw:sr-only">Actions</span>
                </Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {attributes.map(([key, value]) => (
                <Table.Row key={key}>
                  <Table.Cell>
                    <Code>{key}</Code>
                  </Table.Cell>
                  <Table.Cell>{value}</Table.Cell>
                  <Table.Cell className="tw:text-right">
                    <Button
                      variant="outline"
                      size="small"
                      aria-label={`Remove ${key} from ${user.email}`}
                      onClick={() => void runAction(() => removeAttribute.mutateAsync({ email: user.email, key }))}
                    >
                      <X className="tw:size-4" /> Remove
                    </Button>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
      <Card.Content padded>
        <AttributeForm email={user.email} submitLabel="Set attribute" />
      </Card.Content>
    </Card>
  );
}

export function UsersPage() {
  const users = useUsers();
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Users" />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          <CreateUserForm />
          {users.isPending ? (
            <PageSkeleton />
          ) : users.isError ? (
            <LoadError error={users.error} onRetry={() => void users.refetch()} />
          ) : users.data.users.length === 0 ? (
            <Card bordered>
              <EmptyState variant="compact">
                <EmptyState.Title>No users.</EmptyState.Title>
              </EmptyState>
            </Card>
          ) : (
            users.data.users.map((user) => <UserCard key={user.email} user={user} />)
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
