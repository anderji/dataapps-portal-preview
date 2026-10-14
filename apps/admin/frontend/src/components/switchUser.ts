import { config } from '../config';

/** Switch-user URL that returns to `path` afterwards (a full page navigation, not a route). */
export function switchUserHref(switchUserPath: string, path: string): string {
  return `${switchUserPath}?${new URLSearchParams({ [config.switchUserNextParam]: path }).toString()}`;
}
