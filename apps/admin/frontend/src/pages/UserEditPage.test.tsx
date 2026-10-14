import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { APP_HR, APP_SALES } from '../test/fixtures';
import { mockFetch, renderApp, SESSION } from '../test/utils';

const detail = (email: string) => ({
  user: { email, display_name: 'Alice', active: true },
  attributes: { company: 'acme' },
  apps: [APP_HR, APP_SALES],
  granted: ['sales'],
});

describe('UserEditPage', () => {
  it('saves identity + app grants and moves to the new URL when the email changes', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': { body: detail('alice@example.com') },
      'GET /api/users/alice%40new.example': { body: detail('alice@new.example') },
      'PUT /api/users/alice%40example.com': {
        body: { message: "User 'alice@new.example' saved.", email: 'alice@new.example' },
      },
    });
    const user = userEvent.setup();
    renderApp('/users/alice%40example.com');
    const form = await screen.findByRole('form', { name: 'Edit user' });
    expect(within(form).getByRole('checkbox', { name: /Sales dashboard/ })).toBeChecked();

    const email = within(form).getByLabelText(/Email/);
    await user.clear(email);
    await user.type(email, 'alice@new.example');
    await user.click(within(form).getByRole('checkbox', { name: /HR dashboard/ }));
    await user.click(within(form).getByRole('button', { name: 'Save user' }));

    expect(await screen.findByText("User 'alice@new.example' saved.")).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT');
    expect(put?.body).toEqual({
      new_email: 'alice@new.example',
      display_name: 'Alice',
      active: true,
      app_ids: ['hr', 'sales'],
    });
    // Navigated: the page now loads the user under the new address.
    await screen.findByText('alice@new.example', { selector: 'p, div, span' });
    expect(calls.some((c) => c.url === '/api/users/alice%40new.example')).toBe(true);
  });

  it('removes an attribute', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': { body: detail('alice@example.com') },
      'DELETE /api/users/alice%40example.com/attributes/company': { body: { message: 'Attribute removed.' } },
    });
    const user = userEvent.setup();
    renderApp('/users/alice%40example.com');
    await user.click(await screen.findByRole('button', { name: 'Remove company' }));
    expect(await screen.findByText('Attribute removed.')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true);
  });

  it('shows a 404 as a load error', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/ghost%40example.com': { status: 404, body: { detail: "Unknown user 'ghost@example.com'." } },
    });
    renderApp('/users/ghost%40example.com');
    expect(await screen.findByText("Unknown user 'ghost@example.com'.")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument();
  });
});
