import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { APP_HR, APP_SALES, GROUP_SALES, USER_ALICE } from '../test/fixtures';
import { mockFetch, renderApp, SESSION } from '../test/utils';

const PROVIDERS = {
  entra: { label: 'Microsoft Entra ID', group_id_pattern: '^[0-9a-f-]{36}$', example: '00000000-0000-0000-0000-000000000000' },
  okta: { label: 'Okta', group_id_pattern: '^[A-Za-z0-9]+$', example: '00g1abcd' },
};

describe('GroupsPage', () => {
  it('creates a group with the first provider and its example as placeholder', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/groups': { body: { groups: [GROUP_SALES], providers: PROVIDERS } },
      'POST /api/groups': { body: { message: "Group 'ops' created." } },
    });
    const user = userEvent.setup();
    renderApp('/groups');
    const form = await screen.findByRole('form', { name: 'Create a group' });
    const externalId = within(form).getByLabelText(/External group ID/);
    expect(externalId).toHaveAttribute('placeholder', PROVIDERS.entra.example);
    await user.type(externalId, 'abc');
    await user.type(within(form).getByLabelText(/^Name/), 'ops');
    await user.click(within(form).getByRole('button', { name: 'Create group' }));
    expect(await screen.findByText("Group 'ops' created.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ provider: 'entra', external_id: 'abc', name: 'ops' });
    // Provider templates and the list show labels.
    expect(screen.getByRole('table', { name: 'Provider templates' })).toHaveTextContent('^[A-Za-z0-9]+$');
    expect(screen.getByRole('table', { name: 'Groups' })).toHaveTextContent('Microsoft Entra ID');
  });

  it('saves the group editor with the checked apps', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/groups/g1': {
        body: { group: GROUP_SALES, providers: PROVIDERS, apps: [APP_HR, APP_SALES], granted: [] },
      },
      'PUT /api/groups/g1': { body: { message: "Group 'Sales team' saved." } },
    });
    const user = userEvent.setup();
    renderApp('/groups/g1');
    const form = await screen.findByRole('form', { name: 'Edit group' });
    await user.click(within(form).getByRole('checkbox', { name: /Sales dashboard/ }));
    await user.click(within(form).getByRole('button', { name: 'Save group' }));
    expect(await screen.findByText("Group 'Sales team' saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      provider: 'entra',
      external_id: 'sales-team',
      name: 'Sales team',
      app_ids: ['sales'],
    });
  });
});

describe('GrantsPage', () => {
  it('lists grants per app and revokes one', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/grants': {
        body: {
          apps: [APP_HR, APP_SALES],
          users: [USER_ALICE],
          groups: [GROUP_SALES],
          grants: [
            { app_id: 'sales', subject_type: 'group', subject_id: 'g1', granted_by: 'admin@example.com', granted_at: '2026-09-01' },
          ],
        },
      },
      'POST /api/grants/revoke': { body: { message: 'Grant revoked.' } },
    });
    const user = userEvent.setup();
    renderApp('/grants');
    expect(await screen.findByText('No grants: nobody can open this app.')).toBeInTheDocument();
    const salesTable = screen.getByRole('table', { name: 'Grants on sales' });
    expect(within(salesTable).getByText('Sales team')).toBeInTheDocument();
    await user.click(within(salesTable).getByRole('button', { name: /Revoke group Sales team/ }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke grant' }));
    expect(await screen.findByText('Grant revoked.')).toBeInTheDocument();
    expect(calls.find((c) => c.url === '/api/grants/revoke')?.body).toEqual({
      app_id: 'sales',
      subject_type: 'group',
      subject_id: 'g1',
    });
  });

  it('requires an app and a subject before granting', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/grants': { body: { apps: [APP_SALES], users: [USER_ALICE], groups: [GROUP_SALES], grants: [] } },
    });
    const user = userEvent.setup();
    renderApp('/grants');
    const form = await screen.findByRole('form', { name: 'Grant access' });
    await user.click(within(form).getByRole('button', { name: 'Grant' }));
    expect(await within(form).findByText('Pick an app.')).toBeInTheDocument();
    expect(within(form).getByText('Pick who gets access.')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });
});

describe('AuditPage', () => {
  it('renders event details and filters by event type', async () => {
    const lines = [
      { ts: 't3', event: 'gate', email: 'eve@example.com', app_id: 'sales', allowed: false, reason: 'no_grant', path: '/', matched_grants: [], hostile_headers: ['X-Forwarded-User'] },
      { ts: 't2', event: 'query', email: 'alice@example.com', app_id: 'sales', allowed: true, reason: 'ok', datasets: ['orders'], row_count: 12 },
      { ts: 't1', event: 'admin', email: 'admin@example.com', action: 'app_create', target: 'sales' },
    ];
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/audit': { body: { lines, event: '', events: ['gate', 'query', 'admin'], tail_lines: 200 } },
      'GET /api/audit?event=admin': { body: { lines: [lines[2]], event: 'admin', events: ['gate', 'query', 'admin'], tail_lines: 200 } },
    });
    const user = userEvent.setup();
    renderApp('/audit');
    const table = await screen.findByRole('table', { name: 'Audit log' });
    expect(within(table).getByText('hostile: X-Forwarded-User')).toBeInTheDocument();
    expect(within(table).getByText('· 12 rows')).toBeInTheDocument();
    expect(within(table).getByText('app_create')).toBeInTheDocument();
    expect(screen.getByText('Newest first, last 200 lines.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'admin' }));
    expect(await screen.findByRole('button', { name: 'admin', pressed: true })).toBeInTheDocument();
    expect(calls.some((c) => c.url === '/api/audit?event=admin')).toBe(true);
    expect(await screen.findByText('app_create')).toBeInTheDocument();
    expect(screen.queryByText('hostile: X-Forwarded-User')).not.toBeInTheDocument();
  });
});
