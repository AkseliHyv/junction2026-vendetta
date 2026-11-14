/* Geography and static data for Otaniemi, Espoo.
 *
 * World units are metres on a local flat projection centred on the area
 * (x → east, y → south, so the grid rows run top to bottom like an image).
 * Over a few kilometres the error compared with Web Mercator is below 2 m.
 *
 * Streets come from OpenStreetMap (src/osm-roads.js). Each street gets a
 * modelled peak-hour level from its class, speed limit and lane count. The
 * level varies by hour of day following the profile for its `kind`.
 */
window.NM = window.NM || {};

NM.geo = (function () {
  const LAT0 = 60.183, LON0 = 24.8275;
  const KX = Math.cos((LAT0 * Math.PI) / 180) * 111320; // metres per degree of longitude
  const KY = 110574; // metres per degree of latitude
  return {
    toWorld: (lon, lat) => ({ x: (lon - LON0) * KX, y: (LAT0 - lat) * KY }),
    toLngLat: (x, y) => [LON0 + x / KX, LAT0 - y / KY],
  };
})();

NM.data = (function () {
  const G = NM.geo, U = NM.util;

  // Area covered by the noise model (lon/lat). The map can't be panned outside it.
  const area = { west: 24.785, south: 60.166, east: 24.87, north: 60.2 };
  // What the map shows first: the Otaniemi campus.
  const initialView = { west: 24.814, south: 60.177, east: 24.842, north: 60.191 };

  const tl = G.toWorld(area.west, area.north), br = G.toWorld(area.east, area.south);
  const world = { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y };

  // dB offsets from peak for each hour 00..23, by source kind.
  const profiles = {
    arterial: [-13, -15, -17, -18, -17, -12, -5, -1, 0, -1, -2, -2, -2, -2, -1.5, -1, -0.5, 0, -1, -4, -6, -8, -9, -11],
    street:   [-11, -13, -14, -15, -14, -10, -5, -2, -1, -1.5, -2, -2, -1.5, -1.5, -1.5, -1, -0.5, 0, -0.5, -2, -4, -6, -8, -9],
    lane:     [-8, -10, -11, -12, -11, -9, -6, -3, -2, -2, -2, -1.5, -1, -1, -1.5, -1, 0, 0, -0.5, -1.5, -3, -4, -5, -6],
    ambient:  [-6, -7, -8, -8, -8, -6, -4, -2, -1, -1, 0, 0, 0, 0, 0, 0, 0, 0, -1, -2, -3, -4, -5, -5],
  };

  // Peak-hour LAeq at the carriageway, by OSM class, with the speed limit (km/h)
  // and lanes per carriageway the level is meant for.
  const roadClasses = {
    motorway:       { kind: 'arterial', level: 75, speed: 80, lanes: 2, label: 'Motorway' },
    trunk:          { kind: 'arterial', level: 73, speed: 70, lanes: 2, label: 'Main road' },
    primary:        { kind: 'arterial', level: 68, speed: 50, lanes: 2, label: 'Main road' },
    secondary:      { kind: 'street', level: 65, speed: 50, lanes: 2, label: 'Street' },
    tertiary:       { kind: 'street', level: 61, speed: 40, lanes: 2, label: 'Street' },
    motorway_link:  { kind: 'arterial', level: 68, label: 'Ramp' },
    trunk_link:     { kind: 'arterial', level: 67, label: 'Ramp' },
    primary_link:   { kind: 'arterial', level: 64, label: 'Ramp' },
    secondary_link: { kind: 'street', level: 61, label: 'Street' },
    tertiary_link:  { kind: 'street', level: 58, label: 'Street' },
    unclassified:   { kind: 'lane', level: 56, speed: 40, label: 'Street' },
    residential:    { kind: 'lane', level: 53, speed: 30, label: 'Street' },
    living_street:  { kind: 'lane', level: 49, label: 'Street' },
    service:        { kind: 'lane', level: 49, label: 'Service road' },
    pedestrian:     { kind: 'ambient', level: 52, label: 'Pedestrian street' },
    light_rail:     { kind: 'street', level: 60, label: 'Light rail' },
    tram:           { kind: 'street', level: 60, label: 'Tram line' },
  };

  function roadLevel(r, c) {
    let level = c.level;
    if (r.s && c.speed) level += U.clamp(20 * Math.log10(r.s / c.speed), -4, 4);
    if (r.l && c.lanes) level += U.clamp(10 * Math.log10(r.l / c.lanes), -2, 3);
    if (r.bp) level -= 6; // passes under a building
    return level;
  }

  const roads = [];
  for (const r of NM.osmRoads || []) {
    const c = roadClasses[r.h];
    if (!c || r.c.length < 4) continue;
    const coords = [], pts = [];
    for (let i = 0; i < r.c.length; i += 2) {
      coords.push([r.c[i], r.c[i + 1]]);
      const p = G.toWorld(r.c[i], r.c[i + 1]);
      pts.push([p.x, p.y]);
    }
    roads.push({ name: r.n || null, cls: r.h, label: c.label, kind: c.kind, level: roadLevel(r, c), coords, pts });
  }

  // Named spots for search and for naming a tapped point. `alt` is also searched.
  const places = [
    ['Otaniemi', 'Neighbourhood', 60.18794, 24.83182],
    ['Keilaniemi', 'Neighbourhood', 60.17463, 24.83212],
    ['Tapiola', 'Neighbourhood', 60.17664, 24.80317],
    ['Lehtisaari', 'Island', 60.18104, 24.85066],
    ['Kuusisaari', 'Island', 60.18654, 24.86502],
    ['Maari', 'Neighbourhood', 60.18586, 24.81548],
    ['Teekkari Village', 'Neighbourhood', 60.18935, 24.8365, 'teekkarikylä'],
    ['Kivimies', 'Neighbourhood', 60.18189, 24.8282],
    ['Itäranta', 'Neighbourhood', 60.17575, 24.82357],
    ['Otsolahti', 'Neighbourhood', 60.1775, 24.8164],
    ['Pohjois-Tapiola', 'Neighbourhood', 60.18596, 24.79372],
    ['Tietäjä', 'Neighbourhood', 60.18818, 24.80521],
    ['Laajalahti', 'Neighbourhood', 60.19975, 24.79577],
    ['Aalto University', 'Metro station', 60.18446, 24.82357, 'aalto-yliopisto metro'],
    ['Keilaniemi', 'Metro station', 60.17565, 24.82948, 'metro'],
    ['Tapiola', 'Metro station', 60.17502, 24.80382, 'metro'],
    ['Dipoli', 'Building', 60.18513, 24.83244],
    ['Undergraduate Centre', 'Building', 60.1863, 24.82852, 'kandidaattikeskus'],
    ['Harald Herlin Learning Centre', 'Building', 60.18476, 24.8279, 'library oppimiskeskus'],
    ['Marsio', 'Building', 60.18645, 24.82588],
    ['Otaniemi Chapel', 'Building', 60.18937, 24.83725, 'otaniemen kappeli'],
    ['Otahalli', 'Sports centre', 60.18425, 24.83508],
    ['Otaniemi Sports Park', 'Sports ground', 60.18564, 24.83549, 'otaniemen urheilupuisto'],
    ['Alvar Aalto Park', 'Park', 60.18498, 24.82893, 'alvar aallon puisto'],
    ['Keilaniemi Park', 'Park', 60.17519, 24.82748, 'keilaniemenpuisto'],
    ['Maarinranta', 'Park', 60.19166, 24.81523],
    ['Lehtisaari beach', 'Beach', 60.17471, 24.8505, 'lehtisaaren uimaranta'],
    ['Westend beach', 'Beach', 60.16845, 24.80995, 'westendin uimaranta'],
  ].map(([name, kind, lat, lon, alt]) => ({ name, kind, lat, lon, alt, ...G.toWorld(lon, lat) }));

  // Measuring stations (Arduino UNO Q devices). `id` must match the device's
  // `station_id`; lat/lon is where the device is mounted.
  // These are placeholder positions for the demo: replace them with the real ones.
  const stations = [
    { id: 'otaniemi-01', name: 'Otakaari', lat: 60.18676, lon: 24.82742 },
    { id: 'otaniemi-02', name: 'Alvar Aalto Park', lat: 60.18498, lon: 24.82893 },
    { id: 'otaniemi-03', name: 'Kehä I at Otaniemi', lat: 60.18257, lon: 24.81556 },
    { id: 'otaniemi-04', name: 'Otaniementie', lat: 60.1849, lon: 24.82606 },
    { id: 'otaniemi-05', name: 'Länsiväylä', lat: 60.1689, lon: 24.82904 },
    { id: 'otaniemi-06', name: 'Otaranta shore', lat: 60.184, lon: 24.8364 },
    { id: 'otaniemi-07', name: 'Teekkari Village', lat: 60.18907, lon: 24.83822 },
    { id: 'otaniemi-08', name: 'Maarintie', lat: 60.18702, lon: 24.81394 },
  ].map((s) => ({ ...s, ...G.toWorld(s.lon, s.lat) }));

  return { area, initialView, world, profiles, roadClasses, roads, places, stations };
})();

/* Noise categories, shared by the map sheet and the meter. */
NM.category = function (db) {
  if (db < 45) return { label: 'Quiet', desc: 'Calm, like a quiet room', color: '#7457e0' };
  if (db < 56) return { label: 'Moderate', desc: 'Quiet street', color: '#b453cc' };
  if (db < 65) return { label: 'Noisy', desc: 'Steady traffic', color: '#e8526c' };
  if (db < 75) return { label: 'Loud', desc: 'Busy traffic', color: '#ff9f45' };
  return { label: 'Very loud', desc: 'Heavy traffic', color: '#ffd36a' };
};

/* Bar colour in the hourly chart (finer steps at the quiet end, as in the design). */
NM.barColor = function (db) {
  if (db < 52) return '#6f52dc';
  if (db < 60) return '#b04fc8';
  if (db < 65) return '#e5506a';
  if (db < 75) return '#ff9f45';
  return '#ffd36a';
};
