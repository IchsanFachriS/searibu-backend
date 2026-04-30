"""Luwes water-level observation database operations (PostgreSQL).

Tables managed:
    water_level_observations — tidal telemetry records from Luwes stations
    fetch_log                — scheduler fetch audit log

Schema is created via migrations/001_initial_schema.sql.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

from .pg_db import execute_one, execute_all, execute_returning

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))


def init_db(db_path: str = None) -> None:
    """No-op — kept for backward-compatibility with app.py startup sequence."""
    logger.debug("luwes_db: PostgreSQL mode — schema managed via migration SQL")


def insert_observation(db_path: str, obs: Dict) -> bool:
    """Insert a single observation record.

    Returns True if the row was inserted, False if it was a duplicate (same rec).

    Args:
        db_path: ignored in PostgreSQL mode.
        obs: dict with keys rec, station_id, station_name, imei,
             level_m, recorded_at, fetched_at.
    """
    try:
        execute_returning(
            """
            INSERT INTO water_level_observations
                (rec, station_id, station_name, imei, level_m, recorded_at, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                obs.get("rec"),
                obs.get("station_id"),
                obs.get("station_name"),
                obs.get("imei"),
                obs.get("level_m"),
                obs.get("recorded_at"),
                obs.get("fetched_at"),
            ),
        )
        return True
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return False
        logger.error("insert_observation error: %s", exc)
        raise


def insert_fetch_log(db_path: str, log: Dict) -> None:
    """Insert a fetch audit log entry (non-critical; errors are swallowed)."""
    try:
        execute_returning(
            """
            INSERT INTO fetch_log
                (fetched_at, imei, status, rec, level_m, recorded_at, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                log.get("fetched_at"),
                log.get("imei"),
                log.get("status"),
                log.get("rec"),
                log.get("level_m"),
                log.get("recorded_at"),
                log.get("message"),
            ),
        )
    except Exception as exc:
        logger.warning("insert_fetch_log failed (non-critical): %s", exc)


def get_latest(db_path: str, imei: str) -> Optional[Dict]:
    """Return the most recent observation for the given IMEI, or None."""
    row = execute_one(
        """
        SELECT rec, station_id, station_name, imei, level_m,
               recorded_at AT TIME ZONE 'UTC' AS recorded_at,
               fetched_at  AT TIME ZONE 'UTC' AS fetched_at
        FROM water_level_observations
        WHERE imei = %s
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (imei,),
    )
    return _fmt(dict(row)) if row else None


def get_by_date_range(
    db_path: str,
    imei: str,
    start: str,
    end: str,
    limit: int = 10_000,
) -> List[Dict]:
    """Return observations within a time range, ordered ascending.

    Args:
        start, end: ISO 8601 strings, e.g. '2024-01-01T00:00:00+07:00'.
        limit: maximum number of rows to return.
    """
    rows = execute_all(
        """
        SELECT rec, station_id, station_name, imei, level_m, recorded_at, fetched_at
        FROM water_level_observations
        WHERE imei = %s
          AND recorded_at >= %s::TIMESTAMPTZ
          AND recorded_at <= %s::TIMESTAMPTZ
        ORDER BY recorded_at ASC
        LIMIT %s
        """,
        (imei, start, end, limit),
    )
    return [_fmt(dict(r)) for r in rows]


def get_stats(db_path: str, imei: str) -> Dict:
    """Return aggregate statistics for the given IMEI."""
    row = execute_one(
        """
        SELECT
            COUNT(*)                                     AS total_records,
            MIN(recorded_at AT TIME ZONE 'Asia/Jakarta') AS oldest_record,
            MAX(recorded_at AT TIME ZONE 'Asia/Jakarta') AS newest_record
        FROM water_level_observations
        WHERE imei = %s
        """,
        (imei,),
    )
    if not row or not row["total_records"]:
        return {"total_records": 0, "oldest_record": None, "newest_record": None, "date_range_days": 0}

    oldest = row["oldest_record"]
    newest = row["newest_record"]
    days = (newest - oldest).days if oldest and newest else 0

    return {
        "total_records": row["total_records"],
        "oldest_record": oldest.isoformat() if hasattr(oldest, "isoformat") else str(oldest),
        "newest_record": newest.isoformat() if hasattr(newest, "isoformat") else str(newest),
        "date_range_days": days,
    }


def get_recent_fetch_logs(db_path: str, imei: str, limit: int = 20) -> List[Dict]:
    """Return the most recent fetch log entries for monitoring."""
    rows = execute_all(
        """
        SELECT fetched_at, status, rec, level_m, recorded_at, message
        FROM fetch_log
        WHERE imei = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (imei, limit),
    )
    result = []
    for r in rows:
        d = dict(r)
        for key in ("fetched_at", "recorded_at"):
            if d.get(key) and hasattr(d[key], "isoformat"):
                d[key] = d[key].isoformat()
        result.append(d)
    return result


def rec_exists(db_path: str, rec: int) -> bool:
    """Return True if the given rec value already exists."""
    row = execute_one(
        "SELECT 1 FROM water_level_observations WHERE rec = %s LIMIT 1",
        (rec,),
    )
    return row is not None


def get_latest_rec(db_path: str, imei: str) -> Optional[int]:
    """Return the highest rec value for the given IMEI, or None."""
    row = execute_one(
        "SELECT MAX(rec) AS max_rec FROM water_level_observations WHERE imei = %s",
        (imei,),
    )
    return row["max_rec"] if row and row["max_rec"] is not None else None


def _fmt(row: Dict) -> Dict:
    """Serialise datetime fields to WIB ISO 8601 strings."""
    for key in ("recorded_at", "fetched_at"):
        val = row.get(key)
        if val is not None and hasattr(val, "isoformat"):
            if hasattr(val, "tzinfo") and val.tzinfo is not None:
                row[key] = val.astimezone(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
            else:
                row[key] = val.isoformat()
    return row