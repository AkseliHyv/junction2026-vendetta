/* Experimental map controls: the view switcher, the time slider and the legend
 * that goes with the chosen view. The view is kept in the URL (?view=…). */
window.NM = window.NM || {};

NM.controls = (function () {
  const T = NM.time, U = NM.util;
  const VIEW_LABELS = { glow: 'Glow', surface: 'Surface', clusters: 'Clusters', sources: 'Sources' };
  const STEP = () => NM.config.windowMinutes * T.MIN;
  const PLAY_MS = 450; // per step while playing

  let onView = () => {}, onTime = () => {};
  let range, timeLabel, playBtn, liveBtn, legend;
  let bounds = { min: 0, max: 0 };
  let time = null; // null = live, else end of the shown window (ms)
  let playTimer = null;

  function init(opts = {}) {
    onView = opts.onView || onView;
    onTime = opts.onTime || onTime;

    const views = document.getElementById('views');
    views.innerHTML = NM.map.VIEWS.map((v) => `<button type="button" role="radio" data-view="${v}">${VIEW_LABELS[v]}</button>`).join('');
    views.addEventListener('click', (e) => {
      const b = e.target.closest('[data-view]');
      if (b) setView(b.dataset.view);
    });

    const bar = document.getElementById('timebar');
    range = bar.querySelector('.tb-range');
    timeLabel = bar.querySelector('.tb-time');
    playBtn = bar.querySelector('.tb-play');
    liveBtn = bar.querySelector('.tb-live');
    legend = document.querySelector('.legend');

    // Scrubbing redraws at most once a frame; the surface view is not free.
    let pending = false;
    range.addEventListener('input', () => {
      stop();
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; setTime(Number(range.value)); });
    });
    playBtn.addEventListener('click', () => (playTimer ? stop() : play()));
    liveBtn.addEventListener('click', () => { stop(); setTime(null); });

    setView(new URLSearchParams(location.search).get('view') || 'glow', { initial: true });
  }

  // ---- View ----------------------------------------------------------------------------
  function setView(v, { initial = false } = {}) {
    if (!NM.map.VIEWS.includes(v)) v = 'glow';
    for (const b of document.querySelectorAll('#views [data-view]')) b.setAttribute('aria-checked', String(b.dataset.view === v));
    const url = new URL(location.href);
    if (v === 'glow') url.searchParams.delete('view');
    else url.searchParams.set('view', v);
    history.replaceState(null, '', url);
    renderLegend(v);
    onView(v, initial);
  }

  function renderLegend(v) {
    if (v === 'sources') {
      legend.classList.add('legend-sources');
      legend.setAttribute('aria-label', 'Colours of the main noise source');
      legend.innerHTML = Object.values(NM.SOURCES)
        .map((s) => `<span class="lg-src"><i style="background:${s.color}"></i>${U.escape(s.label)}</span>`)
        .join('');
      return;
    }
    legend.classList.remove('legend-sources');
    legend.setAttribute('aria-label', 'Colour scale from 40 to 80 decibels');
    const note = v === 'clusters' ? '<span class="lg-note">fill = average · ring = loudest</span>' : '';
    legend.innerHTML = `<span>40</span><i class="legend-bar"></i><span class="legend-max">80<br />dB</span>${note}`;
  }

  // ---- Time ------------------------------------------------------------------------------
  /** Called after each data refresh with the oldest and newest reading times. */
  function setBounds(oldest, newest) {
    if (!Number.isFinite(oldest) || !Number.isFinite(newest)) return;
    const step = STEP();
    bounds = { min: Math.ceil(oldest / step) * step + step, max: Math.floor(newest / step) * step + step };
    range.min = bounds.min;
    range.max = bounds.max;
    range.step = step;
    if (time != null) time = U.clamp(time, bounds.min, bounds.max);
    sync();
  }

  function setTime(t) {
    time = t == null || t >= bounds.max ? null : U.clamp(t, bounds.min, bounds.max);
    sync();
    onTime(time);
  }

  function sync() {
    range.value = time == null ? bounds.max : time;
    timeLabel.textContent = time == null ? 'Live' : T.hhmm(time);
    liveBtn.hidden = time == null;
    document.getElementById('timebar').dataset.live = String(time == null);
  }

  function play() {
    if (bounds.max <= bounds.min) return;
    // From live, replay the whole history; otherwise carry on from here.
    if (time == null) setTime(bounds.min);
    playBtn.setAttribute('aria-label', 'Pause');
    playBtn.dataset.playing = 'true';
    playTimer = setInterval(() => {
      const next = (time ?? bounds.max) + STEP();
      setTime(next);
      if (time == null) stop();
    }, PLAY_MS);
  }

  function stop() {
    clearInterval(playTimer);
    playTimer = null;
    playBtn.setAttribute('aria-label', 'Play the last 24 hours');
    playBtn.dataset.playing = 'false';
  }

  return { init, setBounds, setTime, get time() { return time; } };
})();
