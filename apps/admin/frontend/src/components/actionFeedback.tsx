/**
 * Feedback for one-click actions (toggle, delete, revoke, remove): the server's success message
 * as a toast, a failure as a persistent Alert at the top of the page until dismissed or the next
 * action succeeds.
 *
 * `ActionFeedbackScope` wraps a page's content and renders that Alert; rows deep in a table call
 * `usePageAction()` to run their mutation through it.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { Alert, Button, toast } from '@keboola/design';
import { errorMessage } from '../api/client';

type RunAction = (action: () => Promise<{ message: string }>) => Promise<void>;

type ActionFeedback = { error: string | null; clear: () => void; run: RunAction };

export function useActionFeedback(): ActionFeedback {
  const [error, setError] = useState<string | null>(null);
  const run = useCallback<RunAction>(async (action) => {
    try {
      const result = await action();
      setError(null);
      toast.success(result.message);
    } catch (exc) {
      setError(errorMessage(exc));
    }
  }, []);
  const clear = useCallback(() => setError(null), []);
  return useMemo(() => ({ error, clear, run }), [error, clear, run]);
}

export function ActionErrorAlert({ error, onDismiss }: { error: string | null; onDismiss: () => void }) {
  if (!error) return null;
  return (
    <Alert
      variant="error"
      title="The action failed"
      actions={
        <Button size="small" variant="outline" onClick={onDismiss}>
          Dismiss
        </Button>
      }
    >
      {error}
    </Alert>
  );
}

const ActionFeedbackContext = createContext<RunAction | null>(null);

export function ActionFeedbackScope({ children }: { children: ReactNode }) {
  const { error, clear, run } = useActionFeedback();
  return (
    <ActionFeedbackContext value={run}>
      <div className="tw:flex tw:flex-col tw:gap-4">
        <ActionErrorAlert error={error} onDismiss={clear} />
        {children}
      </div>
    </ActionFeedbackContext>
  );
}

/** Run a mutation with toast-on-success / Alert-on-failure; must render inside ActionFeedbackScope. */
export function usePageAction(): RunAction {
  const run = useContext(ActionFeedbackContext);
  if (!run) throw new Error('usePageAction must be used inside <ActionFeedbackScope>.');
  return run;
}
