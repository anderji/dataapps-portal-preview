import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { APP_HR, APP_SALES, GROUP_SALES, LOCAL_GROUP_SALES, USER_ALICE } from '../test/fixtures';
import { mockFetch, renderApp, SESSION } from '../test/utils';

const BOB = { email: 'bob@example.com', display_name: 'Bob', active: true, attributes: {} };

describe('Navigation', () => {
  it('names the IdP groups entry and adds a Local groups entry', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': { body: [] },
    });
    renderApp('/local-groups');
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    expect(within(nav).getByRole('link', { name: 'IdP groups' })).toHaveAttribute('href', '/groups');
    expect(within(nav).getByRole('link', { name: 'Local groups' })).toHaveAttribute('href', '/local-groups');
    expect(within(nav).queryByRole('link', { name: 'Groups' })).not.toBeInTheDocument();
  });
});

describe('LocalGroupsPage', () => {
  it('lists local groups with member counts', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': { body: [LOCAL_GROUP_SALES] },
    });
    renderApp('/local-groups');
    const table = await screen.findByRole('table', { name: 'Local groups' });
    expect(within(table).getByText('Sales managers')).toBeInTheDocument();
    expect(within(table).getByText('sales-managers')).toBeInTheDocument();
    expect(within(table).getByText('Regional sales leads')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: 'Edit Sales managers' })).toHaveAttribute(
      'href',
      '/local-groups/sales-managers',
    );
  });

  it('creates a local group and refetches the list', async () => {
    let groups: unknown[] = [];
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': () => ({ body: groups }),
      'POST /api/local-groups': () => {
        groups = [LOCAL_GROUP_SALES];
        return { status: 201, body: LOCAL_GROUP_SALES };
      },
    });
    const user = userEvent.setup();
    renderApp('/local-groups');
    expect(await screen.findByText('No local groups.')).toBeInTheDocument();
    const form = screen.getByRole('form', { name: 'Create a local group' });
    await user.type(within(form).getByLabelText(/Local group ID/), 'sales-managers');
    await user.type(within(form).getByLabelText(/^Name/), 'Sales managers');
    await user.type(within(form).getByLabelText(/Description/), 'Regional sales leads');
    await user.click(within(form).getByRole('button', { name: 'Create local group' }));
    expect(await screen.findByText("Local group 'sales-managers' created.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
      local_group_id: 'sales-managers',
      name: 'Sales managers',
      description: 'Regional sales leads',
    });
    expect(await screen.findByRole('table', { name: 'Local groups' })).toBeInTheDocument();
  });

  it('rejects a bad slug before sending', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': { body: [] },
    });
    const user = userEvent.setup();
    renderApp('/local-groups');
    const form = await screen.findByRole('form', { name: 'Create a local group' });
    await user.type(within(form).getByLabelText(/Local group ID/), 'Bad Slug!');
    await user.type(within(form).getByLabelText(/^Name/), 'x');
    await user.click(within(form).getByRole('button', { name: 'Create local group' }));
    expect(await within(form).findByText(/lowercase letters, digits/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('shows a 409 inline', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': { body: [LOCAL_GROUP_SALES] },
      'POST /api/local-groups': { status: 409, body: { detail: "Local group 'sales-managers' already exists." } },
    });
    const user = userEvent.setup();
    renderApp('/local-groups');
    const form = await screen.findByRole('form', { name: 'Create a local group' });
    await user.type(within(form).getByLabelText(/Local group ID/), 'sales-managers');
    await user.type(within(form).getByLabelText(/^Name/), 'Sales managers');
    await user.click(within(form).getByRole('button', { name: 'Create local group' }));
    expect(await within(form).findByText("Local group 'sales-managers' already exists.")).toBeInTheDocument();
  });

  it('deletes a local group after confirmation', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups': { body: [LOCAL_GROUP_SALES] },
      'DELETE /api/local-groups/sales-managers': { body: { message: 'Local group deleted.' } },
    });
    const user = userEvent.setup();
    renderApp('/local-groups');
    const table = await screen.findByRole('table', { name: 'Local groups' });
    await user.click(within(table).getByRole('button', { name: 'Delete Sales managers' }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete local group' }));
    expect(await screen.findByText('Local group deleted.')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/local-groups/sales-managers')).toBe(true);
  });
});

