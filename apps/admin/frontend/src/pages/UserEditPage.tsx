/**
 * Edit one user: identity (email, display name, active) plus direct app grants saved together,
 * and the user's attributes (update / remove / add). Changing the email moves grants and
 * attributes server-side; the page then moves to the new URL.
 */
import { useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';
import { z } from 'zod';
import {
  Breadcrumb,
  Button,
  Card,
  EmptyState,
  PageHeader,
  PageLayout,
  setSubmitError,
  Table,
  TextInput,
  toast,
  useAppForm,
} from '@keboola/design';
import { X } from '@keboola/design/icons';
import { errorMessage } from '../api/client';
import { useRemoveAttribute, useSetAttribute, useUpdateUser, useUser } from '../api/hooks';
import type { UserDetail } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { AppCheckboxList } from '../components/AppCheckboxList';
import { AttributeForm } from '../components/AttributeForm';
import { Code, Muted } from '../components/Code';
import { UserDatasetAccess } from '../components/datasets/UserDatasetAccess';
import { LoadError, PageSkeleton } from '../components/PageStates';

const FORM_ID = 'user-form';
const emailSchema = z.email('Enter a valid email address.');

function IdentityForm({ detail }: { detail: UserDetail }) {
  const update = useUpdateUser();
  const navigate = useNavigate();
  const { user } = detail;
  const form = useAppForm({
    defaultValues: {
      new_email: user.email,
      display_name: user.display_name,
      active: user.active,
      app_ids: detail.granted,
    },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await update.mutateAsync({ email: user.email, ...value, new_email: value.new_email.trim() });
        toast.success(result.message);
        if (result.email !== user.email) {
          navigate(`/users/${encodeURIComponent(result.email)}`, { replace: true });
        }
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });

  return (
    <form.AppForm>
      <form.FormBody id={FORM_ID} aria-label="Edit user">
        <Card bordered>
          <Card.Header layout="stack">
            <Card.Title as="h2">Identity</Card.Title>
            <Muted>
              Changing the email moves the user&apos;s direct grants and attributes to the new address. A deactivated
              user is refused by every app, even through group grants.
            </Muted>
          </Card.Header>
          <Card.Content>
            <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-2">
              <form.AppField name="new_email" validators={{ onBlur: emailSchema, onSubmit: emailSchema }}>
                {(field) => <field.TextField label="Email" type="email" required />}
              </form.AppField>
              <form.AppField name="display_name">{(field) => <field.TextField label="Display name" />}</form.AppField>
            </div>
            <form.AppField name="active">{(field) => <field.SwitchField>Active</field.SwitchField>}</form.AppField>
          </Card.Content>
        </Card>
        <Card bordered>
          <Card.Header layout="stack">
            <Card.Title as="h2">Direct app access</Card.Title>
            <Muted>
              Apps this user may open as a person, independent of their IdP groups. Group access is set on the group.
            </Muted>
          </Card.Header>
          <Card.Content>
            <form.Field name="app_ids">
              {(field) => (
                <AppCheckboxList apps={detail.apps} value={field.state.value} onChange={field.handleChange} />
              )}
            </form.Field>
          </Card.Content>
        </Card>
        <form.SubmitErrorAlert />
        <div className="tw:flex tw:gap-2">
          <form.SubmitButton form={FORM_ID}>Save user</form.SubmitButton>
          <Button asChild variant="secondary">
            <RouterLink to="/users">Cancel</RouterLink>
          </Button>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

function AttributeRow({ email, attrKey, value }: { email: string; attrKey: string; value: string }) {
  const runAction = usePageAction();
  const setAttribute = useSetAttribute();
  const removeAttribute = useRemoveAttribute();
  const [draft, setDraft] = useState(value);
  return (
    <Table.Row>
      <Table.Cell>
        <Code>{attrKey}</Code>
      </Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:items-center tw:gap-2">
          <TextInput aria-label={`Value of ${attrKey}`} value={draft} onValueChange={setDraft} sizing="small" />
          <Button
            variant="secondary"
            size="small"
            disabled={!draft || setAttribute.isPending}
            isLoading={setAttribute.isPending}
            onClick={() => void runAction(() => setAttribute.mutateAsync({ email, key: attrKey, value: draft }))}
          >
            Update
          </Button>
        </div>
      </Table.Cell>
      <Table.Cell className="tw:text-right">
        <Button
          variant="outline"
          size="small"
          aria-label={`Remove ${attrKey}`}
          onClick={() => void runAction(() => removeAttribute.mutateAsync({ email, key: attrKey }))}
        >
          <X className="tw:size-4" /> Remove
        </Button>
      </Table.Cell>
    </Table.Row>
  );
}

function Attributes({ detail }: { detail: UserDetail }) {
  const email = detail.user.email;
  const attributes = Object.entries(detail.attributes).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">Attributes</Card.Title>
        <Muted>
          Attributes parameterise row-level security (for example <Code>company</Code>).
        </Muted>
      </Card.Header>
      <Card.Content>
        {attributes.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No attributes.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Attributes">
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
                // Keyed on the value too, so a saved change resets the row's draft.
                <AttributeRow key={`${key}=${value}`} email={email} attrKey={key} value={value} />
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
      <Card.Content padded>
        <AttributeForm email={email} submitLabel="Add attribute" />
      </Card.Content>
    </Card>
  );
}

function LocalGroups({ detail }: { detail: UserDetail }) {
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">Local groups</Card.Title>
        <Muted>
          Local groups this user belongs to. Membership is managed on the local group&apos;s page.
        </Muted>
      </Card.Header>
      <Card.Content>
        {detail.local_groups.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>Not in any local group.</EmptyState.Title>
          </EmptyState>
        ) : (
          <ul aria-label="Local groups of this user" className="tw:flex tw:flex-col tw:gap-2 tw:p-4">
            {detail.local_groups.map((g) => (
              <li key={g.local_group_id}>
                <RouterLink to={`/local-groups/${encodeURIComponent(g.local_group_id)}`}>{g.name}</RouterLink>{' '}
                <Code muted>{g.local_group_id}</Code>
              </li>
            ))}
          </ul>
        )}
      </Card.Content>
    </Card>
  );
}

export function UserEditPage() {
  const { email = '' } = useParams();
  const user = useUser(email);
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Detail
          breadcrumbs={
            <Breadcrumb>
              <Breadcrumb.List>
                <Breadcrumb.Item>
                  <Breadcrumb.Link asChild>
                    <RouterLink to="/users">All users</RouterLink>
                  </Breadcrumb.Link>
                </Breadcrumb.Item>
              </Breadcrumb.List>
            </Breadcrumb>
          }
          title="Edit user"
          description={email}
        />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          {user.isPending ? (
            <PageSkeleton />
          ) : user.isError ? (
            <LoadError error={user.error} onRetry={() => void user.refetch()} />
          ) : (
            <>
              <IdentityForm key={user.data.user.email} detail={user.data} />
              <LocalGroups detail={user.data} />
              <Attributes detail={user.data} />
              <UserDatasetAccess email={user.data.user.email} />
            </>
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
