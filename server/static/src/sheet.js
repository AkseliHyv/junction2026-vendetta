/* Bottom sheet: place / road / station details with the 24-hour chart. */
window.NM = window.NM || {};

NM.sheet = (function () {
  const U = NM.util;
  const BAR_MIN_DB = 40, BAR_MAX_DB = 76;
  let el, onClose = () => {};
  let open = false;

  function init(opts = {}) {
    el = document.getElementById('sheet');
    onClose = opts.onClose || onClose;
    el.querySelector('.sheet-close').addEventListener('click', () => hide(true));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && open) hide(true); });
    bindSwipe();
  }

  /**
   * @param {object} d
   *   title, db (current level), bars (24 values by clock hour, null = no data),
   *   nowHour, desc (optional override of the category description),
   *   detailsHtml (optional extra block), meta (optional footnote)
   * @returns sheet height in px
   */
  function show(d) {
    const cat = NM.category(d.db);
    el.querySelector('.sheet-title').textContent = d.title;
    el.querySelector('.reading-value').textContent = Number.isFinite(d.db) ? Math.round(d.db) : '–';
    el.querySelector('.category-dot').style.background = cat.color;
    el.querySelector('.category-label').textContent = cat.label;
    el.querySelector('.category-desc').textContent = d.desc || cat.desc;

    const bars = el.querySelector('.bars');
    bars.innerHTML = d.bars
      .map((v, h) => {
        if (v == null) return `<span class="bar bar-empty" title="${String(h).padStart(2, '0')}:00 · no data"></span>`;
        const pct = U.clamp((v - BAR_MIN_DB) / (BAR_MAX_DB - BAR_MIN_DB), 0.08, 1) * 100;
        const when = h > d.nowHour ? 'yesterday ' : '';
        return `<span class="bar" style="height:${pct.toFixed(1)}%;background:${NM.barColor(v)}" title="${when}${String(h).padStart(2, '0')}:00 · ${Math.round(v)} dB"></span>`;
      })
      .join('');
    el.querySelector('.now-dot').style.left = `calc(${((d.nowHour + 0.5) / 24) * 100}% - 2px)`;

    const details = el.querySelector('.sheet-details');
    details.innerHTML = d.detailsHtml || '';
    details.hidden = !d.detailsHtml;
    const meta = el.querySelector('.sheet-meta');
    meta.textContent = d.meta || '';
    meta.hidden = !d.meta;

    el.hidden = false;
    el.setAttribute('aria-hidden', 'false');
    // Force layout so the transition runs when first opened.
    void el.offsetHeight;
    el.classList.add('open');
    open = true;
    return el.getBoundingClientRect().height + 8;
  }

  function hide(user) {
    if (!open) return;
    open = false;
    el.classList.remove('open');
    el.setAttribute('aria-hidden', 'true');
    if (user) onClose();
  }

  function bindSwipe() {
    let y0 = null, dy = 0;
    const grab = el.querySelector('.sheet-grab');
    grab.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button')) return;
      y0 = e.clientY; dy = 0;
      grab.setPointerCapture(e.pointerId);
      el.style.transition = 'none';
    });
    grab.addEventListener('pointermove', (e) => {
      if (y0 == null) return;
      dy = Math.max(0, e.clientY - y0);
      el.style.transform = `translateY(${dy}px)`;
    });
    const end = () => {
      if (y0 == null) return;
      y0 = null;
      el.style.transition = '';
      el.style.transform = '';
      if (dy > 70) hide(true);
    };
    grab.addEventListener('pointerup', end);
    grab.addEventListener('pointercancel', end);
  }

  return { init, show, hide, get isOpen() { return open; } };
})();
