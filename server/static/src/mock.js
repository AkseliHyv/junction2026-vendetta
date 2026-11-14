/* Demo feed for ?demo: made-up payloads in the same format the boxes send.
 * Each station sends one payload per 15 minutes with 15 one-minute readings.
 * Values depend only on station and minute, so reloading shows the same history.
 */
window.NM = window.NM || {};

NM.mock = (function () {
  const T = NM.time, U = NM.util;

  // Per station: dB added to the daily curve, and the mix of sources.
  const CHARACTER = {
    'otaniemi-01': { bias: () => -1, mix: { traffic: 0.45, voices: 0.3, nature: 0.17, construction: 0.08 } },
    'otaniemi-02': { bias: () => -3, mix: { nature: 0.55, voices: 0.25, traffic: 0.18, construction: 0.02 } },
    'otaniemi-03': { bias: () => 0.5, mix: { traffic: 0.71, construction: 0.18, voices: 0.08, nature: 0.03 } },
    // Building site: loud on weekday working hours.
    'otaniemi-04': {
      bias: (h, dow) => (dow >= 1 && dow <= 5 && h >= 7 && h < 16 ? 7 : 0),
      mix: (h, dow) =>
        dow >= 1 && dow <= 5 && h >= 7 && h < 16
          ? { construction: 0.62, traffic: 0.24, voices: 0.12, nature: 0.02 }
          : { traffic: 0.5, voices: 0.35, nature: 0.13, construction: 0.02 },
    },
    'otaniemi-05': { bias: () => 1.5, mix: { traffic: 0.8, voices: 0.1, construction: 0.06, nature: 0.04 } },
    // Waterfront: busy evenings.
    'otaniemi-06': {
      bias: (h) => (h >= 17 && h < 23 ? 4 : 0),
      mix: (h) => (h >= 17 && h < 23 ? { voices: 0.58, traffic: 0.25, nature: 0.15, construction: 0.02 } : { nature: 0.42, traffic: 0.38, voices: 0.18, construction: 0.02 }),
    },
    'otaniemi-07': { bias: () => -2, mix: { traffic: 0.4, nature: 0.35, voices: 0.22, construction: 0.03 } },
    // Went offline 2 h ago — shows how a silent device is handled.
    'otaniemi-08': { bias: () => 0, mix: { traffic: 0.55, voices: 0.3, nature: 0.12, construction: 0.03 }, offlineFor: 2 * T.HOUR },
  };

  function rng(seed) {
    // mulberry32
    return function () {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  // Typical level by hour: quiet at night, peaks in the morning and afternoon rush.
  const DAILY_DB = [44, 42, 41, 41, 42, 46, 52, 57, 58, 56, 55, 55, 55, 55, 56, 57, 58, 58, 56, 53, 51, 49, 47, 45];
  const dailyDb = (hf) => {
    const h = Math.floor(hf) % 24;
    return U.lerp(DAILY_DB[h], DAILY_DB[(h + 1) % 24], hf - Math.floor(hf));
  };

  const hashStr = (s) => [...s].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) | 0, 7);

  function reading(station, t) {
    const ch = CHARACTER[station.id] || { bias: () => 0, mix: { traffic: 0.5, voices: 0.3, nature: 0.2 } };
    const minuteIdx = Math.floor(t / T.MIN);
    const rand = rng(hashStr(station.id) ^ minuteIdx);
    const hf = T.hourFrac(t);
    const h = Math.floor(hf);
    const dow = new Date(t).getUTCDay();

    // Slow drift (traffic waves) + per-minute jitter.
    const drift = 1.4 * Math.sin(minuteIdx / 9 + hashStr(station.id)) + 0.8 * Math.sin(minuteIdx / 37);
    const laeq = dailyDb(hf) + ch.bias(h, dow) + drift + (rand() - 0.5) * 2.4;

    const mix = typeof ch.mix === 'function' ? ch.mix(h, dow) : ch.mix;
    const raw = Object.fromEntries(Object.entries(mix).map(([k, v]) => [k, Math.max(0, v + (rand() - 0.5) * 0.1)]));
    const sum = Object.values(raw).reduce((a, b) => a + b, 0) || 1;
    const sources = Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, +(v / sum).toFixed(2)]));

    const windy = rand() < 0.03;
    return {
      minute: new Date(minuteIdx * T.MIN).toISOString().replace('.000Z', 'Z'),
      laeq_db: +laeq.toFixed(1),
      lmax_db: +(laeq + 7 + rand() * 9).toFixed(1),
      l90_db: +(laeq - 9 - rand() * 7).toFixed(1),
      sources,
      valid_s: windy ? 30 + Math.floor(rand() * 15) : 56 + Math.floor(rand() * 5),
      flags: windy ? ['wind'] : [],
    };
  }

  /** All payloads for windows that completed after `sinceMs` and by `now`. */
  function fetchSince(sinceMs, now = Date.now()) {
    const win = NM.config.windowMinutes * T.MIN;
    const payloads = [];
    for (const st of NM.data.stations) {
      const ch = CHARACTER[st.id] || {};
      const until = now - (ch.offlineFor || 0);
      const lastEnd = Math.floor(until / win) * win;
      for (let end = Math.max(Math.ceil(sinceMs / win) * win, lastEnd - 24 * T.HOUR); end <= lastEnd; end += win) {
        const readings = [];
        for (let t = end - win; t < end; t += T.MIN) readings.push(reading(st, t));
        payloads.push({ v: 1, station_id: st.id, readings });
      }
    }
    // Simulate network latency.
    return new Promise((res) => setTimeout(() => res(payloads), 250));
  }

  return { fetchSince };
})();
