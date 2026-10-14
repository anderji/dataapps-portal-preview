/**
 * "Test as" on the dataset editor: pick a user, optionally add the IdP group ids they would sign
 * in with (the admin app cannot know those), and see what they get from the saved rules. After a
 * save the page bumps `savedVersion` and the last test runs again.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { Alert, Button, Card, ChipEditor, Combobox, Label } from '@keboola/design';
import { Play } from '@keboola/design/icons';
import { errorMessage } from '../../api/client';
import { useDatasetTest, useUsers } from '../../api/hooks';
import type { DatasetTestInput } from '../../api/types';
import { Muted } from '../Code';
import { TestResultView, type LineInfo } from './TestResultView';

type Props = {
  dataset: string;
  /** Changes after every successful save; a test already run re-runs then. */
  savedVersion: number;
  lineInfo: (ruleId: string) => LineInfo | undefined;
};

export function TestPanel({ dataset, savedVersion, lineInfo }: Props) {
  const userId = useId();
  const groupsId = useId();
  const users = useUsers();
  const test = useDatasetTest(dataset);
  const [email, setEmail] = useState('');
  const [groups, setGroups] = useState<string[]>([]);
  const lastInput = useRef<DatasetTestInput | null>(null);
  const userItems = (users.data?.users ?? []).map((u) => u.email);
  const names = new Map((users.data?.users ?? []).map((u) => [u.email, u.display_name ? `${u.display_name} (${u.email})` : u.email]));

  const run = (input: DatasetTestInput) => {
    lastInput.current = input;
    test.mutate(input);
  };

  // Re-run the last test against the freshly saved rules. `test.mutate` is stable per hook.
  const { mutate } = test;
  useEffect(() => {
    if (savedVersion > 0 && lastInput.current) mutate(lastInput.current);
  }, [savedVersion, mutate]);

  return (
    <Card bordered>
      <Card.Header layout="stack">
        <Card.Title as="h2">Test as</Card.Title>
        <Muted>
          See what one person gets from the saved rules: how many rows, why each line applies or not, and their view
          of the data. Unsaved changes are not included - save first.
        </Muted>
      </Card.Header>
      <Card.Content>
        <div className="tw:grid tw:items-end tw:gap-3 tw:md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
          <div className="tw:flex tw:flex-col tw:gap-1">
            <Label htmlFor={userId}>User</Label>
            <Combobox
              id={userId}
              value={email || null}
              items={userItems}
              itemToStringLabel={(id) => names.get(id) ?? id}
              onValueChange={(value) => setEmail(value ?? '')}
              placeholder="Pick a user"
              empty="No matching user"
              isLoading={users.isPending}
            />
          </div>
          <div className="tw:flex tw:flex-col tw:gap-1">
            <Label htmlFor={groupsId}>IdP group ids (optional)</Label>
            <ChipEditor
              id={groupsId}
              value={groups}
              onValueChange={setGroups}
              placeholder="Type an id and press Enter"
              removeLabel={(id) => `Remove group ${id}`}
            />
          </div>
          <Button disabled={!email || test.isPending} isLoading={test.isPending} onClick={() => run({ email, groups })}>
            <Play className="tw:size-4" /> Run test
          </Button>
        </div>
        <Muted>
          IdP groups come from the identity provider at sign-in, so the admin app does not know them. Add the group
          ids (as the provider sends them) to include lines given to IdP groups.
        </Muted>
        {test.isError && (
          <Alert variant="error" title="Could not run the test">
            {errorMessage(test.error)}
          </Alert>
        )}
        {test.data && <TestResultView result={test.data} lineInfo={lineInfo} />}
      </Card.Content>
    </Card>
  );
}
