// JSON API client. All URLs are relative so that they work below the Ingress base path.

export class ApiError extends Error {
  constructor(status, key, params = {}) {
    super(key);
    this.status = status;
    this.key = key;
    this.params = params;
  }
}

let csrfToken = null;

export function setCsrf(token) {
  csrfToken = token;
}

// Changing calls carry the CSRF token; `revision` becomes If-Match ("<rev>") for the settings document.
export async function api(method, path, body, { revision } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    if (revision !== undefined && revision !== null) headers['If-Match'] = `"${revision}"`;
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new ApiError(0, 'network_error');
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = data && data.error ? data.error : {};
    throw new ApiError(response.status, error.key || `http_${response.status}`, error.params || {});
  }
  return data;
}
