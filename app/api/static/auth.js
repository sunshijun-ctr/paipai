/* Shared client for /api/auth/* and /api/me. Used by login/register/account pages
   and by index.html for the auto-refresh / logout flow. */
(function () {
  const BASE = '';

  async function request(path, options = {}) {
    const opts = Object.assign({
      method: 'GET',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    }, options);
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(BASE + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* might be 204 */ }
    if (!res.ok) {
      const err = new Error((data && (data.detail?.message || data.message)) || `HTTP ${res.status}`);
      err.status = res.status;
      err.code = data && (data.detail?.code || data.code);
      err.payload = data;
      throw err;
    }
    return data;
  }

  const Auth = {
    async getMe() {
      try { return await request('/api/me'); }
      catch (e) { if (e.status === 401) return null; throw e; }
    },

    registerEmail({ email, password, display_name }) {
      return request('/api/auth/email/register', {
        method: 'POST',
        body: { email, password, display_name },
      });
    },

    loginEmail({ email, password }) {
      return request('/api/auth/email/login', {
        method: 'POST',
        body: { email, password },
      });
    },

    forgotPassword({ email }) {
      return request('/api/auth/email/forgot', {
        method: 'POST',
        body: { email },
      });
    },

    resetPassword({ token, new_password }) {
      return request('/api/auth/email/reset', {
        method: 'POST',
        body: { token, new_password },
      });
    },

    refresh() {
      return request('/api/auth/refresh', { method: 'POST' });
    },

    logout() {
      return request('/api/auth/logout', { method: 'POST' });
    },

    unbindIdentity(identityId) {
      return request('/api/auth/identity/' + encodeURIComponent(identityId), {
        method: 'DELETE',
      });
    },

    redirectToLogin() {
      const next = encodeURIComponent(location.pathname + location.search);
      location.assign('/login?next=' + next);
    },
  };

  // Wrap fetch so any 401 from /api/* triggers a refresh attempt, then login redirect.
  const originalFetch = window.fetch.bind(window);
  let refreshing = null;
  window.fetch = async function (input, init) {
    init = init || {};
    if (!('credentials' in init)) init.credentials = 'include';
    const url = typeof input === 'string' ? input : input.url;
    const res = await originalFetch(input, init);
    if (res.status !== 401 || !url || !url.includes('/api/')) return res;
    // Don't try to refresh on the refresh endpoint itself or on auth/me.
    if (url.includes('/api/auth/') || url.endsWith('/api/me')) return res;
    if (!refreshing) {
      refreshing = Auth.refresh()
        .then(() => true)
        .catch(() => false)
        .finally(() => { setTimeout(() => { refreshing = null; }, 100); });
    }
    const ok = await refreshing;
    if (!ok) {
      Auth.redirectToLogin();
      return res;
    }
    return originalFetch(input, init);
  };

  window.Auth = Auth;
})();
