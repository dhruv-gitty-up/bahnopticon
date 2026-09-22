/** API base is set at build time; an empty value uses the current origin. */
const API_BASE_URL = (import.meta.env.VITE_API_URL ?? '').trim().replace(/\/+$/, '');

export function apiUrl(path: `/${string}`): string {
  return `${API_BASE_URL}${path}`;
}
