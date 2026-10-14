/**
 * Provider + external ID + name fields shared by the group create form and the group editor.
 * The external ID placeholder follows the selected provider's example, since the ID is checked
 * against that provider's template on the server.
 */
import { z } from 'zod';
import { withForm } from '@keboola/design';
import type { Provider } from '../api/types';

const requiredSchema = (message: string) => z.string().trim().min(1, message);
const externalIdSchema = requiredSchema('Enter the group ID the IdP sends.');
const nameSchema = requiredSchema('Enter a name.');

/**
 * Both group forms share one value shape (TanStack's `withForm` needs the exact type); the create
 * form carries an empty `app_ids` it never sends.
 */
export type GroupFormValues = { provider: string; external_id: string; name: string; app_ids: string[] };

export const GroupFields = withForm({
  defaultValues: { provider: '', external_id: '', name: '', app_ids: [] } as GroupFormValues,
  props: { providers: {} as Record<string, Provider> },
  render: function Render({ form, providers }) {
    const items = Object.entries(providers).map(([key, p]) => ({ value: key, label: p.label }));
    return (
      <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-3">
        <form.AppField name="provider">
          {(field) => <field.SelectField label="Provider" required items={items} placeholder="Select a provider" />}
        </form.AppField>
        <form.Subscribe selector={(state) => state.values.provider}>
          {(provider) => (
            <form.AppField name="external_id" validators={{ onSubmit: externalIdSchema }}>
              {(field) => (
                <field.TextField label="External group ID" required placeholder={providers[provider]?.example ?? ''} />
              )}
            </form.AppField>
          )}
        </form.Subscribe>
        <form.AppField name="name" validators={{ onSubmit: nameSchema }}>
          {(field) => <field.TextField label="Name" required placeholder="sales-managers" />}
        </form.AppField>
      </div>
    );
  },
});
