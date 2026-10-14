import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { mockFetch, renderApp } from '../test/utils';

describe('SessionGate', () => {
  it('shows "Not signed in" with the reason on 401', async () => {
    mockFetch({
      'GET /api/session': {
        status: 401,
        body: { detail: 'missing X-Kbc-User-Email header', reason: 'not_authenticated' },
      },
    });
    renderApp('/users');
    expect(await screen.findByRole('heading', { name: 'Not signed in' })).toBeInTheDocument();
    expect(screen.getByText(/missing X-Kbc-User-Email header/)).toBeInTheDocument();
    // No page content leaks through the gate.
    expect(screen.queryByRole('heading', { name: 'Users' })).not.toBeInTheDocument();
  });

  it('shows the deny message, reason code, email and groups on 403', async () => {
    mockFetch({
      'GET /api/session': {
        status: 403,
        body: {
          detail: 'You have no grant for this app.',
          reason: 'no_grant',
          user: { email: 'eve@example.com', groups: ['zeta', 'alpha'] },
        },
      },
    });
    renderApp('/');
    expect(await screen.findByRole('heading', { name: "Sorry, you can't get in here" })).toBeInTheDocument();
    expect(screen.getByText('You have no grant for this app.')).toBeInTheDocument();
    expect(screen.getByText('no_grant')).toBeInTheDocument();
    const table = screen.getByRole('table');
    expect(within(table).getByText('eve@example.com')).toBeInTheDocument();
    const groups = within(table).getAllByText(/^(alpha|zeta)$/).map((n) => n.textContent);
    expect(groups).toEqual(['alpha', 'zeta']);
  });

  it('links to the launcher when the refusal names it', async () => {
    mockFetch({
      'GET /api/session': {
        status: 403,
        body: {
          detail: 'Denied.',
          reason: 'no_grant',
          user: { email: 'eve@example.com', groups: [] },
          launcher_url: 'https://launcher.example/',
        },
      },
    });
    renderApp('/');
    const link = await screen.findByRole('link', { name: 'Go to the launcher' });
    expect(link).toHaveAttribute('href', 'https://launcher.example/');
    expect(screen.getByText('none')).toBeInTheDocument();
  });

  it('renders the shell with who is signed in once admitted', async () => {
    mockFetch({
      'GET /api/session': {
        body: {
          user: { email: 'admin@example.com', groups: ['a', 'b'] },
          app_name: 'Access admin',
          launcher_url: 'https://launcher.example/',
          switch_user_path: '/_switch',
        },
      },
      'GET /api/audit': { body: { lines: [], event: '', events: ['gate', 'query', 'admin'], tail_lines: 200 } },
    });
    renderApp('/audit');
    expect(await screen.findByText('admin@example.com')).toBeInTheDocument();
    expect(screen.getByText('2 groups')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Launcher' })).toHaveAttribute('href', 'https://launcher.example/');
    expect(screen.getByRole('link', { name: 'Switch user' })).toHaveAttribute('href', '/_switch?next=%2Faudit');
    expect(screen.getByRole('link', { name: 'Audit' })).toHaveAttribute('aria-current', 'page');
  });
});
