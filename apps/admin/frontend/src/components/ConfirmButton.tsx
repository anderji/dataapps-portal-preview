/**
 * Destructive action behind a confirmation: `AlertModal` owns the confirm/cancel buttons and the
 * async state (it stays open with a spinner until `onConfirm` resolves).
 */
import type { ReactNode } from 'react';
import { AlertModal, Button } from '@keboola/design';
import { Trash2 } from '@keboola/design/icons';

type Props = {
  /** Button text, e.g. "Delete". */
  label: string;
  /** Accessible name of the trigger when several share a label, e.g. "Delete sales". */
  ariaLabel?: string;
  title: string;
  confirmLabel: string;
  children: ReactNode;
  onConfirm: () => Promise<void>;
};

export function ConfirmButton({ label, ariaLabel, title, confirmLabel, children, onConfirm }: Props) {
  return (
    <AlertModal onConfirm={onConfirm}>
      <AlertModal.Trigger asChild>
        <Button variant="danger" size="small" aria-label={ariaLabel}>
          <Trash2 className="tw:size-4" /> {label}
        </Button>
      </AlertModal.Trigger>
      <AlertModal.Content variant="danger" title={title} confirmLabel={confirmLabel}>
        {children}
      </AlertModal.Content>
    </AlertModal>
  );
}
