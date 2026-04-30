/**
 * Tiny fetch wrapper used everywhere in the app.
 * - Always includes the session cookie (`credentials: 'same-origin'`).
 * - Auto-encodes JSON bodies and parses JSON responses.
 * - Throws an Error with a useful message on non-2xx so callers can `try/catch`.
 */

const baseFetch = (input, init = {}) =>
  fetch(input, { credentials: 'same-origin', ...init });

const ensureJson = async (res) => {
  if (!res.ok) {
    let detail = '';
    try { detail = await res.text(); } catch {}
    const e = new Error(`${res.status} ${res.statusText}${detail ? ' — ' + detail.slice(0, 200) : ''}`);
    e.status = res.status;
    e.body = detail;
    throw e;
  }
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('json')) return null;
  return res.json();
};

export const apiGet = (url) => baseFetch(url).then(ensureJson);

export const apiSend = (url, method, body, extraHeaders = {}) =>
  baseFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
    body: body == null ? undefined : JSON.stringify(body),
  }).then(ensureJson);

export const apiDelete = (url) =>
  baseFetch(url, { method: 'DELETE' }).then(ensureJson);

// Returns the raw response so the caller can deal with non-JSON bodies (e.g. /api/run).
export const apiRaw = (url, init = {}) => baseFetch(url, init);
