/** Loading and load-failure states for a page's primary query. */
import { Button, Card, ErrorState, SkeletonList } from '@keboola/design';
import { RotateCw } from '@keboola/design/icons';
import { errorMessage } from '../api/client';

export function PageSkeleton() {
  return (
    <Card bordered>
      <Card.Content>
        <SkeletonList groups={3} rowHeight="sm" />
      </Card.Content>
    </Card>
  );
}

export function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <ErrorState variant="destructive">
      <ErrorState.Title as="h2">Could not load this page</ErrorState.Title>
      <ErrorState.Description>{errorMessage(error)}</ErrorState.Description>
      <ErrorState.Actions>
        <Button variant="secondary" onClick={onRetry}>
          <RotateCw className="tw:size-4" /> Retry
        </Button>
      </ErrorState.Actions>
    </ErrorState>
  );
}
