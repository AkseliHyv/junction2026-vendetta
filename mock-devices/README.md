# Mock boxes

A separate process that pretends to be a fleet of Night-Noise Boxes. Each mock
box sends the same v1 analysis payload as a real box (see
[PROTOCOL.md](../PROTOCOL.md)) to the map server's `POST /api/readings`, on
the same schedule (one 10 s reading every 10 s), so the map can't tell the
difference.

```bash
python3 server/server.py --no-seed --db /tmp/noise_map_mock.sqlite3
python3 mock-devices/mock_fleet.py                       # 36 boxes around Helsinki and Espoo
```

Or the whole capital region, 3,000 boxes on real buildings (see "The city"):

```bash
python3 server/server.py --no-seed --quiet --raw-hours 1 --db /tmp/noise_map_city.sqlite3
python3 mock-devices/mock_fleet.py --city --processes 4 --backfill-hours 6 --quiet
```

Open http://localhost:8080 for the map and http://localhost:8090 for the fleet.

Use a separate `--db` for the server: `server/noise_map.sqlite3` is in git, and
36 boxes add about 300,000 rows a day. 3,000 boxes send ~26 million payloads
a day (tens of GB), hence `--raw-hours 1`: the server keeps only the last hour
of raw payloads, next to its 5-minute and hourly summaries (about 1 GB).

## Options

| Option | Default | |
| --- | --- | --- |
| `--server URL` | `http://localhost:8080` | map server |
| `--city` | | run the 3,000 boxes in `city/devices.json` instead of `--devices` |
| `--processes N` | 1 | split the fleet over N processes; with `--backfill-hours`, N more compute the history alongside |
| `--generate N` | 0 | add N generated boxes, scattered ~1 km around the listed ones |
| `--limit N` | all | only the first N boxes |
| `--shard I/N` | `0/1` | run every N-th box starting at I (see "Scale") |
| `--analysis-seconds`, `--reading-seconds` | 10, 10 | same as `ANALYSIS_SECONDS` / `READING_SECONDS` on the box. `900` and `60` give the map's original 15 one-minute readings per upload |
| `--backfill-hours H` | 0 | also send H hours (max 24) of history, as 15 one-minute readings per payload, so the 24 h chart has something in it |
| `--backfill-only` | | send the history and exit, no live uploads |
| `--senders N` | 8 | parallel upload threads |
| `--status-port` | 8090 | 0 turns the status page off |
| `--seed` | 2026 | same seed, same sound |
| `--quiet` | | don't log each outage and failed upload |
| `--preview ID` | | print 24 hourly LAeq/L90/Lmax values and top sources for one box, send nothing |

## What the numbers are based on

`acoustics.py` simulates the sound at each box and summarises it the way the
real box does (`analysis.py`): 125 ms blocks, LAeq as their energy mean, Lmax
the loudest block, L90 the 10th percentile. So L90 ≤ LAeq ≤ Lmax always holds,
and short loud events raise Lmax far more than LAeq, like on a real box.

- **Sites.** Each box has a profile (`motorway`, `arterial_tram`, `nightlife`,
  `construction`, `harbour_ferry`, `courtyard`, `forest_park`, `school`,
  `airport`, ...) with a day and night background level and a set of sound
  events: cars, buses and trams passing, voices, crowds, music, hammering,
  ferries, aircraft, dogs, sirens.
- **Time.** Event rates follow curves in Helsinki local time: weekday rush
  hours, quiet weekend mornings, bars on Friday and Saturday nights,
  construction only on weekdays 7-18 (lighter on Saturdays), school breaks,
  flights from 06 to 23, ferry departures.
- **Weather.** City-wide wind and rain, the same for every box. Wind adds mic
  noise at exposed sites (shores, parks, the island), rain raises the
  background.
- **Classifier.** `sources` uses the real model's labels (`background`,
  `construction`, `noise`, `traffic`, `voices`). Each 1 s window is scored from
  the share of energy each sound contributes, through a confusion table (a
  tram is mostly `traffic` with some `noise` and `construction`, music is
  mostly `noise` and `voices`), with some noise added. 10 windows are averaged per
  reading, like the box does.
- **Imperfections.** Every box has a small mic sensitivity error (±1 dB),
  some more. Rare mic dropouts (`mic_dropout`, `valid_s` < `duration_s`),
  clipping above the calibration offset (`clipping`), and one box without a
  model (`no_model`, empty `sources`).
- **Outages.** Wi-Fi outages (about one every two days per box, more for
  some) work like on a real box: the box keeps measuring, queues up to 500
  payloads and resends them in order. The uploader's 1-60 s backoff applies
  when the server is down too. Power outages (rare) lose the data.

Rough levels, in dB (Z-weighted like the box, so a few dB above typical dB(A)
figures):

| Site | Tue 03:00 | Tue 08:00 | Sat 00:30 |
| --- | --- | --- | --- |
| motorway | 54 | 72 | 63 |
| arterial with trams | 51 | 70 | 56 |
| nightlife street | 43 | 64 | 65 (Friday 21:00: ~71) |
| residential street | 36 | 54 | 40 |
| courtyard | 39 | 47 | 39 |
| forest park | 38 | 50 | 36 |

## The city

`--city` runs 3,000 boxes on real buildings in Helsinki, Espoo, Vantaa and
Kauniainen, built from OpenStreetMap. Instead of a site profile, each box
hears the actual roads, rail lines, bars, schools, building sites, venues,
ferries and flight paths around it, and boxes near each other hear the same
cars, trams and planes.

