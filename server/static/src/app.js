/* Wiring: polls for data, and connects the map, the station sheet and search. */
(function () {
  const U = NM.util, T = NM.time, D = NM.data;
  const SOURCE_LABEL = { traffic: 'Traffic', construction: 'Construction', voices: 'Voices', nature: 'Nature' };

  let selected = null; // the station shown in the sheet: { id, lon, lat }
  let lastRefresh = 0;
  let timer = null;
  let liveStations = [];
  let status = null, flashTimer = null;
  const statusEl = document.getElementById('status');

  // ---- Boot -----------------------------------------------------------------------
  try {
    NM.map.init(document.getElementById('map'), { onTap: openStation });
  } catch (err) {
    // No MapLibre (CDN unreachable) or no WebGL: keep the rest of the UI alive.
    console.error('[noise-map] map failed to start', err);
    flash(window.maplibregl ? 'This browser cannot show the map (WebGL off?)' : 'Map library failed to load');
  }
  NM.map.refresh([]);
  NM.sheet.init({ onClose: () => { selected = null; NM.map.select(null); } });
  NM.search.init({ onPick });
  document.getElementById('locate').addEventListener('click', locate);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && Date.now() - lastRefresh > NM.config.windowMinutes * T.MIN) refresh();
  });
  refresh(true);

  // ---- Data refresh -----------------------------------------------------------------
  async function refresh(initial = false) {
    clearTimeout(timer);
    if (initial) setStatus('loading', 'Loading measurements…');
    try {
      const newest = NM.store.newestMinute();
      const since = Number.isFinite(newest) ? newest - T.HOUR : Date.now() - 24 * T.HOUR;
      const payloads = await NM.api.fetchSince(since);
      const { rejected } = NM.store.ingest(payloads);
      if (rejected) console.warn(`[noise-map] ${rejected} malformed payloads/readings skipped`);
      lastRefresh = Date.now();
      recompute();
    } catch (err) {
      console.error('[noise-map] refresh failed', err);
      const when = liveStations.length ? ` · showing ${T.hhmm(Math.max(...liveStations.map((s) => s.stats.end)))} data` : '';
      setStatus('error', `Can't reach server${when}`);
    }
    schedule();
  }

  /** Next poll shortly after the next quarter hour, when devices have uploaded. */
  function schedule() {
    const win = NM.config.windowMinutes * T.MIN;
    const now = Date.now();
    const next = Math.ceil((now + 1000) / win) * win + NM.config.pollDelaySeconds * 1000;
    timer = setTimeout(refresh, next - now);
  }

  function recompute() {
    const now = Date.now();
    const all = D.stations.map((st) => ({ ...st, stats: NM.store.latest(st.id, now) }));
    liveStations = all.filter((s) => s.stats && s.stats.ok && !s.stats.stale);
    const t = liveStations.length ? Math.max(...liveStations.map((s) => s.stats.end)) : now;

    NM.map.refresh(
      all.filter((s) => s.stats && s.stats.ok).map((s) => ({ id: s.id, lon: s.lon, lat: s.lat, db: s.stats.laeq, stale: s.stats.stale }))
    );

    const offline = all.length - liveStations.length;
    if (liveStations.length) {
      const label = NM.api.isDemo ? 'Demo data' : 'Live';
      setStatus(NM.api.isDemo ? 'demo' : 'live', `${label} · updated ${T.hhmm(t)}`,
        `${liveStations.length} of ${all.length} sensors reporting` + (offline ? ` · ${offline} offline` : ''));
    } else {
      setStatus('error', 'No recent measurements');
    }

    if (selected) openStation(selected, { keepView: true });
  }

  function setStatus(kind, text, title = '') {
    status = { kind, text, title };
    if (!flashTimer) showStatus(status);
  }

  function showStatus({ kind, text, title = '' }) {
    statusEl.dataset.state = kind;
    statusEl.querySelector('.status-text').textContent = text;
    statusEl.title = title;
  }

  /** Show a short message in the status chip, then go back to the data status. */
  function flash(text) {
    clearTimeout(flashTimer);
    showStatus({ kind: 'error', text });
    flashTimer = setTimeout(() => { flashTimer = null; if (status) showStatus(status); }, 4000);
  }

  // ---- My location -----------------------------------------------------------------
  function locate() {
    if (!navigator.geolocation) return flash('Location is not available in this browser');
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const p = { lon: pos.coords.longitude, lat: pos.coords.latitude };
        NM.map.setUser(p);
        NM.map.reveal(p, 0, { force: true, zoom: 15 });
      },
      (err) => flash(err.code === err.PERMISSION_DENIED ? 'Location permission denied' : 'Could not get your location'),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  }

  // ---- Selection -----------------------------------------------------------------
  /** Open the sheet for a station: { id, lon, lat }. Tapping empty map passes null. */
  function openStation(hit, opts = {}) {
    if (!hit) return;
    const now = Date.now();
    const st = D.stations.find((s) => s.id === hit.id);
    const stats = st && NM.store.latest(st.id, now);
    if (!stats || !stats.ok) return;
    selected = hit;
    const top = stats.sources[0];
    const sheetH = NM.sheet.show({
      title: st.name,
      db: stats.laeq,
      bars: NM.store.hourly(st.id, now),
      nowHour: T.hm(now).h,
      desc: stats.stale ? `Offline since ${T.hhmm(stats.end)}` : top ? `Mostly ${SOURCE_LABEL[top[0]]?.toLowerCase() || top[0]}` : undefined,
      detailsHtml: stationDetails(stats),
      meta: `Sensor ${st.id} · ${T.hhmm(stats.start)}–${T.hhmm(stats.end)}` + (stats.flags.length ? ` · flagged: ${stats.flags.join(', ')}` : ''),
    });
    NM.map.select(hit);
    if (!opts.keepView) NM.map.reveal(hit, sheetH);
  }

  function stationDetails(s) {
    const n = (v) => (v == null ? '–' : Math.round(v));
    const pct = Math.round((s.validS / s.totalS) * 100);
    const sources = s.sources
      .map(([k, v]) => `<li><span class="s-name">${U.escape(SOURCE_LABEL[k] || k)}</span><span class="s-track"><i style="width:${(v * 100).toFixed(0)}%"></i></span><span class="s-pct">${Math.round(v * 100)}%</span></li>`)
      .join('');
    return `
      <dl class="stats">
        <div><dt>Peak</dt><dd>${n(s.lmax)}<small>dB</small></dd></div>
        <div><dt>Background</dt><dd>${n(s.l90)}<small>dB</small></dd></div>
        <div><dt>Valid audio</dt><dd>${pct}<small>%</small></dd></div>
      </dl>
      ${sources ? `<ul class="sources" aria-label="Sound sources">${sources}</ul>` : ''}`;
  }

  function onPick(st) {
    openStation({ id: st.id, lon: st.lon, lat: st.lat });
  }
})();
