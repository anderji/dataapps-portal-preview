/** Checkbox per registered app, for the direct / group app-access sections of the editors. */
import { Checkbox, EmptyState } from '@keboola/design';
import type { App } from '../api/types';
import { Code } from './Code';
import { InactiveBadge } from './Badges';

type Props = {
  apps: App[];
  value: string[];
  onChange: (appIds: string[]) => void;
};

export function AppCheckboxList({ apps, value, onChange }: Props) {
  if (apps.length === 0) {
    return (
      <EmptyState variant="compact">
        <EmptyState.Title>No apps registered.</EmptyState.Title>
      </EmptyState>
    );
  }
  const selected = new Set(value);
  const toggle = (appId: string, checked: boolean) => {
    const next = new Set(selected);
    if (checked) next.add(appId);
    else next.delete(appId);
    // Keep the apps' own order so the payload is stable.
    onChange(apps.map((a) => a.app_id).filter((id) => next.has(id)));
  };
  return (
    <div className="tw:flex tw:flex-col tw:gap-2">
      {apps.map((a) => (
        <Checkbox
          key={a.app_id}
          checked={selected.has(a.app_id)}
          onCheckedChange={(checked: boolean) => toggle(a.app_id, checked)}
        >
          <span className="tw:flex tw:items-center tw:gap-2">
            {a.name} <Code muted>{a.app_id}</Code>
            <InactiveBadge active={a.active} />
          </span>
        </Checkbox>
      ))}
    </div>
  );
}
