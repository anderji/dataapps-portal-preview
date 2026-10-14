import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { UserWithAttributes } from '../api/types';
import { mockFetch, renderApp, SESSION } from '../test/utils';

const ALICE: UserWithAttributes = { email: 'alice@example.com', display_name: 'Alice', active: true, attributes: { company: 'acme' } };
const BOB: UserWithAttributes = { email: 'bob@example.com', display_name: 'Bob', active: true, attributes: {} };

describe('UsersPage', () => {
  it('creates a user, toasts the server message and refetches the list', async () => {
    let users = [ALICE];
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': () => ({ body: { users } }),
      'POST /api/users': ({ body }) => {
        users = [...users, BOB];
        return { body: { message: `User '${(body as { email: string }).email}' created.` } };
      },
    });
    const user = userEvent.setup();
    renderApp('/users');
    const form = await screen.findByRole('form', { name: 'Create a user' });
    expect(screen.queryByText('bob@example.com')).not.toBeInTheDocument();

    await user.type(within(form).getByLabelText(/Email/), 'bob@example.com');
    await user.type(within(form).getByLabelText(/Display name/), 'Bob');
    await user.click(within(form).getByRole('button', { name: 'Create user' }));

    expect(await screen.findByText("User 'bob@example.com' created.")).toBeInTheDocument();
    expect(await screen.findByText('bob@example.com')).toBeInTheDocument();
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.body).toEqual({ email: 'bob@example.com', display_name: 'Bob' });
    expect(post?.headers.get('X-Access-Csrf')).toBe('1');
    expect(calls.filter((c) => c.method === 'GET' && c.url === '/api/users')).toHaveLength(2);
    // The form is cleared for the next user.
    await waitFor(() => expect(within(form).getByLabelText(/Email/)).toHaveValue(''));
  });

  it('shows a 409 inline and keeps the typed values', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': { body: { users: [ALICE] } },
      'POST /api/users': { status: 409, body: { detail: "User 'alice@example.com' already exists." } },
    });
    const user = userEvent.setup();
    renderApp('/users');
    const form = await screen.findByRole('form', { name: 'Create a user' });
    await user.type(within(form).getByLabelText(/Email/), 'alice@example.com');
    await user.click(within(form).getByRole('button', { name: 'Create user' }));
    expect(await within(form).findByText("User 'alice@example.com' already exists.")).toBeInTheDocument();
    expect(within(form).getByLabelText(/Email/)).toHaveValue('alice@example.com');
  });

  it('sets an attribute with the key in the URL and the value in the body', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': { body: { users: [ALICE, BOB] } },
      'PUT /api/users/bob%40example.com/attributes/company': { body: { message: 'Attribute saved.' } },
    });
    const user = userEvent.setup();
    renderApp('/users');
    const form = await screen.findByRole('form', { name: 'Set attribute for bob@example.com' });
    await user.type(within(form).getByLabelText('Key'), 'company');
    await user.type(within(form).getByLabelText('Value'), 'globex');
    await user.click(within(form).getByRole('button', { name: 'Set attribute' }));
    expect(await screen.findByText('Attribute saved.')).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT');
    expect(put?.body).toEqual({ value: 'globex' });
  });

  it('flags an invalid attribute key before sending', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': { body: { users: [ALICE] } },
    });
    const user = userEvent.setup();
    renderApp('/users');
    const form = await screen.findByRole('form', { name: 'Set attribute for alice@example.com' });
    await user.type(within(form).getByLabelText('Key'), 'Bad Key');
    await user.type(within(form).getByLabelText('Value'), 'x');
    await user.click(within(form).getByRole('button', { name: 'Set attribute' }));
    expect(await within(form).findByText(/starts with a letter/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
  });

  it('deletes a user after confirmation', async () => {
    let users = [ALICE, BOB];
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': () => ({ body: { users } }),
      'DELETE /api/users/bob%40example.com': () => {
        users = [ALICE];
        return { body: { message: "User 'bob@example.com' deleted." } };
      },
    });
    const user = userEvent.setup();
    renderApp('/users');
    await user.click(await screen.findByRole('button', { name: 'Delete bob@example.com' }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete user' }));
    expect(await screen.findByText("User 'bob@example.com' deleted.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('bob@example.com')).not.toBeInTheDocument());
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true);
  });

  it('shows a failed action in a persistent Alert', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users': { body: { users: [ALICE] } },
      'POST /api/users/alice%40example.com/toggle': { status: 404, body: { detail: "Unknown user 'alice@example.com'." } },
    });
    const user = userEvent.setup();
    renderApp('/users');
    await user.click(await screen.findByRole('button', { name: 'Deactivate alice@example.com' }));
    expect(await screen.findByText("Unknown user 'alice@example.com'.")).toBeInTheDocument();
    expect(screen.getByText('The action failed')).toBeInTheDocument();
  });
});
