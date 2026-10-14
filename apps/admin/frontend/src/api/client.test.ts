import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import { mockFetch } from '../test/utils';
import { ApiError, apiRequest, CSRF_HEADER, errorMessage, segment } from './client';
import { grantSchema, grantsSchema, userDetailSchema } from './types';

const okSchema = z.object({ message: z.string() });

describe('apiRequest', () => {
  it('sends GET without the CSRF header, to a relative /api URL', async () => {
    const { calls } = mockFetch({ 'GET /api/apps': { body: { message: 'ok' } } });
    await apiRequest('/apps', okSchema);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/apps');
    expect(calls[0].headers.has(CSRF_HEADER)).toBe(false);
  });

  it.each(['POST', 'PUT', 'DELETE'] as const)('sends %s with the CSRF header', async (method) => {
    const { calls } = mockFetch({ [`${method} /api/things`]: { body: { message: 'done' } } });
    await apiRequest('/things', okSchema, { method, body: method === 'DELETE' ? undefined : { a: 1 } });
    expect(calls[0].headers.get(CSRF_HEADER)).toBe('1');
  });

  it('uses the header name the backend checks', () => {
    expect(CSRF_HEADER).toBe('X-Access-Csrf');
  });

  it('serialises the JSON body', async () => {
    const { calls } = mockFetch({ 'POST /api/users': { body: { message: 'created' } } });
    const result = await apiRequest('/users', okSchema, { method: 'POST', body: { email: 'a@b.c' } });
    expect(result).toEqual({ message: 'created' });
    expect(calls[0].body).toEqual({ email: 'a@b.c' });
    expect(calls[0].headers.get('Content-Type')).toBe('application/json');
  });

  it('raises ApiError with status, detail and the full body', async () => {
    mockFetch({
      'POST /api/users': { status: 409, body: { detail: "User 'a@b.c' already exists." } },
    });
    const error = await apiRequest('/users', okSchema, { method: 'POST', body: {} }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toBe("User 'a@b.c' already exists.");
    expect(errorMessage(error)).toBe("User 'a@b.c' already exists.");
  });

  it('keeps extra fields of an error body (the gate reads reason and user)', async () => {
    const body = { detail: 'No grant.', reason: 'no_grant', user: { email: 'x@y.z', groups: ['g'] } };
    mockFetch({ 'GET /api/session': { status: 403, body } });
    const error = (await apiRequest('/session', okSchema).catch((e: unknown) => e)) as ApiError;
    expect(error.body).toEqual(body);
  });

  it("joins FastAPI's list-shaped validation detail", async () => {
    mockFetch({
      'POST /api/apps': { status: 422, body: { detail: [{ msg: 'field required' }, { msg: 'bad value' }] } },
    });
    const error = (await apiRequest('/apps', okSchema, { method: 'POST', body: {} }).catch((e: unknown) => e)) as ApiError;
    expect(error.detail).toBe('field required; bad value');
  });

  it('rejects a response that does not match the schema', async () => {
    mockFetch({ 'GET /api/apps': { body: { unexpected: true } } });
    await expect(apiRequest('/apps', okSchema)).rejects.toThrow();
  });
});

describe('local group routes', () => {
  it('encodes the group id and the member email in the member URL', async () => {
    const { calls } = mockFetch({
      'PUT /api/local-groups/sales-managers/members/a%2Bb%40example.com': { body: { message: 'ok' } },
    });
    await apiRequest(`/local-groups/${segment('sales-managers')}/members/${segment('a+b@example.com')}`, okSchema, {
      method: 'PUT',
    });
    expect(calls[0].headers.get(CSRF_HEADER)).toBe('1');
    expect(calls[0].body).toBeUndefined();
  });

  it('accepts local_group as a grant subject type', () => {
    expect(grantSchema.parse({ app_id: 'a', subject_type: 'local_group', subject_id: 'x', granted_by: 'u', granted_at: 't' }).subject_type).toBe('local_group');
  });

  it('defaults local groups to empty for a backend that predates them', () => {
    const grants = grantsSchema.parse({ apps: [], grants: [], users: [], groups: [] });
    expect(grants.local_groups).toEqual([]);
    const detail = userDetailSchema.parse({
      user: { email: 'a@b.c', display_name: '', active: true },
      attributes: {},
      apps: [],
      granted: [],
    });
    expect(detail.local_groups).toEqual([]);
  });
});
