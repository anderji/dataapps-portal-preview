import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { mockFetch, renderApp, SESSION } from '../test/utils';

const row = (over: Record<string, unknown>) => ({
  ds_app_id: '1',
  config_id: 'c1',
  project_id: 'p',
  name: 'Sales',
  name_source: 'configuration',
  slug: 'sales',
  url: 'https://sales.example/',
  state: 'running',
  status: 'new',
  local_id: 'sales',
  auth_mode: 'required',
  auth_providers: [{ id: 'oidc-1', type: 'oidc', issuer_url: 'https://login.microsoftonline.com/tenant-1/v2.0' }],
  ...over,
});

describe('DiscoveryPage', () => {
  it('explains why discovery is unavailable', async () => {
    mockFetch({ 'GET /api/session': { body: SESSION }, 'GET /api/discovery': { body: { available: false } } });
    renderApp('/discovery');
    expect(await screen.findByText(/available only when the admin app runs in Keboola/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Scan this project/ })).not.toBeInTheDocument();
  });

  it('scans, lets the admin edit new rows, and imports the selected ones', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/discovery': { body: { available: true } },
      'POST /api/discovery/scan': {
        body: {
          results: [
            row({}),
            row({ ds_app_id: '2', name: 'App 2', name_source: 'slug', local_id: 'app-2' }),
            row({ ds_app_id: '3', name: 'Launcher', status: 'launcher', local_id: 'launcher' }),
            row({ ds_app_id: '4', name: 'Old', status: 'registered', local_id: 'old' }),
          ],
        },
      },
      'POST /api/discovery/import': { body: { message: 'Imported 1 app.', error: null } },
    });
    const user = userEvent.setup();
    renderApp('/discovery');
    await user.click(await screen.findByRole('button', { name: /Scan this project/ }));
    const table = await screen.findByRole('table', { name: 'Discovered Data Apps' });
    expect(within(table).getByText("this suite's launcher")).toBeInTheDocument();
    expect(within(table).getByText('already registered')).toBeInTheDocument();
    expect(within(table).getByText(/name from the slug/)).toBeInTheDocument();
    // Only new rows are importable.
    expect(within(table).getAllByRole('checkbox')).toHaveLength(2);

    await user.click(within(table).getByRole('checkbox', { name: 'Import App 2' }));
    const name = within(table).getByLabelText('Name of 1');
    await user.clear(name);
    await user.type(name, 'Sales board');
    await user.click(screen.getByRole('button', { name: /Import selected/ }));

    expect(await screen.findByText('Imported 1 app.')).toBeInTheDocument();
    expect(calls.find((c) => c.url === '/api/discovery/import')?.body).toEqual({
      items: [{ ds_app_id: '1', local_id: 'sales', name: 'Sales board' }],
    });
  });

  it('blocks import while a local ID is invalid', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/discovery': { body: { available: true } },
      'POST /api/discovery/scan': { body: { results: [row({})] } },
    });
    const user = userEvent.setup();
    renderApp('/discovery');
    await user.click(await screen.findByRole('button', { name: /Scan this project/ }));
    const input = await screen.findByLabelText('App ID for 1');
    await user.clear(input);
    await user.type(input, 'Bad ID');
    expect(screen.getByRole('button', { name: /Import selected/ })).toBeDisabled();
  });

  it('shows a scan failure', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/discovery': { body: { available: true } },
      'POST /api/discovery/scan': { status: 502, body: { detail: 'Discovery failed: timeout (token redacted)' } },
    });
    const user = userEvent.setup();
    renderApp('/discovery');
    await user.click(await screen.findByRole('button', { name: /Scan this project/ }));
    expect(await screen.findByText('Discovery failed: timeout (token redacted)')).toBeInTheDocument();
  });

  it('shows how each app is authorized and warns when some are unknown', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/discovery': { body: { available: true } },
      'POST /api/discovery/scan': {
        body: {
          results: [
            row({}),
            row({
              ds_app_id: '2',
              name: 'Pwd',
              local_id: 'pwd',
              auth_providers: [{ id: 'simpleAuth', type: 'password', issuer_url: '' }],
            }),
            row({ ds_app_id: '3', name: 'Open', local_id: 'open', auth_mode: 'public', auth_providers: [] }),
            row({ ds_app_id: '4', name: 'Hidden', local_id: 'hidden', auth_mode: 'unknown', auth_providers: [] }),
          ],
        },
      },
    });
    const user = userEvent.setup();
    renderApp('/discovery');
    await user.click(await screen.findByRole('button', { name: /Scan this project/ }));
    const table = await screen.findByRole('table', { name: 'Discovered Data Apps' });
    expect(within(table).getByRole('columnheader', { name: 'Authorization' })).toBeInTheDocument();
    expect(within(table).getAllByText('login required')).toHaveLength(2);
    expect(within(table).getByText('OIDC')).toBeInTheDocument();
    expect(within(table).getByText('login.microsoftonline.com/tenant-1')).toBeInTheDocument();
    expect(within(table).getByText('Password')).toBeInTheDocument();
    expect(within(table).getByText('public - no login')).toBeInTheDocument();
    expect(within(table).getByText('unknown')).toBeInTheDocument();
    expect(screen.getByText('Some logins are unknown')).toBeInTheDocument();
  });

  it('does not warn when every login is known', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/discovery': { body: { available: true } },
      'POST /api/discovery/scan': { body: { results: [row({})] } },
    });
    const user = userEvent.setup();
    renderApp('/discovery');
    await user.click(await screen.findByRole('button', { name: /Scan this project/ }));
    await screen.findByRole('table', { name: 'Discovered Data Apps' });
    expect(screen.queryByText('Some logins are unknown')).not.toBeInTheDocument();
  });
});