describe('LocalGroupEditPage', () => {
  const routes = (extra = {}) => ({
    'GET /api/session': { body: SESSION },
    'GET /api/local-groups/sales-managers': { body: LOCAL_GROUP_SALES },
    'GET /api/users': { body: { users: [{ ...USER_ALICE, attributes: {} }, BOB] } },
    'GET /api/grants': {
      body: {
        apps: [APP_HR, APP_SALES],
        users: [USER_ALICE],
        groups: [GROUP_SALES],
        local_groups: [{ local_group_id: 'sales-managers', name: 'Sales managers' }],
        grants: [
          { app_id: 'sales', subject_type: 'local_group', subject_id: 'sales-managers', granted_by: 'admin@example.com', granted_at: '2026-10-01' },
          { app_id: 'hr', subject_type: 'group', subject_id: 'g1', granted_by: 'admin@example.com', granted_at: '2026-10-01' },
        ],
      },
    },
    ...extra,
  });

  it('saves name and description', async () => {
    const { calls } = mockFetch(
      routes({ 'PUT /api/local-groups/sales-managers': { body: { ...LOCAL_GROUP_SALES, name: 'Sales leads' } } }),
    );
    const user = userEvent.setup();
    renderApp('/local-groups/sales-managers');
    const form = await screen.findByRole('form', { name: 'Edit local group' });
    const name = within(form).getByLabelText(/^Name/);
    await user.clear(name);
    await user.type(name, 'Sales leads');
    await user.click(within(form).getByRole('button', { name: 'Save local group' }));
    expect(await screen.findByText("Local group 'Sales leads' saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      name: 'Sales leads',
      description: 'Regional sales leads',
    });
  });

  it('lists members and the group grants, and removes a member', async () => {
    const { calls } = mockFetch(
      routes({ 'DELETE /api/local-groups/sales-managers/members/alice%40example.com': { body: { message: 'Member removed.' } } }),
    );
    const user = userEvent.setup();
    renderApp('/local-groups/sales-managers');
    const members = await screen.findByRole('table', { name: 'Members' });
    expect(within(members).getByRole('link', { name: 'alice@example.com' })).toHaveAttribute(
      'href',
      '/users/alice%40example.com',
    );
    const grants = await screen.findByRole('table', { name: 'Group grants' });
    expect(within(grants).getByText('Sales dashboard')).toBeInTheDocument();
    expect(within(grants).queryByText('HR dashboard')).not.toBeInTheDocument();
    await user.click(within(members).getByRole('button', { name: 'Remove alice@example.com' }));
    expect(await screen.findByText('Member removed.')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true);
  });

  it('adds a registered user as member', async () => {
    const { calls } = mockFetch(
      routes({ 'PUT /api/local-groups/sales-managers/members/bob%40example.com': { body: { message: 'Member added.' } } }),
    );
    const user = userEvent.setup();
    renderApp('/local-groups/sales-managers');
    const form = await screen.findByRole('form', { name: 'Add member' });
    await user.click(within(form).getByRole('combobox'));
    // Alice is already a member, so only Bob is offered.
    expect(screen.queryByRole('option', { name: 'alice@example.com' })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('option', { name: 'bob@example.com' }));
    await user.click(within(form).getByRole('button', { name: 'Add member' }));
    expect(await screen.findByText('Member added.')).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT');
    expect(put?.url).toBe('/api/local-groups/sales-managers/members/bob%40example.com');
    expect(put?.body).toBeUndefined();
  });

  it('shows the 422 for an unregistered user inline', async () => {
    mockFetch(
      routes({
        'PUT /api/local-groups/sales-managers/members/bob%40example.com': {
          status: 422,
          body: { detail: 'user must be registered first' },
        },
      }),
    );
    const user = userEvent.setup();
    renderApp('/local-groups/sales-managers');
    const form = await screen.findByRole('form', { name: 'Add member' });
    await user.click(within(form).getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'bob@example.com' }));
    await user.click(within(form).getByRole('button', { name: 'Add member' }));
    expect(await within(form).findByText('user must be registered first')).toBeInTheDocument();
  });

  it('shows a load error for an unknown group', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups/nope': { status: 404, body: { detail: "Local group 'nope' not found." } },
    });
    renderApp('/local-groups/nope');
    expect(await screen.findByText("Local group 'nope' not found.")).toBeInTheDocument();
  });
});

