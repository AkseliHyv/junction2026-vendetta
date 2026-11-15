# Noise map

The page `server.py` serves at `http://localhost:8080`. It's a world map: every box shows up at the `lat`/`lon` it sends, coloured by its latest level, with a glow around it. Tap a box to see its readings.

No build step and no API key. The basemap is [MapLibre GL](https://maplibre.org) from a CDN with [OpenFreeMap](https://openfreemap.org) tiles.

Open `http://localhost:8080/?demo` to see it with made-up data from 8 stations instead (one of them deliberately offline). "My location" needs `http://localhost` or HTTPS.

## How the map works

1. **Stations** (the boxes) upload readings to the server. The map was designed for one upload per 15 minutes containing 15 one-minute readings; shorter readings (the boxes currently send 10 s ones) work too.
2. The frontend polls the backend **20 s after every quarter hour** (`:00 :15 :30 :45`). It also polls when the tab becomes visible again.
3. For each station, the latest window is aggregated:
   - **LAeq**: energy average of `laeq_db`, weighted by `valid_s`
   - **Lmax**: max of `lmax_db`
   - **L90**: median of the per-minute `l90_db` (an approximation)
   - **sources**: `valid_s`-weighted mean, normalised
   - **flags**: union over the window

   Readings with less than 20 s of valid audio per minute are ignored. A station silent for more than 40 min is shown grey, as offline.
4. Tapping a station shows its latest level, a 24-hour chart, peak, background, valid audio, sound sources and flags.

### Map views (experimental)

The switcher in the corner picks how levels are drawn. The choice is kept in the URL (`?view=surface`), and so is the map position (`#at=zoom/lat/lon`), so a link reopens the same picture.

| View | What it shows |
| --- | --- |
| **Glow** (default) | A soft 70 m circle per live station in its own level's colour. Overlaps blend; nothing is added up. |
| **Surface** | The level estimated between stations: an inverse-distance average (in dB) of the stations within 250 m. Areas with no station nearby stay blank instead of looking quiet. Drawn under the roads. |
| **Clusters** | Zoomed out, nearby stations merge into one bubble: fill = energy-average level, ring = the loudest station, number = average dB. Tap to zoom in. |
| **Sources** | Like Glow, but coloured by the main noise source (traffic, voices, …). Brightness still follows the level. |

The **time slider** replays the last 24 hours in 15-minute windows (▶ plays them in order); the map, the station sheet and the status all follow the chosen time. **Live** goes back to the newest data.

## Backend

`server.py` implements this. To point the map at a different server, set `apiBase` in `index.html` or open the page with `?api=https://…/api`.

```
GET {apiBase}/readings?since=2026-11-14T12:30:00.000Z
```

It returns payloads with readings newer than `since`, either as a bare array or as `{ "payloads": [...] }`. `server.py` sends one payload per station whose readings are summaries: one per 5-minute window from the start of the hour an hour ago, one per hour before that (`duration_s` is the window's total). The map aggregates them like any other readings, and a day of 3000 stations stays around 100k readings. With `&raw=1` it returns the payloads exactly as the boxes sent them:

```json
[
  {
    "v": 1,
    "station_id": "box-01",
    "lat": 60.1868,
    "lon": 24.8274,
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

- The first request asks for the last 24 h. Later requests overlap by 1 h; duplicate readings are dropped in the browser, so re-sending data is harmless.
- An API on another origin must send CORS headers.
- Only `v: 1` is accepted.
- `lat`/`lon` place the station on the map. There's no list of stations to keep up to date; a payload without them is ignored with a console warning. The 8 stations in `src/data.js` are only for `?demo`.

## Basemap

`NM_CONFIG.mapStyle` (in `index.html`) takes any MapLibre style URL. The default is `https://tiles.openfreemap.org/styles/dark`, which is free and needs no key. To use MapTiler, Stadia, etc., paste their style URL with your key, e.g. `https://api.maptiler.com/maps/dataviz-dark/style.json?key=…`. The glow is drawn under the style's labels, the surface under its roads.

## Files

| File | Purpose |
| --- | --- |
| `src/config.js` | API URL, time zone (`Europe/Helsinki`), window/poll/stale settings |
| `src/data.js` | Stations and the colour scales |
| `src/store.js` | Ingests device payloads, aggregates windows and hourly bars |
| `src/api.js` / `src/mock.js` | Backend client / demo feed |
| `src/map.js` | MapLibre map: stations, the views above, markers |
| `src/surface.js` | Builds the Surface view's image |
| `src/controls.js` | View switcher, time slider, legend |
| `src/sheet.js`, `src/search.js` | Station sheet and station search |
| `src/app.js` | Wiring, the 15-minute refresh loop, my location |
