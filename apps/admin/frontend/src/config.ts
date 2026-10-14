/**
 * UI configuration for the Access admin SPA. Every tunable value lives here, not inline in
 * components, so a change of rule (an identifier pattern, a cache window) is made in one place.
 *
 * The identifier patterns mirror the store's own validation (kbc_access.store) so the form can
 * flag an obviously bad value before the round trip; the server stays the authority and its
 * error message is shown when it disagrees.
 */
export const config = {
  api: {
    /** Prefix of every JSON endpoint; relative, because the app owns the root of its origin. */
    basePath: '/api',
  },
  query: {
    /** How long fetched data counts as fresh before a remount refetches it. */
    staleTimeMs: 10_000,
    /** Retries for failed GETs; auth failures are never retried (see App.tsx). */
    retry: 1,
  },
  patterns: {
    /** Local app ID, as the old Jinja form enforced: [a-z0-9][a-z0-9_-]{0,63}. */
    appId: /^[a-z0-9][a-z0-9_-]{0,63}$/,
    /** User attribute key: [a-z][a-z0-9_]{0,63}. */
    attributeKey: /^[a-z][a-z0-9_]{0,63}$/,
  },
  datasets: {
    /**
     * How the editor reads a warehouse column type (as GET /datasets reports it) to pick the
     * operators it offers. The server types literals itself; this only shapes the UI. Checked in
     * order: a type matching none of them is text.
     */
    columnKinds: {
      number: /^(TINY|SMALL|BIG|HUGE)?INT|INTEGER|NUMBER|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL/i,
      date: /DATE|TIME/i,
      boolean: /BOOL/i,
    },
  },
  /**
   * Product name for the screens shown before a session exists (not signed in / denied); once
   * signed in the header shows the configured `app_name` from /api/session instead.
   */
  productName: 'Access admin',
  /** Query-string parameter the switch-user endpoint reads to return to the current page. */
  switchUserNextParam: 'next',
} as const;
