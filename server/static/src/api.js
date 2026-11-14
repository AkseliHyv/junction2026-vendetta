/* Data source. With NM.config.apiBase set, polls the backend; otherwise uses
 * the built-in demo feed. See README.md for the endpoint contract. */
window.NM = window.NM || {};

NM.api = {
  get isDemo() {
    return !NM.config.apiBase;
  },

  /**
   * Device payloads containing readings newer than `sinceMs`.
   * GET {apiBase}/readings?since=<ISO 8601>
   *   → [ <device payload>, ... ]   or   { "payloads": [ ... ] }
   */
  async fetchSince(sinceMs) {
    if (this.isDemo) return NM.mock.fetchSince(sinceMs);

    const url = new URL(NM.config.apiBase.replace(/\/$/, '') + '/readings', location.href);
    url.searchParams.set('since', new Date(sinceMs).toISOString());
    const res = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    return Array.isArray(body) ? body : body.payloads || [];
  },
};
