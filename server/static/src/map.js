/* Map view: MapLibre basemap with the noise field, glowing streets, stations,
 * the user's position and the selection. Positions in and out of this module
 * are world coordinates (metres, see NM.geo). */
window.NM = window.NM || {};

NM.map = (function () {
  const U = NM.util, D = NM.data, G = NM.geo;
  const ll = (x, y) => G.toLngLat(x, y);

  let map = null;
  let ready = false;
  let state = { stations: [] }; // last data from refresh(), applied once the style has loaded
  let selectionMarker = null, userMarker = null;
  let onTap = () => {};

  // ---- Setup -------------------------------------------------------------------
  function init(el, opts = {}) {
    onTap = opts.onTap || onTap;
    const V = D.initialView;
    map = new maplibregl.Map({
      container: el,
      style: NM.config.mapStyle,
      bounds: [[V.west, V.south], [V.east, V.north]],
      minZoom: 1,
      renderWorldCopies: false,
      maxZoom: 18.5,
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
    map.on('click', (e) => onTap(hitTest(e.point, e.lngLat)));
    map.on('mouseenter', 'stations-ring', () => (map.getCanvas().style.cursor = 'pointer'));
    map.on('mouseleave', 'stations-ring', () => (map.getCanvas().style.cursor = ''));
  }

  /** The first basemap label layer above the buildings: the overlay goes under it. */
  function labelLayerId() {
    const layers = map.getStyle().layers;
    const b = layers.findIndex((l) => l.id === 'building' || l['source-layer'] === 'building');
    const first = layers.find((l, i) => i > b && l.type === 'symbol' && l.layout && l.layout['text-field']);
    return first && first.id;
  }

  function onLoad() {
    const before = labelLayerId();

    map.addSource('noise', { type: 'image', url: NM.field.canvas.toDataURL(), coordinates: NM.field.corners() });
    map.addLayer({
      id: 'noise', type: 'raster', source: 'noise',
      paint: { 'raster-resampling': 'linear', 'raster-fade-duration': 0, 'raster-opacity': 1 },
    }, before);

    map.addSource('roads', { type: 'geojson', data: roadsGeoJSON() });
    const loud = ['>=', ['get', 'db'], 57];
    map.addLayer({
      id: 'roads-glow', type: 'line', source: 'roads', filter: loud,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': ['get', 'glow'],
        'line-opacity': 0.3,
        'line-width': ['interpolate', ['exponential', 1.6], ['zoom'], 12, 3, 15, 10, 18, 34],
        'line-blur': ['interpolate', ['exponential', 1.6], ['zoom'], 12, 2, 15, 6, 18, 20],
      },
    }, before);
    map.addLayer({
      id: 'roads-core', type: 'line', source: 'roads', filter: loud,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': ['get', 'core'],
        'line-opacity': ['interpolate', ['linear'], ['get', 'db'], 57, 0.25, 70, 0.85],
        'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 12, 0.6, 15, 1.6, 18, 4],
      },
    }, before);

    map.addSource('stations', { type: 'geojson', data: stationsGeoJSON() });
    // World view: a heat glow around each live station, fading out as the street model takes over.
    map.addLayer({
      id: 'stations-heat', type: 'heatmap', source: 'stations', maxzoom: 14, filter: ['!', ['get', 'stale']],
      paint: {
        'heatmap-weight': ['interpolate', ['linear'], ['get', 'db'], 35, 0.1, 80, 1],
        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 1, 1, 13, 2],
        'heatmap-radius': ['interpolate', ['exponential', 2], ['zoom'], 1, 12, 8, 30, 13, 70],
        'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.85, 14, 0],
        'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
          0, 'rgba(0,0,0,0)', 0.15, 'rgba(116,87,224,0.5)', 0.4, '#b453cc', 0.65, '#e8526c', 0.85, '#ff9f45', 1, '#ffd36a'],
      },
    }, before);
    map.addLayer({
      id: 'stations-ring', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 5, 12, 4, 16, 5.5],
        'circle-color': 'rgba(11,13,18,0.75)',
        'circle-stroke-width': 1.3,
        'circle-stroke-color': ['case', ['get', 'stale'], 'rgba(150,150,160,0.5)', 'rgba(255,255,255,0.7)'],
      },
    });
    map.addLayer({
      id: 'stations-dot', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 3, 12, 2, 16, 2.4],
        'circle-color': ['get', 'color'],
      },
    });

    ready = true;
    apply();
  }

  // ---- Data from app -------------------------------------------------------------
  /** Re-read levels from the field after it has been updated. */
  function refresh(stationList) {
    state = { stations: stationList };
    apply();
  }

  function apply() {
    if (!ready) return;
    map.getSource('noise').updateImage({ url: NM.field.canvas.toDataURL(), coordinates: NM.field.corners() });
    map.getSource('roads').setData(roadsGeoJSON());
    map.getSource('stations').setData(stationsGeoJSON());
    fitStationsOnce();
  }

  /** On the first data, zoom out to show every station if some are outside the start view. */
  let fitted = false;
  function fitStationsOnce() {
    if (fitted || !state.stations.length) return;
    fitted = true;
    const V = D.initialView;
    const pts = state.stations.map((s) => ll(s.x, s.y));
    if (pts.every(([lon, lat]) => lon >= V.west && lon <= V.east && lat >= V.south && lat <= V.north)) return;
    const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    map.fitBounds(b, { padding: 100, maxZoom: 13, duration: 0 });
  }

  /** A street's level: the 60th percentile of the field sampled every ~20 m along it. */
  function roadDb(road) {
    const samples = [];
    const pts = road.pts;
    for (let k = 0; k < pts.length - 1; k++) {
      const [ax, ay] = pts[k], [bx, by] = pts[k + 1];
      const n = Math.max(1, Math.round(Math.hypot(bx - ax, by - ay) / 20));
      for (let t = 0; t < n; t++) samples.push(NM.field.dbAt(ax + ((bx - ax) * t) / n, ay + ((by - ay) * t) / n));
    }
    samples.push(NM.field.dbAt(pts[pts.length - 1][0], pts[pts.length - 1][1]));
    samples.sort((a, b) => a - b);
    return samples[Math.floor(samples.length * 0.6)];
  }

  function roadsGeoJSON() {
    return {
      type: 'FeatureCollection',
      features: D.roads.map((road, i) => {
        const db = roadDb(road);
        const glow = NM.field.color(db + 2);
        const core = U.mix(NM.field.color(db + 5), [255, 236, 222], 0.3);
        return {
          type: 'Feature', id: i,
          geometry: { type: 'LineString', coordinates: road.coords },
          properties: { db, glow: U.rgba(glow, 1), core: U.rgba(core, 1) },
        };
      }),
    };
  }

  function stationsGeoJSON() {
    return {
      type: 'FeatureCollection',
      features: state.stations.map((s) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: ll(s.x, s.y) },
        properties: { id: s.id, db: s.db, stale: !!s.stale, color: s.stale ? '#6b6d75' : NM.category(s.db).color },
      })),
    };
  }

  // ---- Markers -------------------------------------------------------------------
  function marker(className) {
    const el = document.createElement('div');
    el.className = className;
    return new maplibregl.Marker({ element: el });
  }

  function select(sel) {
    if (!map) return;
    if (!sel) {
      if (selectionMarker) selectionMarker.remove();
      selectionMarker = null;
      return;
    }
    if (!selectionMarker) selectionMarker = marker('marker-selection');
    selectionMarker.setLngLat(ll(sel.x, sel.y)).addTo(map);
  }

  function setUser(p) {
    if (!map) return;
    if (!p) {
      if (userMarker) userMarker.remove();
      userMarker = null;
      return;
    }
    if (!userMarker) userMarker = marker('marker-user');
    userMarker.setLngLat(ll(p.x, p.y)).addTo(map);
  }

  // ---- Hit testing ----------------------------------------------------------------
  function metresPerPixel() {
    const lat = map.getCenter().lat;
    return (40075016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * Math.pow(2, map.getZoom()));
  }

  function hitTest(point, lngLat) {
    if (ready) {
      const r = 14;
      const hits = map.queryRenderedFeatures([[point.x - r, point.y - r], [point.x + r, point.y + r]], { layers: ['stations-ring'] });
      if (hits.length) {
        let best = null;
        for (const f of hits) {
          const st = state.stations.find((s) => s.id === f.properties.id);
          if (!st) continue;
          const q = map.project(ll(st.x, st.y));
          const d = Math.hypot(q.x - point.x, q.y - point.y);
          if (!best || d < best.d) best = { d, st };
        }
        if (best) return { type: 'station', id: best.st.id, x: best.st.x, y: best.st.y };
      }
    }
    if (!D.inArea(lngLat.lng, lngLat.lat)) return null; // no street model out here; only stations are tappable
    const p = G.toWorld(lngLat.lng, lngLat.lat);
    const mpp = metresPerPixel(), reach = 16 * mpp;
    let best = null;
    for (const road of D.roads) {
      const c = U.polyClosest(p.x, p.y, road.pts);
      const d = Math.sqrt(c.d2);
      if (d > reach) continue;
      const score = d - (road.name ? 6 * mpp : 0); // prefer named streets when close
      if (!best || score < best.score) best = { score, road, x: c.x, y: c.y };
    }
    if (best) return { type: 'road', road: best.road, x: best.x, y: best.y };
    return { type: 'point', x: p.x, y: p.y };
  }

  // ---- Camera ------------------------------------------------------------------
  /** Make (x, y) visible above a bottom sheet of height `inset` px. */
  function reveal(x, y, inset = 0, opts = {}) {
    if (!map) return;
    const { clientWidth: w, clientHeight: h } = map.getContainer();
    const q = map.project(ll(x, y));
    const top = 130, bottom = h - inset - 40;
    const zoom = opts.zoom ?? map.getZoom();
    if (!opts.force && zoom === map.getZoom() && q.x > 30 && q.x < w - 30 && q.y > top && q.y < bottom) return;
    const targetY = Math.max(top, (top + bottom) / 2);
    map.easeTo({ center: ll(x, y), zoom, offset: [0, targetY - h / 2], duration: 450 });
  }

  /** World position of the map centre. */
  function center() {
    if (!map) return { x: 0, y: 0 };
    const c = map.getCenter();
    return G.toWorld(c.lng, c.lat);
  }

  function showArea() {
    if (!map) return;
    const V = D.initialView;
    map.fitBounds([[V.west, V.south], [V.east, V.north]], { duration: 600 });
  }

  return { init, refresh, select, setUser, reveal, center, showArea };
})();
