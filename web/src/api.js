/* Thin fetch wrapper for the chat UI.
 *
 * What it does for you:
 *   - Adds `credentials: 'include'` so the HttpOnly auth cookie ships
 *   - JSON-encodes the body and sets Content-Type for POST/PUT/PATCH
 *   - Parses the response as JSON
 *   - Throws on non-2xx with a useful error message
 *   - Redirects to /login when the server returns 401
 *
 * What it does NOT do:
 *   - Retry. WS reconnect lives in ws.js; HTTP failures bubble up so the
 *     caller can decide whether to retry, toast, or ignore.
 *   - Auto-toast. Some callers want a silent failure (e.g. background
 *     polling); they catch and decide.
 *
 * 59 fetch call sites in index.html — they migrate over to these
 * helpers gradually as their host modules get extracted. New code
 * should always go through here. */

class ApiError extends Error {
  constructor(status, body, url) {
    super(`HTTP ${status} at ${url}: ${typeof body === "string" ? body : JSON.stringify(body)}`.slice(0, 500));
    this.status = status;
    this.body = body;
    this.url = url;
  }
}

async function _request(method, path, body, init = {}) {
  const opts = {
    method,
    credentials: "include",
    headers: { ...(init.headers || {}) },
    ...init,
  };
  if (body !== undefined && body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (networkErr) {
    // DNS / TCP / TLS / CORS failure — re-throw with context so Sentry
    // captures something useful instead of a bare TypeError.
    throw new ApiError(0, String(networkErr.message || networkErr), path);
  }

  // Auth expired — bounce to login. Use replace so the broken page
  // doesn't sit in history.
  if (resp.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.replace(`/login?next=${next}`);
    // Never resolve — page is unloading.
    return new Promise(() => {});
  }

  // 204 / empty bodies — return null so callers don't choke on
  // JSON.parse('').
  if (resp.status === 204) return null;

  const ct = resp.headers.get("content-type") || "";
  const isJson = ct.includes("application/json");
  let payload;
  try {
    payload = isJson ? await resp.json() : await resp.text();
  } catch {
    payload = null;
  }

  if (!resp.ok) {
    throw new ApiError(resp.status, payload, path);
  }
  return payload;
}

export const apiGet    = (path, init)        => _request("GET", path, null, init);
export const apiPost   = (path, body, init)  => _request("POST", path, body, init);
export const apiPut    = (path, body, init)  => _request("PUT", path, body, init);
export const apiPatch  = (path, body, init)  => _request("PATCH", path, body, init);
export const apiDelete = (path, body, init)  => _request("DELETE", path, body, init);
export { ApiError };