- **Placement.** 90 % of the boxes are on homes (apartment blocks weighted by
  floors, so the inner city gets more), 10 % on hotspots: next to motorways
  and main roads or within 50 m of a bar. Each box is on a facade, on a random
  floor, facing the street or the yard (the building itself then screens the
  street by about 12 dB).
- **Roads.** Every road segment within earshot, by class: peak flow per
  direction, speed limit (OSM `maxspeed`), share of lorries and buses (for
  example a primary road: 800 vehicles/h, 5 % heavy, 18 buses/h). Vehicles are
  generated per segment and direction and heard one by one as they pass:
  distance, how much of the road the box sees, screening by buildings in
  between, ground absorption, studded tyres from November to March. Traffic
  further away (300 m to 1.5 km) becomes the background level.
- **Rail.** Trams, metro and trains from the route relations, on their real
  track, silent in tunnels, with timetable headways and delays.
- **Points.** Bars and nightclubs (crowds, closing time, music), schools and
  kindergartens (breaks, drop-off), building sites (weekday hours, not every
  site active), stadiums and arenas (event nights with crowds arriving and
  leaving), ferries (manoeuvring, horns, cars driving off).
- **Flights.** Helsinki-Vantaa's runways: arrivals and departures along the
  extended centrelines, two runway ends active at a time.
- **The box itself.** Neighbours, children in the yard, dogs, parking cars,
  the occasional refuse lorry, ventilation in dense blocks, and the same mic
  errors, dropouts and outages as the 36-box fleet. Also city-wide: about 2 %
  of hours one district (0.6-1.8 km across) loses power or Wi-Fi for 20-90
  minutes.

Every sound comes from a seeded stream per emitter and time slot, so shards
and restarts hear the same city.

### Rebuilding the city

`city/devices.json` and `city/emitters.json` are committed; rebuild only to
change the placement or the OSM data. Download the Finland extract once,
cut the capital region and convert it ([osmium](https://osmcode.org/osmium-tool/),
`brew install osmium-tool`, and the `osmium` Python package):

```bash
curl -LO https://download.geofabrik.de/europe/finland-latest.osm.pbf          # ~780 MB
osmium extract -b 24.40,60.05,25.37,60.42 -s complete_ways -o capital.osm.pbf finland-latest.osm.pbf
python3 -m venv /tmp/osm_venv && /tmp/osm_venv/bin/pip install osmium
/tmp/osm_venv/bin/python mock-devices/city/osm_from_pbf.py capital.osm.pbf     # ~1 min
python3 mock-devices/city/build_city.py                                       # ~20 s
```

`build_city.py --count N --hotspots 0.1 --seed S` places a different number of
boxes. `city/osm_fetch.py` gets the same data from the Overpass API instead,
but the public servers are often too busy for the building queries. Map data
(c) OpenStreetMap contributors, ODbL.

## Adding boxes

Edit `devices.json`. Each box needs `id`, `lat`, `lon` and `profile` (a key of
`PROFILES` in `acoustics.py`). Optional: `level_bias_db`, `cal_error_db`,
`calibration_offset_db`, `no_model`, `wifi_outages_per_day`,
`power_outages_per_day`. Or use `--generate N` for any number of generated
ones; their mix of profiles is `GENERATED_MIX` in `mock_fleet.py`. New site
types go in `PROFILES`, new kinds of sound in `CONFUSION`.

## Scale

A process falling behind says so in its 30 s log line (`behind by`). Each
shard gets a different set of boxes and the same weather and city;
`--processes N` starts N shards and serves their combined status on
`--status-port`.

Measured on an 8 GB laptop with 3,000 city boxes, `--processes 4`:

- Live: 300 uploads/s, each shard ~16 % of a core and ~100 MB, no lag. The
  map server uses ~4 % of a core; `GET /api/readings` returns 3,000 stations
  in ~130 kB (gzipped) in ~70 ms. With 6 h of history: ~1.7 MB, ~1.7 s.
- Backfill: about 0.07 CPU-seconds per box-hour. With `--processes`, 4 extra
  processes compute it alongside the live shards, uploading as they go, and
  exit when done, so the map is live straight away and the history fills in
  from the oldest hour. 6 h took 12.5 minutes, with the laptop short of
  memory. Meanwhile the map server couldn't keep up with live and history
  together: live uploads queued (no data lost, the boxes buffer) and caught up
  within a minute after the backfill. On a smaller machine use fewer
  `--backfill-hours`.
- The map server summarises (see [PROTOCOL.md](../PROTOCOL.md)): one row per
  station per hour for older data, per 5 minutes for the last hour or so.
  `--raw-hours` limits how long the raw payloads are kept.

## Status and control

`http://localhost:8090` shows every box: state, latest LAeq/L90/Lmax, top
source, flags, and how many uploads were sent, are queued or failed.

```
GET  /api/status                               totals
GET  /api/devices                              every box
POST /api/devices/<id|all>/outage?kind=wifi&minutes=10    kind=power loses data instead
POST /api/devices/<id|all>/restore
```

For example, to take the whole fleet offline for 5 minutes and watch the
backlog arrive afterwards:

```bash
curl -X POST 'localhost:8090/api/devices/all/outage?kind=wifi&minutes=5'
```
