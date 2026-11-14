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

Open http://localhost:8080 for the map and http://localhost:8090 for the fleet.

Use a separate `--db` for the server: `server/noise_map.sqlite3` is in git, and
36 boxes add about 300,000 rows a day.

## Options

| Option | Default | |
| --- | --- | --- |
| `--server URL` | `http://localhost:8080` | map server |
| `--generate N` | 0 | add N generated boxes, scattered ~1 km around the listed ones |
| `--limit N` | all | only the first N boxes |
| `--shard I/N` | `0/1` | run every N-th box starting at I (see "Scale") |
| `--analysis-seconds`, `--reading-seconds` | 10, 10 | same as `ANALYSIS_SECONDS` / `READING_SECONDS` on the box. `900` and `60` give the map's original 15 one-minute readings per upload |
| `--backfill-hours H` | 0 | first send H hours (max 24) of history, as 15 one-minute readings per payload, so the 24 h chart has something in it |
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

## Adding boxes

Edit `devices.json`. Each box needs `id`, `lat`, `lon` and `profile` (a key of
`PROFILES` in `acoustics.py`). Optional: `level_bias_db`, `cal_error_db`,
`calibration_offset_db`, `no_model`, `wifi_outages_per_day`,
`power_outages_per_day`. Or use `--generate N` for any number of generated
ones; their mix of profiles is `GENERATED_MIX` in `mock_fleet.py`. New site
types go in `PROFILES`, new kinds of sound in `CONFUSION`.

## Scale

One process runs about 1,000 boxes at 10 s uploads (~100 uploads/s) on ~20 %
of one CPU core. A process falling behind says so in its 30 s log line. Above
a few thousand boxes, split the fleet across processes. Each shard gets a
different set of boxes, the same weather, and needs its own status port:

```bash
for i in 0 1 2 3; do
  python3 mock-devices/mock_fleet.py --generate 10000 --shard $i/4 --status-port $((8090 + i)) --quiet &
done
```

What the map server did in testing (36 and 1,036 boxes):

- `GET /api/readings` returns at most 5,000 payloads. With 1,036 boxes that is
  under a minute of data, so the map's 15-minute windows and 24 h chart only
  cover that last minute. The server now returns the newest 5,000 (it used to
  return the oldest, which left the map with stale data after ~23 minutes of
  36 boxes). Big fleets need aggregation on the server side.
- `ThreadingHTTPServer` accepts 5 waiting connections; bursts of uploads
  sometimes get `Connection reset`. The mock boxes retry, like real ones.
- Every upload is a separate SQLite commit.

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
