import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { OVERVIEW, APP_HR, APP_SALES } from '../test/fixtures';
import { mockFetch, renderApp, SESSION } from '../test/utils';

describe('OverviewPage', () => {
  it('shows the counts and both matrices', async () => {
    mockFetch({ 'GET /api/session': { body: SESSION }, 'GET /api/overview': { body: OVERVIEW } });
    renderApp('/');
    const userMatrix = await screen.findByRole('table', { name: 'User grants' });
    expect(within(userMatrix).getByText('alice@example.com')).toBeInTheDocument();
    expect(within(userMatrix).getByText('user:alice@example.com')).toBeInTheDocument();
    expect(within(userMatrix).getByText('no_grant')).toBeInTheDocument();
    const groupMatrix = screen.getByRole('table', { name: 'Group grants' });
    expect(within(groupMatrix).getByText('entra:sales-team')).toBeInTheDocument();
    expect(within(groupMatrix).getAllByText('granted')).toHaveLength(1);
    expect(screen.getByText('Attributes')).toBeInTheDocument();
  });

  it('submits the simulator and renders the decision per app', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/overview': { body: OVERVIEW },
      'POST /api/simulate': {
        body: {
          email: 'bob@example.com',
          groups: [
            { external_id: 'ghost', name: null },
            { external_id: 'sales-team', name: 'Sales team' },
          ],
          results: [
            { app: APP_HR, decision: { allowed: false, reason: 'no_grant', matched: [] } },
            { app: APP_SALES, decision: { allowed: true, reason: 'granted', matched: ['group:g1'] } },
          ],
        },
      },
    });
    const user = userEvent.setup();
    renderApp('/');
    const form = await screen.findByRole('form', { name: 'Access simulator' });
    await user.type(within(form).getByLabelText(/Email/), 'Bob@Example.com');
    await user.type(within(form).getByLabelText(/Other group IDs/), 'ghost, sales-team');
    await user.click(within(form).getByRole('button', { name: 'Check access' }));

    expect(await screen.findByText('Result for')).toBeInTheDocument();
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.url).toBe('/api/simulate');
    expect(post?.body).toEqual({ email: 'Bob@Example.com', groups: [], extra: 'ghost, sales-team' });
    expect(post?.headers.get('X-Access-Csrf')).toBe('1');

    const groupsLine = screen.getByTestId('simulation-groups');
    expect(within(groupsLine).getByText('unknown')).toBeInTheDocument();
    expect(groupsLine).toHaveTextContent('sales-team (Sales team)');

    const results = screen.getAllByRole('table')[0];
    const rows = within(results).getAllByRole('row').slice(1);
    expect(within(rows[0]).getByText('denied')).toBeInTheDocument();
    expect(within(rows[0]).getByText('no_grant')).toBeInTheDocument();
    expect(within(rows[1]).getByText('allowed')).toBeInTheDocument();
    expect(within(rows[1]).getByText('group:g1')).toBeInTheDocument();
  });

  it('shows the server validation message when the email is rejected', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/overview': { body: OVERVIEW },
      'POST /api/simulate': { status: 400, body: { detail: 'Enter the email of the principal to check.' } },
    });
    const user = userEvent.setup();
    renderApp('/');
    const form = await screen.findByRole('form', { name: 'Access simulator' });
    // Empty email: the browser lets it through (the field is aria-required only), the server says no.
    await user.click(within(form).getByRole('button', { name: 'Check access' }));
    await waitFor(() =>
      expect(within(form).getByText('Enter the email of the principal to check.')).toBeInTheDocument(),
    );
  });
});
