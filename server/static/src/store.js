/* Station readings store.
 *
 * Ingests device payloads exactly as the Arduino sends them:
 *   { v: 1, station_id, readings: [{ minute, laeq_db, lmax_db, l90_db,
 *     sources: {traffic, construction, voices, nature}, valid_s, flags }] }
 * keeps the last ~25 h per station, and aggregates per-minute readings into
 * 15-minute windows and hourly bars.
 */
window.NM = window.NM || {};

NM.store = (function () {
  const U = NM.util, T = NM.time;
  const KEEP_MS = 25 * T.HOUR;
  const stations = new Map(); // station_id → Map<minuteMs, reading>
  const unknownWarned = new Set();

  function ingest(payloads, now = Date.now()) {
    let accepted = 0, rejected = 0;
    for (const p of payloads || []) {
      if (!p || p.v !== 1 || typeof p.station_id !== 'string' || !Array.isArray(p.readings)) {
        rejected++;
        continue;
      }
      const known = NM.data.stations.some((s) => s.id === p.station_id);
      const placed = !NM.api.isDemo && NM.data.addStation(p.station_id, Number(p.lat), Number(p.lon));
      if (!known && !placed && !unknownWarned.has(p.station_id)) {
        unknownWarned.add(p.station_id);
        console.warn(
          `[noise-map] Station "${p.station_id}" sent no valid lat/lon (${p.lat},${p.lon}) — ` +
          `set DEVICE_LAT / DEVICE_LON in night-noise-box/python/config.py.`
        );
      }
      let map = stations.get(p.station_id);
      if (!map) stations.set(p.station_id, (map = new Map()));
      for (const r of p.readings) {
        const t = Date.parse(r && r.minute);
        if (!Number.isFinite(t) || !Number.isFinite(r.laeq_db)) { rejected++; continue; }
        map.set(t, r); // same minute re-sent → replaced, so overlapping polls are harmless
        accepted++;
      }
    }
    prune(now);
    return { accepted, rejected };
  }

  function prune(now) {
    for (const map of stations.values()) {
      for (const t of map.keys()) if (t < now - KEEP_MS) map.delete(t);
    }
  }

  const weight = (r) => (Number.isFinite(r.valid_s) ? r.valid_s : 60);

  /** Aggregate a list of [t, reading] into one window summary (or null). */
  function aggregate(entries) {
    // minValidSeconds is per 60 s; scale it for shorter readings (duration_s, e.g. 10 s).
    const min = NM.config.minValidSeconds / 60;
    const len = (r) => (Number.isFinite(r.duration_s) && r.duration_s > 0 ? r.duration_s : 60);
    const ok = entries.filter(([, r]) => weight(r) >= min * len(r));
    if (!ok.length) return null;
    let eSum = 0, wSum = 0, lmax = -Infinity;
    const src = {}, l90s = [], flags = new Set();
    for (const [, r] of ok) {
      const w = weight(r);
      eSum += w * U.dbToE(r.laeq_db);
      wSum += w;
      if (Number.isFinite(r.lmax_db)) lmax = Math.max(lmax, r.lmax_db);
      if (Number.isFinite(r.l90_db)) l90s.push(r.l90_db);
      for (const [k, v] of Object.entries(r.sources || {})) if (Number.isFinite(v)) src[k] = (src[k] || 0) + w * v;
      for (const f of r.flags || []) flags.add(f);
    }
    const srcTotal = Object.values(src).reduce((a, b) => a + b, 0);
    const sources = Object.entries(src)
      .map(([k, v]) => [k, srcTotal ? v / srcTotal : 0])
      .sort((a, b) => b[1] - a[1]);
    l90s.sort((a, b) => a - b);
    return {
      laeq: U.eToDb(eSum / wSum), // energy average, weighted by valid seconds
      lmax: Number.isFinite(lmax) ? lmax : null,
      l90: l90s.length ? l90s[l90s.length >> 1] : null, // median of per-minute L90s (approximation)
      sources,
      validS: wSum,
      totalS: entries.reduce((a, [, r]) => a + len(r), 0),
      minutes: entries.length,
      flags: [...flags],
    };
  }

  /** Latest window for a station: the 15 minutes ending at its newest reading. */
  function latest(id, now = Date.now()) {
    const map = stations.get(id);
    if (!map || !map.size) return null;
    let last = -Infinity;
    for (const t of map.keys()) if (t > last) last = t;
    const winMs = NM.config.windowMinutes * T.MIN;
    // A reading covers at least a minute; the server's 5-minute summaries cover their duration_s.
    const lastDur = Number(map.get(last).duration_s) * 1000;
    const end = last + Math.max(T.MIN, Number.isFinite(lastDur) ? lastDur : 0);
    const entries = [...map].filter(([t]) => t >= end - winMs && t < end);
    const agg = aggregate(entries);
    return {
      ...(agg || {}),
      ok: !!agg,
      start: end - winMs,
      end,
      stale: now - end > NM.config.staleAfterMinutes * T.MIN,
    };
  }

  /** Hourly LAeq for clock hours 0..23 over the last 24 h (null = no data). */
  function hourly(id, now = Date.now()) {
    const map = stations.get(id);
    const buckets = T.dayBuckets(now);
    const out = new Array(24).fill(null);
    if (!map) return out;
    const byStart = new Map(buckets.map((b, h) => [b, []]));
    for (const e of map) {
      const b = T.hourStart(e[0]);
      if (byStart.has(b)) byStart.get(b).push(e);
    }
    buckets.forEach((b, h) => {
      const agg = aggregate(byStart.get(b));
      out[h] = agg ? agg.laeq : null;
    });
    return out;
  }

  function newestMinute() {
    let last = -Infinity;
    for (const map of stations.values()) for (const t of map.keys()) if (t > last) last = t;
    return last;
  }

  return { ingest, latest, hourly, newestMinute };
})();