describe('UserEditPage local groups', () => {
  const detail = (localGroups: unknown[]) => ({
    user: USER_ALICE,
    attributes: {},
    apps: [APP_SALES],
    granted: [],
    local_groups: localGroups,
  });

  it('links to the local groups of the user', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': {
        body: detail([{ local_group_id: 'sales-managers', name: 'Sales managers' }]),
      },
    });
    renderApp('/users/alice%40example.com');
    const list = await screen.findByRole('list', { name: 'Local groups of this user' });
    expect(within(list).getByRole('link', { name: 'Sales managers' })).toHaveAttribute(
      'href',
      '/local-groups/sales-managers',
    );
  });

  it('says so when the user is in no local group', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': { body: detail([]) },
    });
    renderApp('/users/alice%40example.com');
    expect(await screen.findByText('Not in any local group.')).toBeInTheDocument();
  });
});

describe('GrantsPage local groups', () => {
  const grantsBody = {
    apps: [APP_SALES],
    users: [USER_ALICE],
    groups: [GROUP_SALES],
    local_groups: [{ local_group_id: 'sales-managers', name: 'Sales managers' }],
    grants: [
      { app_id: 'sales', subject_type: 'local_group', subject_id: 'sales-managers', granted_by: 'admin@example.com', granted_at: '2026-10-01' },
    ],
  };

  it('shows and revokes a local group grant', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/grants': { body: grantsBody },
      'POST /api/grants/revoke': { body: { message: 'Grant revoked.' } },
    });
    const user = userEvent.setup();
    renderApp('/grants');
    const table = await screen.findByRole('table', { name: 'Grants on sales' });
    expect(within(table).getByText('Sales managers')).toBeInTheDocument();
    expect(within(table).getByText('local_group:sales-managers')).toBeInTheDocument();
    await user.click(within(table).getByRole('button', { name: /Revoke local group Sales managers/ }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke grant' }));
    expect(await screen.findByText('Grant revoked.')).toBeInTheDocument();
    expect(calls.find((c) => c.url === '/api/grants/revoke')?.body).toEqual({
      app_id: 'sales',
      subject_type: 'local_group',
      subject_id: 'sales-managers',
    });
  });

  it('grants access to a local group chosen as subject type', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/grants': { body: { ...grantsBody, grants: [] } },
      'POST /api/grants': { body: { message: 'Granted.' } },
    });
    const user = userEvent.setup();
    renderApp('/grants');
    const form = await screen.findByRole('form', { name: 'Grant access' });
    await user.click(within(form).getByRole('combobox', { name: /^App/ }));
    await user.click(await screen.findByRole('option', { name: /Sales dashboard/ }));
    await user.click(within(form).getByRole('combobox', { name: /Subject type/ }));
    await user.click(await screen.findByRole('option', { name: 'local group' }));
    await user.click(await within(form).findByRole('combobox', { name: /^Local group/ }));
    await user.click(await screen.findByRole('option', { name: 'Sales managers' }));
    await user.click(within(form).getByRole('button', { name: 'Grant' }));
    expect(await screen.findByText('Granted.')).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
      app_id: 'sales',
      subject_type: 'local_group',
      subject_id: 'sales-managers',
    });
  });
});
