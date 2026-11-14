# Messages and data formats

Reference for what the parts of the system send each other. If you change a
message, update both ends and this file.

## Box → map server

If the server has `NOISE_MAP_KEY` set, every POST must send
`Authorization: Bearer <key>`, otherwise it gets a 401. GET requests are public.

### Analysis payload (analysis mode)

`POST /api/readings`, one per analysis. The server stores it as-is and keeps
per-station summaries (5-minute and hourly windows). The map reads the
summaries back from `GET /api/readings?since=<ISO time>`, in the same format
with one reading per window (`duration_s` is the window's total, plus `n`, the
number of readings in it). `&raw=1` returns the stored payloads instead
(newest 5000). Re-sending a payload with the same first `minute` replaces it,
in the summaries too.

```json
{
  "v": 1,
  "station_id": "box-01",
  "lat": 60.1868,
  "lon": 24.8274,
  "model": "edge-impulse:night-noise#2",
  "weighting": "Z",
  "calibration_offset_db": 94.0,
  "analysis_seconds": 10.0,
  "readings": [
    {
      "minute": "2026-11-14T22:10:00Z",
      "duration_s": 10.0,
      "laeq_db": 48.3,
      "lmax_db": 61.0,
      "l90_db": 39.5,
      "sources": { "traffic": 0.7, "voices": 0.3 },
      "valid_s": 10.0,
      "inferences": 10,
      "flags": []
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `minute` | Start of the reading, UTC. Named after the map's original one-minute readings. |
| `laeq_db` | Energy-average level. Not A-weighted yet (`"weighting": "Z"`). |
| `lmax_db` | Loudest 125 ms block. |
| `l90_db` | Level exceeded 90 % of the time, i.e. the background. |
| `sources` | Classifier scores averaged over the reading, summing to 1. Empty without a model. |
| `valid_s` | Seconds of audio actually captured. Less than `duration_s` if the mic dropped out. |
| `flags` | Any of `no_model`, `clipping`, `mic_dropout`. |

### Event records (events mode)

`POST /api/events` with `{"device_id", "lat", "lon", "events": [record, ...]}`.
The server keys events on `(device_id, id)`, so re-sending one updates it.

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | int | Local SQLite id. |
| `start_time` | str | Local time the event started, `YYYY-MM-DDTHH:MM:SS`. |
| `duration_ms` | int | From the STM32's `event_end`. 0 for a standalone "woke me". |
| `label` | str | `traffic`, `construction`, `music`, `voices`, `background`, or `unknown` for a standalone "woke me". |
| `confidence` | float | Average confidence of that label while it was active. |
| `avg_level`, `peak_level` | float | dB over the event. |
| `woke_me` | bool | A button was pressed during the event or up to 10 minutes after it. |
| `tamper` | bool | The box moved during the event or up to 60 s before it started. |
| `grid_cell` | str | `GRID_CELL` from config.py. |

The same columns, in this order, are what the local page's CSV export contains.

## Linux ↔ STM32 (Bridge)

Linux → STM32:

| Message | Args | When |
| --- | --- | --- |
| `level` | `db` (float) | Every 125 ms. If it stops for 2 s, the STM32 closes any open event and shows "no signal". |
| `label` | `name`, `confidence` | When the label changes (events mode). |
| `set_thresholds` | `start_db`, `end_db`, `start_hold_ms`, `end_hold_ms` | At startup and when the profile changes (events mode). |

STM32 → Linux:

| Message | Args | When |
| --- | --- | --- |
| `event_start` | | Level above `start_db` for `start_hold_ms`. |
| `event_end` | `duration_ms` | Level below `end_db` for `end_hold_ms`, or the 2 s no-signal timeout. |
| `woke_me` | | A button press (debounced 200 ms). |
| `tamper` | | The box moved (debounced 1 s). |
| `heartbeat` | | Once a second. |
| `knob` | `value` | The knob turned. Ignored. |

In analysis mode Linux only sends `level`, to drive the LED bar, and ignores
everything the STM32 sends.

## Linux ↔ local page (events mode)

Over the WebUI brick's Socket.IO on the board, or SSE (`/api/events`) plus
`POST /api/cmd` when `main.py` runs on a laptop.

Linux → page:

| Event | Payload |
| --- | --- |
| `live` | `{level, raw_db, label}`, every 125 ms |
| `event` | an event record, when one is stored or updated |
| `heatmap` | `{night, area, hours, labels, box_cell, recent_events}` |
| `diagnostics` | profile, calibration, STM32 heartbeat status, database size, board clock; once a second |
| `csv_export` | `{filename, csv}` |

`heatmap.night` is a list of `{hour, label, count, avg_level, woke_count}`.
`heatmap.area` is a list of `{cell, count, avg_level, simulated}`; only the
box's own cell is real, the rest are made up and marked `simulated`.

Page → Linux:

| Event | Payload | Effect |
| --- | --- | --- |
| `request_state` | `{}` | Linux sends `live`, `heatmap` and `diagnostics`. |
| `set_profile` | `{profile}` | Switches profile and sends `set_thresholds`. |
| `calibrate` | `{phone_db}` | Offset = `phone_db` − mean raw dBFS over the last 3 s. Saved to `<db>.calibration.json`. |
| `export_csv` | `{}` | Linux replies with `csv_export`. |
