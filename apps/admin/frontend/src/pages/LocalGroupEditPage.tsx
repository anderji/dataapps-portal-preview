/**
 * Edit one local group: name and description, its members (added from the registered users,
 * removed one by one) and the app grants it holds (read-only here; grants are managed on the
 * Grants page).
 */
import { Link as RouterLink, useParams } from 'react-router';
import {
  Breadcrumb,
  Button,
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
import { X } from '@keboola/design/icons';
import { z } from 'zod';
import { errorMessage } from '../api/client';
import {
  useAddLocalGroupMember,
  useGrants,
  useLocalGroup,
  useRemoveLocalGroupMember,
  useUpdateLocalGroup,
  useUsers,
} from '../api/hooks';
import type { LocalGroup } from '../api/types';
import { ActionFeedbackScope, usePageAction } from '../components/actionFeedback';
import { Code, Muted } from '../components/Code';
import { SubjectDatasetLines } from '../components/datasets/SubjectDatasetLines';
import { LoadError, PageSkeleton } from '../components/PageStates';

const FORM_ID = 'local-group-form';
const nameSchema = z.string().trim().min(1, 'Enter a name.');
const memberSchema = z.string().min(1, 'Pick a registered user.');

function DetailsForm({ group }: { group: LocalGroup }) {
  const update = useUpdateLocalGroup();
  const form = useAppForm({
    defaultValues: { name: group.name, description: group.description },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await update.mutateAsync({
          local_group_id: group.local_group_id,
          name: value.name.trim(),
          description: value.description.trim(),
        });
        toast.success(`Local group '${result.name}' saved.`);
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  return (
    <form.AppForm>
      <form.FormBody id={FORM_ID} aria-label="Edit local group">
        <Card bordered>
          <Card.Header layout="stack">
            <Card.Title as="h2">Local group</Card.Title>
            <Muted>
              The ID <Code>{group.local_group_id}</Code> cannot be changed; grants and dataset rules reference it.
            </Muted>
          </Card.Header>
          <Card.Content>
            <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-2">
              <form.AppField name="name" validators={{ onSubmit: nameSchema }}>
                {(field) => <field.TextField label="Name" required />}
              </form.AppField>
              <form.AppField name="description">
                {(field) => <field.TextField label="Description" />}
              </form.AppField>
            </div>
          </Card.Content>
        </Card>
        <form.SubmitErrorAlert />
        <div className="tw:flex tw:gap-2">
          <form.SubmitButton form={FORM_ID}>Save local group</form.SubmitButton>
          <Button asChild variant="secondary">
            <RouterLink to="/local-groups">Cancel</RouterLink>
          </Button>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

function AddMemberForm({ group, candidates }: { group: LocalGroup; candidates: string[] }) {
  const add = useAddLocalGroupMember();
  const form = useAppForm({
    defaultValues: { email: '' },
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await add.mutateAsync({ localGroupId: group.local_group_id, email: value.email });
        toast.success(result.message);
        formApi.reset();
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });
  return (
    <form.AppForm>
      <form.FormBody aria-label="Add member">
        <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-3">
          <form.AppField name="email" validators={{ onSubmit: memberSchema }}>
            {() => (
              <ComboboxField
                label="User"
                required
                items={candidates}
                placeholder="Pick a registered user"
                empty="No matching user"
              />
            )}
          </form.AppField>
        </div>
        <form.SubmitErrorAlert />
        <div>
          <form.SubmitButton>Add member</form.SubmitButton>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

function MemberRow({ localGroupId, email }: { localGroupId: string; email: string }) {
  const runAction = usePageAction();
  const remove = useRemoveLocalGroupMember();
  return (
    <Table.Row>
      <Table.Cell>
        <RouterLink to={`/users/${encodeURIComponent(email)}`}>{email}</RouterLink>
      </Table.Cell>
      <Table.Cell className="tw:text-right">
        <Button
          variant="outline"
          size="small"
          aria-label={`Remove ${email}`}
          disabled={remove.isPending}
          onClick={() => void runAction(() => remove.mutateAsync({ localGroupId, email }))}
        >
          <X className="tw:size-4" /> Remove
        </Button>
      </Table.Cell>
    </Table.Row>
  );
}

function Members({ group }: { group: LocalGroup }) {
  const users = useUsers();
  const members = new Set(group.members);
  const candidates = (users.data?.users ?? []).map((u) => u.email).filter((email) => !members.has(email));
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">Members</Card.Title>
        <Muted>Only registered users can be members. Register a user on the Users page first.</Muted>
      </Card.Header>
      <Card.Content>
        {group.members.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No members.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Members">
            <Table.Header>
              <Table.Row>
                <Table.Head>User</Table.Head>
                <Table.Head>
                  <span className="tw:sr-only">Actions</span>
                </Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {[...group.members].sort().map((email) => (
                <MemberRow key={email} localGroupId={group.local_group_id} email={email} />
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
      <Card.Content padded>
        <AddMemberForm group={group} candidates={candidates} />
      </Card.Content>
    </Card>
  );
}

function GroupGrants({ localGroupId }: { localGroupId: string }) {
  const grants = useGrants();
  const rows = (grants.data?.grants ?? []).filter(
    (g) => g.subject_type === 'local_group' && g.subject_id === localGroupId,
  );
  const appsById = new Map((grants.data?.apps ?? []).map((a) => [a.app_id, a]));
  return (
    <Card bordered padding="none">
      <Card.Header divider layout="stack">
        <Card.Title as="h2">App access</Card.Title>
        <Muted>
          Members may open these apps. Manage grants on the <RouterLink to="/grants">Grants</RouterLink> page.
        </Muted>
      </Card.Header>
      <Card.Content>
        {grants.isPending ? (
          <PageSkeleton />
        ) : grants.isError ? (
          <LoadError error={grants.error} onRetry={() => void grants.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>This group holds no grants.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Group grants">
            <Table.Header>
              <Table.Row>
                <Table.Head>App</Table.Head>
                <Table.Head>Granted by</Table.Head>
                <Table.Head>Granted at</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((g) => (
                <Table.Row key={g.app_id}>
                  <Table.Cell>
                    {appsById.get(g.app_id)?.name ?? g.app_id} <Code muted>{g.app_id}</Code>
                  </Table.Cell>
                  <Table.Cell>{g.granted_by}</Table.Cell>
                  <Table.Cell>{g.granted_at}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}

export function LocalGroupEditPage() {
  const { localGroupId = '' } = useParams();
  const group = useLocalGroup(localGroupId);
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Detail
          breadcrumbs={
            <Breadcrumb>
              <Breadcrumb.List>
                <Breadcrumb.Item>
                  <Breadcrumb.Link asChild>
                    <RouterLink to="/local-groups">All local groups</RouterLink>
                  </Breadcrumb.Link>
                </Breadcrumb.Item>
              </Breadcrumb.List>
            </Breadcrumb>
          }
          title="Edit local group"
          description={group.data?.name}
        />
      </PageLayout.Header>
      <PageLayout.Content>
        <ActionFeedbackScope>
          {group.isPending ? (
            <PageSkeleton />
          ) : group.isError ? (
            <LoadError error={group.error} onRetry={() => void group.refetch()} />
          ) : (
            <>
              <DetailsForm key={group.data.local_group_id} group={group.data} />
              <Members group={group.data} />
              <GroupGrants localGroupId={group.data.local_group_id} />
              <SubjectDatasetLines type="local_group" id={group.data.local_group_id} />
            </>
          )}
        </ActionFeedbackScope>
      </PageLayout.Content>
    </PageLayout>
  );
}
