/**
 * Test harness: the real app tree (session gate, shell, routes, query client, toaster) under a
 * MemoryRouter, with `fetch` replaced at the network boundary by a route table.
 */
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { Toaster } from '@keboola/design';
import { renderWithTheme } from '@keboola/design/testing';
import { vi } from 'vitest';
import { AppRoutes, createQueryClient } from '../App';

export type MockResponse = { status?: number; body: unknown };
export type RouteHandler = (request: { method: string; url: string; body: unknown; headers: Headers }) => MockResponse;
/** Keys are "METHOD /api/path" (query string included when the page sends one). */
export type Routes = Record<string, MockResponse | RouteHandler>;

export type RecordedCall = { method: string; url: string; body: unknown; headers: Headers };

export const SESSION = {
  user: { email: 'admin@example.com', groups: ['data-app-admins', 'staff'] },
  app_name: 'Access admin',
  launcher_url: 'https://launcher.example/',
  switch_user_path: '/_switch',
};

export function mockFetch(routes: Routes) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = new Headers(init?.headers);
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
    const call = { method, url, body, headers };
    calls.push(call);
    const route = routes[`${method} ${url}`];
    if (!route) {
      return new Response(JSON.stringify({ detail: `No mock for ${method} ${url}` }), { status: 599 });
    }
    const { status = 200, body: responseBody } = typeof route === 'function' ? route(call) : route;
    return new Response(JSON.stringify(responseBody), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

export function renderApp(path: string) {
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false, staleTime: 0 },
  });
  return renderWithTheme(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
      <Toaster />
    </QueryClientProvider>,
  );
}
