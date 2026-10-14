import { Link as RouterLink } from 'react-router';
import { Button, EmptyState, PageHeader, PageLayout } from '@keboola/design';

export function NotFoundPage() {
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Page not found" />
      </PageLayout.Header>
      <PageLayout.Content>
        <EmptyState>
          <EmptyState.Title>There is no page at this address.</EmptyState.Title>
          <EmptyState.Actions>
            <Button asChild variant="secondary">
              <RouterLink to="/">Back to the overview</RouterLink>
            </Button>
          </EmptyState.Actions>
        </EmptyState>
      </PageLayout.Content>
    </PageLayout>
  );
}
