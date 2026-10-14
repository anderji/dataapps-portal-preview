/**
 * Edit one group: provider, external ID and name, plus the apps its members may open, saved
 * together. Grants stay attached when the external ID changes (they reference the internal ID).
 */
import { Link as RouterLink, useParams } from 'react-router';
import {
  Breadcrumb,
  Button,
  Card,
  PageHeader,
  PageLayout,
  setSubmitError,
  toast,
  useAppForm,
} from '@keboola/design';
import { errorMessage } from '../api/client';
import { useGroup, useUpdateGroup } from '../api/hooks';
import type { GroupDetail } from '../api/types';
import { AppCheckboxList } from '../components/AppCheckboxList';
import { Code, Muted } from '../components/Code';
import { SubjectDatasetLines } from '../components/datasets/SubjectDatasetLines';
import { GroupFields, type GroupFormValues } from '../components/GroupFields';
import { LoadError, PageSkeleton } from '../components/PageStates';

const FORM_ID = 'group-form';

function GroupForm({ detail }: { detail: GroupDetail }) {
  const update = useUpdateGroup();
  const { group } = detail;
  const form = useAppForm({
    defaultValues: {
      provider: group.provider,
      external_id: group.external_id,
      name: group.name,
      app_ids: detail.granted,
    } as GroupFormValues,
    canSubmitWhenInvalid: true,
    onSubmit: async ({ value, formApi }) => {
      try {
        const result = await update.mutateAsync({
          group_id: group.group_id,
          provider: value.provider,
          external_id: value.external_id.trim(),
          name: value.name.trim(),
          app_ids: value.app_ids,
        });
        toast.success(result.message);
      } catch (error) {
        setSubmitError(formApi, errorMessage(error));
      }
    },
  });

  return (
    <form.AppForm>
      <form.FormBody id={FORM_ID} aria-label="Edit group">
        <Card bordered>
          <Card.Header layout="stack">
            <Card.Title as="h2">Group</Card.Title>
            <Muted>
              The external ID must be exactly what the IdP puts in the roles header; it is checked against the
              provider&apos;s template. Grants stay attached when you change it. Internal ID{' '}
              <Code>{group.group_id}</Code>.
            </Muted>
          </Card.Header>
          <Card.Content>
            <GroupFields form={form} providers={detail.providers} />
          </Card.Content>
        </Card>
        <Card bordered>
          <Card.Header layout="stack">
            <Card.Title as="h2">App access</Card.Title>
            <Muted>Everyone the IdP reports as a member of this group may open the checked apps.</Muted>
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
          <form.SubmitButton form={FORM_ID}>Save group</form.SubmitButton>
          <Button asChild variant="secondary">
            <RouterLink to="/groups">Cancel</RouterLink>
          </Button>
        </div>
      </form.FormBody>
    </form.AppForm>
  );
}

export function GroupEditPage() {
  const { groupId = '' } = useParams();
  const group = useGroup(groupId);
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Detail
          breadcrumbs={
            <Breadcrumb>
              <Breadcrumb.List>
                <Breadcrumb.Item>
                  <Breadcrumb.Link asChild>
                    <RouterLink to="/groups">All IdP groups</RouterLink>
                  </Breadcrumb.Link>
                </Breadcrumb.Item>
              </Breadcrumb.List>
            </Breadcrumb>
          }
          title="Edit group"
          description={group.data?.group.name}
        />
      </PageLayout.Header>
      <PageLayout.Content>
        {group.isPending ? (
          <PageSkeleton />
        ) : group.isError ? (
          <LoadError error={group.error} onRetry={() => void group.refetch()} />
        ) : (
          <div className="tw:flex tw:flex-col tw:gap-4">
            <GroupForm key={group.data.group.group_id} detail={group.data} />
            <SubjectDatasetLines type="group" id={group.data.group.group_id} />
          </div>
        )}
      </PageLayout.Content>
    </PageLayout>
  );
}
