/* The MapLibre map: stations, a heat glow around them, the user's position and
 * the selected station. Positions are { lon, lat }. */
window.NM = window.NM || {};

NM.map = (function () {
  let map = null;
  let ready = false;
  let stations = []; // last list from refresh(), drawn once the style has loaded
  let selectionMarker = null, userMarker = null;
  let onTap = () => {};

  // Glow radius around a station, in metres on the ground (constant at every
  // zoom), and the smallest it may shrink to on screen when zoomed far out.
  const GLOW_METERS = 70;
  const GLOW_MIN_PX = 6;
  // Metres per screen pixel at zoom 0 on the equator (512 px world).
  const METERS_PER_PX_Z0 = 40075016.686 / 512;

  function init(el, opts = {}) {
    onTap = opts.onTap || onTap;
    map = new maplibregl.Map({
      container: el,
      style: NM.config.mapStyle,
      center: [15, 30],
      zoom: 1.5,
      minZoom: 1,
      maxZoom: 18.5,
      renderWorldCopies: false,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      attributionControl: false,
    });
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

    map.on('load', onLoad);
    map.on('error', (e) => console.error('[noise-map] map error', e.error || e));
    map.on('click', (e) => onTap(stationAt(e.point)));
    map.on('mouseenter', 'stations-ring', () => (map.getCanvas().style.cursor = 'pointer'));
    map.on('mouseleave', 'stations-ring', () => (map.getCanvas().style.cursor = ''));
  }

  /** First text label layer in the style; the heat glow goes underneath it. */
  function labelLayerId() {
    const first = map.getStyle().layers.find((l) => l.type === 'symbol' && l.layout && l.layout['text-field']);
    return first && first.id;
  }

  function onLoad() {
    map.addSource('stations', { type: 'geojson', data: stationsGeoJSON() });
    // Glow around each live station, coloured by that station's own level.
    // Deliberately not a `heatmap` layer: a heatmap sums nearby points, so a
    // cluster of quiet stations looked like one loud spot, and since its radius
    // was in screen pixels the clusters (and the fake hot spots) changed with
    // zoom. Each glow here has a fixed size on the ground and overlapping glows
    // only blend; the louder station is drawn on top.
    map.addLayer({
      id: 'stations-glow', type: 'circle', source: 'stations', filter: ['!', ['get', 'stale']],
      layout: { 'circle-sort-key': ['get', 'db'] },
      paint: {
        'circle-radius': ['interpolate', ['exponential', 2], ['zoom'],
          0, GLOW_MIN_PX,
          12, ['max', GLOW_MIN_PX, ['*', ['get', 'r0'], 2 ** 12]],
          22, ['*', ['get', 'r0'], 2 ** 22]],
        'circle-color': ['interpolate', ['linear'], ['get', 'db'],
          40, '#8a5ce6', 51.2, '#c057c4', 60.8, '#ec5f78', 71.2, '#ff9f45', 80, '#ffd98a'],
        'circle-opacity': ['interpolate', ['linear'], ['get', 'db'], 40, 0.35, 80, 0.75],
        'circle-blur': 1,
      },
    }, labelLayerId());
    map.addLayer({
      id: 'stations-ring', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 5, 12, 5, 16, 7],
        'circle-color': 'rgba(11,13,18,0.75)',
        'circle-stroke-width': 1.3,
        'circle-stroke-color': ['case', ['get', 'stale'], 'rgba(150,150,160,0.5)', 'rgba(255,255,255,0.7)'],
      },
    });
    map.addLayer({
      id: 'stations-dot', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 3, 12, 3, 16, 4],
        'circle-color': ['get', 'color'],
      },
    });

    ready = true;
    apply();
  }

  /** stationList: [{ id, lon, lat, db, stale }] */
  function refresh(stationList) {
    stations = stationList;
    apply();
  }

  function apply() {
    if (!ready) return;
    map.getSource('stations').setData(stationsGeoJSON());
    fitStationsOnce();
  }

  /** The first time there's data, zoom to show all stations. */
  let fitted = false;
  function fitStationsOnce() {
    if (fitted || !stations.length) return;
    fitted = true;
    const b = new maplibregl.LngLatBounds();
    stations.forEach((s) => b.extend([s.lon, s.lat]));
    map.fitBounds(b, { padding: 100, maxZoom: 14, duration: 0 });
  }

  function stationsGeoJSON() {
    return {
      type: 'FeatureCollection',
      features: stations.map((s) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        properties: {
          id: s.id, db: s.db, stale: !!s.stale, color: s.stale ? '#6b6d75' : NM.category(s.db).color,
          // Glow radius in pixels at zoom 0; doubles with each zoom level.
          r0: GLOW_METERS / (METERS_PER_PX_Z0 * Math.cos((s.lat * Math.PI) / 180)),
        },
      })),
    };
  }

  /** The station nearest to a tapped screen point, or null. */
  function stationAt(point) {
    if (!ready) return null;
    const r = 14;
    const hits = map.queryRenderedFeatures([[point.x - r, point.y - r], [point.x + r, point.y + r]], { layers: ['stations-ring'] });
    let best = null;
    for (const f of hits) {
      const st = stations.find((s) => s.id === f.properties.id);
      if (!st) continue;
      const q = map.project([st.lon, st.lat]);
      const d = Math.hypot(q.x - point.x, q.y - point.y);
      if (!best || d < best.d) best = { d, st };
    }
    return best ? { id: best.st.id, lon: best.st.lon, lat: best.st.lat } : null;
  }

  function marker(className) {
    const el = document.createElement('div');
    el.className = className;
    return new maplibregl.Marker({ element: el });
  }

  /** Highlight a position, or clear the highlight with null. */
  function select(pos) {
    if (!map) return;
    if (!pos) {
      if (selectionMarker) selectionMarker.remove();
      selectionMarker = null;
      return;
    }
    if (!selectionMarker) selectionMarker = marker('marker-selection');
    selectionMarker.setLngLat([pos.lon, pos.lat]).addTo(map);
  }

  function setUser(pos) {
    if (!map) return;
    if (!userMarker) userMarker = marker('marker-user');
    userMarker.setLngLat([pos.lon, pos.lat]).addTo(map);
  }

  /** Make a position visible above a bottom sheet `inset` px tall. */
  function reveal(pos, inset = 0, opts = {}) {
    if (!map) return;
    const { clientWidth: w, clientHeight: h } = map.getContainer();
    const q = map.project([pos.lon, pos.lat]);
    const top = 130, bottom = h - inset - 40;
    const zoom = opts.zoom ?? map.getZoom();
    if (!opts.force && zoom === map.getZoom() && q.x > 30 && q.x < w - 30 && q.y > top && q.y < bottom) return;
    const targetY = Math.max(top, (top + bottom) / 2);
    map.easeTo({ center: [pos.lon, pos.lat], zoom, offset: [0, targetY - h / 2], duration: 450 });
  }

  return { init, refresh, select, setUser, reveal };
})();
