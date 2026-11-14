# Noise map — frontend

A static web map of noise in Otaniemi, Espoo. It has no build step and needs no API key. The basemap is [MapLibre GL](https://maplibre.org) (loaded from a CDN) with [OpenFreeMap](https://openfreemap.org) dark vector tiles. Streets come from OpenStreetMap. Serve the folder:

```sh
npx serve .            # or: python3 -m http.server
```

Opening `index.html` from `file://` also works, but "my location" needs `http://localhost` or HTTPS.

Without a backend it runs on a **demo feed**: synthetic payloads in the exact device format, from 8 stations, with 24 h of history. One station is deliberately offline. The status chip under the search bar says "Demo data" when this is the case.

## How the map works

1. **Stations** (Arduino UNO Q) upload one analysis per 15-minute window: 15 per-minute readings.
2. The frontend polls the backend **20 s after every quarter hour** (`:00 :15 :30 :45`). It also polls when the tab becomes visible again.
3. For each station, the latest window is aggregated:
   - **LAeq**: energy average of `laeq_db`, weighted by `valid_s`
   - **Lmax**: max of `lmax_db`
   - **L90**: median of the per-minute `l90_db` (an approximation)
   - **sources**: `valid_s`-weighted mean, normalised
   - **flags**: union over the window

   Minutes with `valid_s < 20` are ignored. A station silent for more than 40 min is shown as offline and stops influencing the map.
4. The **noise field** is a 10 m grid over the area (`NM.data.area`). It is a propagation model of the real OSM street network with daily traffic profiles. Each street is a line source: its peak-hour level comes from its road class, adjusted for speed limit and lanes. Its level drops about 3 dB per doubling of distance, plus extra attenuation for ground and buildings. Tunnels are left out. The field is corrected by the stations: each station's residual (measured − modelled) is spread with a Gaussian kernel of about 320 m. The map is exact near stations and falls back to the model far from them. Buildings are not modelled individually, so shielding behind them is underestimated.
5. Tapping a street or spot shows its current estimate and a 24-hour chart. Tapping a sensor shows its measured window: peak, background, valid audio, sound sources and flags.

## Backend contract (to implement)

Set the base URL in `index.html` (`window.NM_CONFIG = { apiBase: 'https://…/api' }`) or open the page with `?api=https://…/api`.

```
GET {apiBase}/readings?since=2026-11-14T12:30:00.000Z
```

Return every stored device payload that contains readings newer than `since`, **unchanged from what the device sent**. You can return either a bare array or `{ "payloads": [...] }`:

```json
[
  {
    "v": 1,
    "station_id": "otaniemi-03",
    "readings": [
      {
        "minute": "2026-11-14T13:00:00Z",
        "laeq_db": 68.2,
        "lmax_db": 81.5,
        "l90_db": 52.0,
        "sources": { "traffic": 0.71, "construction": 0.18, "voices": 0.08, "nature": 0.03 },
        "valid_s": 58,
        "flags": []
      }
    ]
  }
]
```

- The first request asks for the last 24 h. Later requests overlap by 1 h, and duplicate minutes are de-duplicated client-side, so re-sending data is harmless.
- If the API is on another origin, it must send CORS headers.
- Only `v: 1` is accepted.

## Registering a station

Station positions live in `src/data.js` → `stations` (`id` = the device's `station_id`, `lat`/`lon` = where it is mounted). **The positions there now are placeholders for the demo.** Replace them with the real ones. Readings from unknown station ids are ignored, with a console warning.

## Basemap

`NM_CONFIG.mapStyle` (in `index.html`) takes any MapLibre style URL. The default is `https://tiles.openfreemap.org/styles/dark`, which is free and needs no key. To use MapTiler, Stadia, etc., paste their style URL with your key, e.g. `https://api.maptiler.com/maps/dataviz-dark/style.json?key=…`. The noise overlay is inserted under the style's first label layer.

## Updating the street data

`src/osm-roads.js` is a snapshot of OSM highways and light-rail lines in the area (driveways, parking aisles and tunnels are removed, and geometry is simplified to 2.5 m). To refresh it or cover a larger area, follow the instructions at the top of `tools/osm_to_js.py`. If you enlarge the area, also update `area` in `src/data.js`. Map data © OpenStreetMap contributors (ODbL); the attribution is shown on the map.

## Files

| File | Purpose |
| --- | --- |
| `src/config.js` | API URL, time zone (`Europe/Helsinki`), window/poll/stale settings |
| `src/osm-roads.js` | OpenStreetMap street snapshot (generated) |
| `src/data.js` | Projection, area, road-class levels, places, stations, noise categories |
| `src/field.js` | Propagation model, station correction, colour map |
| `src/store.js` | Ingests device payloads, aggregates windows and hourly bars |
| `src/api.js` / `src/mock.js` | Backend client / demo feed |
| `src/map.js` | MapLibre map: noise overlay, street glow, stations, markers, hit-testing |
| `src/sheet.js`, `src/search.js` | Bottom sheet and search |
| `src/app.js` | Wiring, the 15-minute refresh loop, my location |
| `tools/osm_to_js.py` | Regenerates `src/osm-roads.js` from an Overpass download |
