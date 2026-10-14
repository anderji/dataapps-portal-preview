/**
 * Fetch wrapper for the admin JSON API.
 *
 * - Relative URLs under `/api`: the SPA is served from the root of the admin app's own origin.
 * - Every non-GET request carries `CSRF_HEADER`. A browser cannot attach a custom header to a
 *   cross-origin request without a CORS preflight the backend never allows, so the header proves
 *   the request came from this page. The name is a protocol constant shared with the backend
 *   (`kbc_access.web.CSRF_HEADER`) - keep the two in sync.
 * - Errors come back as `{detail, ...}`; they are raised as `ApiError` carrying the status, the
 *   human message and the full body (the session gate reads `reason` and `user` from it).
 * - Responses are validated with a zod schema, so a contract drift fails loudly at the boundary
 *   instead of rendering `undefined` somewhere deep in a page.
 */
import type { z } from 'zod';
import { config } from '../config';

export const CSRF_HEADER = 'X-Access-Csrf';
export const CSRF_HEADER_VALUE = '1';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly body: Record<string, unknown>;

  constructor(status: number, detail: string, body: Record<string, unknown>) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

/** Human message from an error body: `detail` as-is when a string, FastAPI's list form joined. */
function detailOf(body: Record<string, unknown>, status: number, statusText: string): string {
  const detail = body.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) =>
      item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : String(item),
    );
    if (parts.length) return parts.join('; ');
  }
  return `Request failed: ${status} ${statusText}`.trim();
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { detail: text };
  }
}

export type RequestOptions = {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
};

/** Build the URL of an API path; path segments must already be encoded (see `segment`). */
export function apiUrl(path: string): string {
  return `${config.api.basePath}${path}`;
}

/** Encode one dynamic path segment (an email, a group ID, an attribute key). */
export function segment(value: string): string {
  return encodeURIComponent(value);
}

export async function apiRequest<S extends z.ZodType>(
  path: string,
  schema: S,
  { method = 'GET', body, signal }: RequestOptions = {},
): Promise<z.infer<S>> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (!SAFE_METHODS.has(method)) headers[CSRF_HEADER] = CSRF_HEADER_VALUE;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await fetch(apiUrl(path), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
    signal,
  });
  const data = await readJson(response);
  if (!response.ok) {
    const errorBody = data && typeof data === 'object' ? (data as Record<string, unknown>) : {};
    throw new ApiError(response.status, detailOf(errorBody, response.status, response.statusText), errorBody);
  }
  return schema.parse(data);
}

/** Message to show for any thrown value (ApiError detail, zod/network error message). */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}
