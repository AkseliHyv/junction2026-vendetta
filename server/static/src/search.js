/* Search box: streets (from OSM), named places and measuring stations. */
window.NM = window.NM || {};

NM.search = (function () {
  const U = NM.util, D = NM.data;
  let input, list, onPick = () => {};
  let items = [], results = [], active = -1;

  function init(opts = {}) {
    onPick = opts.onPick || onPick;
    input = document.getElementById('search');
    list = document.getElementById('results');

    // OSM splits a street into many ways: group them by name.
    const streets = new Map();
    for (const r of D.roads) {
      if (!r.name) continue;
      if (!streets.has(r.name)) streets.set(r.name, []);
      streets.get(r.name).push(r);
    }
    const streetKind = (roads) => {
      const labels = new Set(roads.map((r) => r.label));
      return labels.has('Motorway') ? 'Motorway' : labels.has('Main road') ? 'Main road' : labels.size === 1 ? [...labels][0] : 'Street';
    };
    items = [
      ...[...streets].map(([name, roads]) => ({ type: 'road', name, kind: streetKind(roads), roads })),
      ...D.places.map((p) => ({ type: 'place', name: p.name, kind: p.kind, place: p, alt: p.alt })),
      ...D.stations.map((s) => ({ type: 'station', name: s.name, kind: `Sensor · ${s.id}`, station: s, alt: s.id })),
    ];

    input.addEventListener('input', render);
    input.addEventListener('focus', render);
    input.addEventListener('blur', () => setTimeout(close, 120));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!results.length) return;
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
        highlight();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (results.length) pick(results[Math.max(0, active)]);
      } else if (e.key === 'Escape') {
        input.value = '';
        close();
        input.blur();
      }
    });
    input.form.addEventListener('submit', (e) => e.preventDefault());
    list.addEventListener('pointerdown', (e) => {
      const li = e.target.closest('[data-i]');
      if (!li) return;
      e.preventDefault();
      pick(results[+li.dataset.i]);
    });
  }

  // "kehä" matches "keha", "vare" matches "Väre".
  const fold = (s) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '');

  function render() {
    const q = input.value.trim().toLowerCase();
    if (!q) return close();
    const score = (it) => {
      const n = it.name.toLowerCase();
      if (n.startsWith(q)) return 0;
      if (n.split(/\s+/).some((w) => w.startsWith(q))) return 1;
      if (n.includes(q) || (it.alt && it.alt.includes(q))) return 2;
      if (fold(n).includes(fold(q)) || (it.alt && fold(it.alt).includes(fold(q)))) return 3;
      return -1;
    };
    results = items
      .map((it) => [score(it), it])
      .filter(([s]) => s >= 0)
      .sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name))
      .slice(0, 6)
      .map(([, it]) => it);
    active = results.length ? 0 : -1;
    list.innerHTML = results.length
      ? results.map((it, i) => `<li data-i="${i}" role="option"><span class="r-name">${U.escape(it.name)}</span><span class="r-kind">${U.escape(it.kind)}</span></li>`).join('')
      : `<li class="r-empty">No places match “${U.escape(input.value.trim())}”</li>`;
    list.hidden = false;
    highlight();
  }

  function highlight() {
    [...list.children].forEach((li, i) => li.classList.toggle('active', i === active));
  }

  function close() {
    list.hidden = true;
    results = [];
    active = -1;
  }

  function pick(it) {
    input.value = '';
    close();
    input.blur();
    onPick(it);
  }

  return { init };
})();
