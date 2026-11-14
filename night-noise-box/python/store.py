#!/usr/bin/env python3
"""SQLite storage for events mode: event records, heatmap data and CSV export.

Only the numbers in CSV_COLUMNS are stored. Events older than RETENTION_DAYS
are deleted on every insert.

    python3 python/store.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from config import GRID_CELL, RETENTION_DAYS  # noqa: E402

DEFAULT_DB_PATH = THIS_DIR / "night_noise.sqlite3"
DEFAULT_RETENTION_DAYS: int = RETENTION_DAYS

NIGHT_HOURS_DEFAULT: List[int] = [22, 23, 0, 1, 2, 3, 4, 5, 6]
CANONICAL_LABELS: List[str] = [
    "traffic",
    "construction",
    "music",
    "voices",
    "background",
]

CSV_COLUMNS: Tuple[str, ...] = (
    "id",
    "start_time",
    "duration_ms",
    "label",
    "confidence",
    "avg_level",
    "peak_level",
    "woke_me",
    "tamper",
    "grid_cell",
)

# Made-up neighbouring cells so the area heatmap isn't a single square.
SIMULATED_AREA_CELLS: List[Dict[str, Any]] = [
    {"cell": "A1", "count": 3, "avg_level": 51.4, "simulated": True},
    {"cell": "A3", "count": 6, "avg_level": 59.8, "simulated": True},
    {"cell": "C2", "count": 4, "avg_level": 55.2, "simulated": True},
    {"cell": "C4", "count": 2, "avg_level": 48.6, "simulated": True},
]


class NoiseStore:
    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        grid_cell: str = GRID_CELL,
        simulated_area_cells: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.db_path = str(db_path)
        self.retention_days = int(retention_days)
        self.grid_cell = str(grid_cell)
        self.simulated_area_cells: List[Dict[str, Any]] = [
            dict(item, simulated=True)
            for item in (
                simulated_area_cells
                if simulated_area_cells is not None
                else SIMULATED_AREA_CELLS
            )
            if item.get("cell") != self.grid_cell
        ]
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time  TEXT    NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    label       TEXT    NOT NULL,
                    confidence  REAL    NOT NULL,
                    avg_level   REAL    NOT NULL,
                    peak_level  REAL    NOT NULL,
                    woke_me     INTEGER NOT NULL DEFAULT 0,
                    tamper      INTEGER NOT NULL DEFAULT 0,
                    grid_cell   TEXT    NOT NULL,
                    end_epoch   REAL    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_start_time ON events(start_time)"
            )
            self._conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "start_time": str(row["start_time"]),
            "duration_ms": int(row["duration_ms"]),
            "label": str(row["label"]),
            "confidence": round(float(row["confidence"]), 3),
            "avg_level": round(float(row["avg_level"]), 1),
            "peak_level": round(float(row["peak_level"]), 1),
            "woke_me": bool(row["woke_me"]),
            "tamper": bool(row["tamper"]),
            "grid_cell": str(row["grid_cell"]),
        }

    def prune_old_events(self, now_dt: Optional[datetime] = None) -> int:
        ref_dt = now_dt if now_dt is not None else datetime.now()
        cutoff_iso = (ref_dt - timedelta(days=self.retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE start_time < ?",
                (cutoff_iso,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def insert_event(
        self,
        *,
        start_time: str,
        duration_ms: int,
        label: str,
        confidence: float,
        avg_level: float,
        peak_level: float,
        woke_me: bool,
        tamper: bool,
        grid_cell: Optional[str] = None,
        end_epoch: Optional[float] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        cell = str(grid_cell if grid_cell is not None else self.grid_cell)
        done_epoch = float(end_epoch if end_epoch is not None else time.time())

        with self._lock:
            self.prune_old_events(now_dt=now_dt)
            cur = self._conn.execute(
                """
                INSERT INTO events (
                    start_time, duration_ms, label, confidence,
                    avg_level, peak_level, woke_me, tamper, grid_cell, end_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(start_time),
                    max(0, int(duration_ms)),
                    str(label),
                    round(float(confidence), 3),
                    round(float(avg_level), 1),
                    round(float(peak_level), 1),
                    1 if woke_me else 0,
                    1 if tamper else 0,
                    cell,
                    done_epoch,
                ),
            )
            row_id = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (row_id,)).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def mark_latest_recent_woke_me(
        self,
        now_epoch: Optional[float] = None,
        max_age_sec: float = 600.0,
    ) -> Optional[Dict[str, Any]]:
        """Sets woke_me on the latest event if it ended less than max_age_sec ago.
        Returns the updated record, or None."""
        ref_epoch = float(now_epoch if now_epoch is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            age_sec = ref_epoch - float(row["end_epoch"])
            if age_sec < 0.0 or age_sec >= max_age_sec:
                return None
            event_id = int(row["id"])
            self._conn.execute(
                "UPDATE events SET woke_me = 1 WHERE id = ?",
                (event_id,),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._row_to_record(updated)

    def list_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def count_events(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM events").fetchone()
            return int(row["cnt"]) if row is not None else 0

    def db_size_bytes(self) -> int:
        with self._lock:
            if self.db_path != ":memory:" and os.path.exists(self.db_path):
                try:
                    return int(os.path.getsize(self.db_path))
                except OSError:
                    pass
            pc = self._conn.execute("PRAGMA page_count").fetchone()
            ps = self._conn.execute("PRAGMA page_size").fetchone()
            pages = int(pc[0]) if pc is not None else 0
            size = int(ps[0]) if ps is not None else 4096
            return pages * size

    def export_csv_text(self) -> str:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, start_time, duration_ms, label, confidence,
                       avg_level, peak_level, woke_me, tamper, grid_cell
                FROM events
                ORDER BY id ASC
                """
            ).fetchall()

        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for r in rows:
            rec = self._row_to_record(r)
            writer.writerow(
                [
                    rec["id"],
                    rec["start_time"],
                    rec["duration_ms"],
                    rec["label"],
                    f"{rec['confidence']:.3f}",
                    f"{rec['avg_level']:.1f}",
                    f"{rec['peak_level']:.1f}",
                    "true" if rec["woke_me"] else "false",
                    "true" if rec["tamper"] else "false",
                    rec["grid_cell"],
                ]
            )
        return buf.getvalue()

    def build_heatmap_data(self) -> Dict[str, Any]:
        """Data for the local page:
          night:          [{hour, label, count, avg_level, woke_count}]
          area:           [{cell, count, avg_level, simulated}]
          recent_events:  the 20 latest events
        """
        with self._lock:
            night_rows = self._conn.execute(
                """
                SELECT
                    CAST(SUBSTR(start_time, 12, 2) AS INTEGER) AS hour,
                    label,
                    COUNT(*)                                   AS count,
                    ROUND(AVG(avg_level), 1)                   AS avg_level,
                    SUM(woke_me)                               AS woke_count
                FROM events
                GROUP BY hour, label
                ORDER BY hour ASC, label ASC
                """
            ).fetchall()

            night_buckets: List[Dict[str, Any]] = []
            seen_hours = list(NIGHT_HOURS_DEFAULT)
            has_unknown = False
            for r in night_rows:
                hr = int(r["hour"])
                lbl = str(r["label"])
                if hr not in seen_hours:
                    seen_hours.append(hr)
                if lbl == "unknown":
                    has_unknown = True
                night_buckets.append(
                    {
                        "hour": hr,
                        "label": lbl,
                        "count": int(r["count"]),
                        "avg_level": round(float(r["avg_level"]), 1),
                        "woke_count": int(r["woke_count"] or 0),
                    }
                )

            box_row = self._conn.execute(
                """
                SELECT
                    COUNT(*)                 AS count,
                    ROUND(AVG(avg_level), 1) AS avg_level
                FROM events
                WHERE grid_cell = ?
                """,
                (self.grid_cell,),
            ).fetchone()

            real_count = int(box_row["count"] or 0) if box_row is not None else 0
            real_avg = (
                round(float(box_row["avg_level"]), 1)
                if (box_row is not None and box_row["avg_level"] is not None)
                else 0.0
            )

            area_buckets: List[Dict[str, Any]] = [
                {
                    "cell": self.grid_cell,
                    "count": real_count,
                    "avg_level": real_avg,
                    "simulated": False,
                }
            ]
            for sim_cell in self.simulated_area_cells:
                area_buckets.append(
                    {
                        "cell": str(sim_cell["cell"]),
                        "count": int(sim_cell["count"]),
                        "avg_level": round(float(sim_cell["avg_level"]), 1),
                        "simulated": True,
                    }
                )

            labels_axis = list(CANONICAL_LABELS) + (["unknown"] if has_unknown else [])
            return {
                "hours": seen_hours,
                "labels": labels_axis,
                "box_cell": self.grid_cell,
                "night": night_buckets,
                "area": area_buckets,
                "recent_events": self.list_recent_events(limit=20),
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def run_self_test() -> None:
    store = NoiseStore(db_path=":memory:", retention_days=7, grid_cell="B2")
    ref_now = datetime(2026, 11, 14, 2, 30, 0)
    ref_epoch = ref_now.timestamp()

    # An event older than the retention period is pruned on the next insert.
    store.insert_event(
        start_time="2026-11-01T23:15:00",
        duration_ms=2400,
        label="traffic",
        confidence=0.85,
        avg_level=60.0,
        peak_level=64.0,
        woke_me=False,
        tamper=False,
        end_epoch=ref_epoch - 10 * 86400,
        now_dt=ref_now,
    )
    fresh = store.insert_event(
        start_time="2026-11-14T02:25:00",
        duration_ms=3200,
        label="traffic",
        confidence=0.90,
        avg_level=62.3,
        peak_level=67.1,
        woke_me=False,
        tamper=False,
        end_epoch=ref_epoch - 180.0,
        now_dt=ref_now,
    )
    assert store.count_events() == 1
    assert store.db_size_bytes() > 0
    records = store.list_recent_events()
    assert len(records) == 1 and records[0]["id"] == fresh["id"], "Old record (>7d) must be pruned"

    # "Woke me" attaches to an event that ended under 10 minutes ago, but not later.
    updated = store.mark_latest_recent_woke_me(now_epoch=ref_epoch, max_age_sec=600.0)
    assert updated is not None and updated["id"] == fresh["id"] and updated["woke_me"] is True

    expired_lookup = store.mark_latest_recent_woke_me(now_epoch=ref_epoch + 700.0, max_age_sec=600.0)
    assert expired_lookup is None

    hm = store.build_heatmap_data()
    assert hm["night"] == [
        {"hour": 2, "label": "traffic", "count": 1, "avg_level": 62.3, "woke_count": 1}
    ]
    real_cells = [c for c in hm["area"] if not c["simulated"]]
    sim_cells = [c for c in hm["area"] if c["simulated"]]
    assert len(real_cells) == 1 and real_cells[0] == {
        "cell": "B2",
        "count": 1,
        "avg_level": 62.3,
        "simulated": False,
    }
    assert len(sim_cells) >= 2

    csv_text = store.export_csv_text()
    parsed = list(csv.reader(io.StringIO(csv_text)))
    assert tuple(parsed[0]) == CSV_COLUMNS
    assert len(parsed) == 2
    assert parsed[1][3] == "traffic" and parsed[1][7] == "true" and parsed[1][9] == "B2"

    store.close()
    print("store self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Night-Noise Box SQLite Store")
    parser.add_argument("--self-test", action="store_true", help="Run SQLite store self-tests")
    args = parser.parse_args()
    run_self_test()


if __name__ == "__main__":
    main()
