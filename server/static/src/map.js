/* The MapLibre map: stations, a glow (or one of the experimental views) around
 * them, the user's position and the selected station. Positions are { lon, lat }.
 *
 * Views (setView):
 *   glow      a soft circle per live station, coloured by its level (default)
 *   surface   the level estimated between stations, see surface.js
 *   clusters  zoomed out, nearby stations merge into one bubble
 *   sources   like glow, but coloured by what makes the noise
 */
window.NM = window.NM || {};

NM.map = (function () {
  let map = null;
  let ready = false;
  let stations = []; // last list from refresh(), drawn once the style has loaded
  let view = 'glow';
  let selectionMarker = null, userMarker = null;
  let onTap = () => {};

  const VIEWS = ['glow', 'surface', 'clusters', 'sources'];
  // Glow radius around a station, in metres on the ground (constant at every
  // zoom), and the smallest it may shrink to on screen when zoomed far out.
  const GLOW_METERS = 70;
  const GLOW_MIN_PX = 6;
  // Metres per screen pixel at zoom 0 on the equator (512 px world).
  const METERS_PER_PX_Z0 = 40075016.686 / 512;
  // Clusters view: above this zoom every station is drawn on its own.
  const CLUSTER_MAX_ZOOM = 14;

  // [dB, colour, dB, colour, …] for interpolate expressions.
  const levelStops = () => NM.LEVEL_STOPS.flat();
  const levelColor = (input) => ['interpolate', ['linear'], input, ...levelStops()];
  const sourceColor = () => ['match', ['get', 'src'],
    ...Object.entries(NM.SOURCES).flatMap(([k, s]) => [k, s.color]), '#8d93a6'];
  // Energy mean of a cluster, in dB.
  const clusterMeanDb = ['*', 10, ['log10', ['/', ['get', 'esum'], ['get', 'point_count']]]];

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
      hash: 'at', // the view is kept in the URL (#at=zoom/lat/lon), so links reopen it
    });
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

    map.on('load', onLoad);
    map.on('error', (e) => console.error('[noise-map] map error', e.error || e));
    map.on('click', onClick);
    for (const id of ['stations-ring', 'clusters-bubble', 'clusters-single']) {
      map.on('mouseenter', id, () => (map.getCanvas().style.cursor = 'pointer'));
      map.on('mouseleave', id, () => (map.getCanvas().style.cursor = ''));
    }
  }

  /** First text label layer in the style; the glow goes underneath it. */
  function labelLayerId() {
    const first = map.getStyle().layers.find((l) => l.type === 'symbol' && l.layout && l.layout['text-field']);
    return first && first.id;
  }

  /** First road layer, so roads stay readable on top of the surface. */
  function roadLayerId() {
    const first = map.getStyle().layers.find((l) => l.type === 'line' && /^(highway|road|tunnel|bridge)/.test(l.id));
    return (first && first.id) || labelLayerId();
  }

  function onLoad() {
    map.addSource('stations', { type: 'geojson', data: stationsGeoJSON() });
    map.addSource('stations-live', {
      type: 'geojson', data: stationsGeoJSON(true),
      cluster: true, clusterRadius: 44, clusterMaxZoom: CLUSTER_MAX_ZOOM,
      clusterProperties: {
        esum: ['+', ['^', 10, ['/', ['get', 'db'], 10]]],
        dbmax: ['max', ['get', 'db']],
      },
    });

    // The surface view's image source and layer are added by renderSurface()
    // the first time it is shown, since an image source needs an image.

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
        'circle-color': levelColor(['get', 'db']),
        'circle-opacity': ['interpolate', ['linear'], ['get', 'db'], 40, 0.35, 80, 0.75],
        'circle-blur': 1,
      },
    }, labelLayerId());
    map.addLayer({
      id: 'stations-ring', type: 'circle', source: 'stations',
      paint: {
        // Small when zoomed out, so thousands of stations don't bury the colours under them.
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2.4, 13, 5, 16, 7],
        'circle-color': 'rgba(11,13,18,0.75)',
        'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 10, 0.6, 13, 1.3],
        'circle-stroke-color': ['case', ['get', 'stale'], 'rgba(150,150,160,0.5)', 'rgba(255,255,255,0.7)'],
      },
    });
    map.addLayer({
      id: 'stations-dot', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 1.6, 13, 3, 16, 4],
        'circle-color': ['get', 'color'],
      },
    });

    // Clusters: fill = energy mean of the stations inside, ring = the loudest one.
    map.addLayer({
      id: 'clusters-bubble', type: 'circle', source: 'stations-live', filter: ['has', 'point_count'],
      maxzoom: CLUSTER_MAX_ZOOM + 1, layout: { visibility: 'none' },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['sqrt', ['get', 'point_count']], 1.4, 13, 5, 20, 12, 30, 30, 42],
        'circle-color': levelColor(clusterMeanDb),
        'circle-opacity': 0.82,
        'circle-stroke-width': 2.5,
        'circle-stroke-color': levelColor(['get', 'dbmax']),
      },
    });
    map.addLayer({
      id: 'clusters-label', type: 'symbol', source: 'stations-live', filter: ['has', 'point_count'],
      maxzoom: CLUSTER_MAX_ZOOM + 1,
      layout: {
        visibility: 'none',
        'text-field': ['to-string', ['round', clusterMeanDb]],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
        'text-allow-overlap': true,
      },
      paint: { 'text-color': '#0b0d12' },
    });
    map.addLayer({
      id: 'clusters-single', type: 'circle', source: 'stations-live', filter: ['!', ['has', 'point_count']],
      maxzoom: CLUSTER_MAX_ZOOM + 1, layout: { visibility: 'none' },
      paint: {
        'circle-radius': 5,
        'circle-color': levelColor(['get', 'db']),
        'circle-stroke-width': 1.3,
        'circle-stroke-color': 'rgba(255,255,255,0.7)',
      },
    });

    ready = true;
    applyView();
    apply();
  }

  /** stationList: [{ id, lon, lat, db, stale, src }] (src = main noise source, optional) */
  function refresh(stationList) {
    stations = stationList;
    apply();
  }

  function apply() {
    if (!ready) return;
    map.getSource('stations').setData(stationsGeoJSON());
    map.getSource('stations-live').setData(stationsGeoJSON(true));
    if (view === 'surface') renderSurface();
    fitStationsOnce();
  }

  // ---- Views ------------------------------------------------------------------------
  function setView(v) {
    if (!VIEWS.includes(v)) v = 'glow';
    view = v;
    if (ready) { applyView(); apply(); }
  }

  function applyView() {
    const show = (id, on) => map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    const clusters = view === 'clusters';
    show('stations-glow', view === 'glow' || view === 'sources' || clusters);
    if (map.getLayer('surface')) show('surface', view === 'surface');
    for (const id of ['clusters-bubble', 'clusters-label', 'clusters-single']) show(id, clusters);
    // In the clusters view the individual stations take over once zoomed in.
    for (const id of ['stations-glow', 'stations-ring', 'stations-dot']) map.setLayerZoomRange(id, clusters ? CLUSTER_MAX_ZOOM + 1 : 0, 24);

    const bySource = view === 'sources';
    map.setPaintProperty('stations-glow', 'circle-color', bySource ? sourceColor() : levelColor(['get', 'db']));
    map.setPaintProperty('stations-dot', 'circle-color', bySource
      ? ['case', ['get', 'stale'], ['get', 'color'], sourceColor()]
      : ['get', 'color']);
  }

  function renderSurface() {
    const img = NM.surface.render(stations.filter((s) => !s.stale));
    const src = map.getSource('surface');
    if (!img) {
      if (map.getLayer('surface')) map.setLayoutProperty('surface', 'visibility', 'none');
      return;
    }
    if (src) src.updateImage(img);
    else {
      map.addSource('surface', { type: 'image', ...img });
      map.addLayer({
        id: 'surface', type: 'raster', source: 'surface',
        paint: { 'raster-opacity': 0.9, 'raster-resampling': 'linear', 'raster-fade-duration': 0 },
      }, roadLayerId());
    }
    map.setLayoutProperty('surface', 'visibility', view === 'surface' ? 'visible' : 'none');
  }

  /** The first time there's data, zoom to show all stations, unless the URL already says where to look. */
  let fitted = /[#&]at=/.test(location.hash);
  function fitStationsOnce() {
    if (fitted || !stations.length) return;
    fitted = true;
    const b = new maplibregl.LngLatBounds();
    stations.forEach((s) => b.extend([s.lon, s.lat]));
    map.fitBounds(b, { padding: 100, maxZoom: 14, duration: 0 });
  }

  function stationsGeoJSON(liveOnly = false) {
    return {
      type: 'FeatureCollection',
      features: stations.filter((s) => !liveOnly || !s.stale).map((s) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        properties: {
          id: s.id, db: s.db, stale: !!s.stale, src: s.src || '',
          color: s.stale ? '#6b6d75' : NM.category(s.db).color,
          // Glow radius in pixels at zoom 0; doubles with each zoom level.
          r0: GLOW_METERS / (METERS_PER_PX_Z0 * Math.cos((s.lat * Math.PI) / 180)),
        },
      })),
    };
  }

  /** Tapping a cluster zooms into it; tapping a station opens it. */
  async function onClick(e) {
    if (ready && view === 'clusters') {
      const [c] = map.queryRenderedFeatures(e.point, { layers: ['clusters-bubble'] });
      if (c) {
        const zoom = await map.getSource('stations-live').getClusterExpansionZoom(c.properties.cluster_id);
        map.easeTo({ center: c.geometry.coordinates, zoom: Math.min(zoom, CLUSTER_MAX_ZOOM + 1), duration: 400 });
        return;
      }
    }
    onTap(stationAt(e.point));
  }

  /** The station nearest to a tapped screen point, or null. */
  function stationAt(point) {
    if (!ready) return null;
    const r = 14;
    const hits = map.queryRenderedFeatures([[point.x - r, point.y - r], [point.x + r, point.y + r]], { layers: ['stations-ring', 'clusters-single'].filter((id) => map.getLayer(id)) });
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

  function noAutoFit() { fitted = true; }
  return { init, refresh, setView, select, setUser, reveal, VIEWS, noAutoFit, debug: () => map };
})();
