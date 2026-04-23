"""
luwes_db.py — PostgreSQL version
Menggantikan implementasi SQLite sebelumnya.

API publik identik dengan versi SQLite sehingga luwes_service.py,
luwes_scheduler.py, dan luwes_routes.py tidak perlu diubah banyak.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

from .pg_db import get_cursor, execute_one, execute_all, execute_returning

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))


# ── No-op: tabel dibuat via SQL migration ────────────────────
def init_db(db_path: str = None):
    """No-op di PostgreSQL mode. Tabel sudah dibuat via migration SQL."""
    logger.info("[luwes_db] PostgreSQL mode — tabel sudah ada via migration SQL")


# ══════════════════════════════════════════════════════════════
# WRITE OPERATIONS
# ══════════════════════════════════════════════════════════════

def insert_observation(db_path: str, obs: Dict) -> bool:
    """
    Simpan satu observasi.
    Return True jika inserted baru, False jika duplikat (rec sudah ada).

    obs keys: rec, station_id, station_name, imei, level_m, recorded_at, fetched_at
    """
    try:
        # Parse recorded_at — bisa string ISO8601 atau sudah TIMESTAMPTZ
        recorded_at = obs.get("recorded_at")
        fetched_at  = obs.get("fetched_at")

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
                recorded_at,
                fetched_at,
            )
        )
        return True
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return False   # duplikat rec
        logger.error(f"[luwes_db] insert_observation error: {e}")
        raise


def insert_fetch_log(db_path: str, log: Dict):
    """Simpan log satu fetch operation."""
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
            )
        )
    except Exception as e:
        logger.warning(f"[luwes_db] insert_fetch_log failed (non-critical): {e}")


# ══════════════════════════════════════════════════════════════
# READ OPERATIONS
# ══════════════════════════════════════════════════════════════

def get_latest(db_path: str, imei: str) -> Optional[Dict]:
    """Ambil observasi terbaru untuk imei ini."""
    row = execute_one(
        """
        SELECT rec, station_id, station_name, imei,
               level_m,
               recorded_at AT TIME ZONE 'UTC' AS recorded_at,
               fetched_at  AT TIME ZONE 'UTC' AS fetched_at
        FROM water_level_observations
        WHERE imei = %s
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (imei,)
    )
    if not row:
        return None
    return _format_obs_row(dict(row))


def get_by_date_range(
    db_path: str,
    imei: str,
    start: str,
    end: str,
    limit: int = 10000
) -> List[Dict]:
    """
    Ambil observasi dalam rentang waktu.
    start, end: ISO8601 string, e.g. '2024-01-01T00:00:00+07:00'
    """
    rows = execute_all(
        """
        SELECT rec, station_id, station_name, imei,
               level_m,
               recorded_at,
               fetched_at
        FROM water_level_observations
        WHERE imei = %s
          AND recorded_at >= %s::TIMESTAMPTZ
          AND recorded_at <= %s::TIMESTAMPTZ
        ORDER BY recorded_at ASC
        LIMIT %s
        """,
        (imei, start, end, limit)
    )
    return [_format_obs_row(dict(r)) for r in rows]


def get_today(db_path: str, imei: str) -> List[Dict]:
    """Ambil semua observasi hari ini (WIB)."""
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    start = f"{today}T00:00:00+07:00"
    end   = f"{today}T23:59:59+07:00"
    return get_by_date_range(db_path, imei, start, end)


def get_stats(db_path: str, imei: str) -> Dict:
    """Statistik keseluruhan data untuk imei ini."""
    row = execute_one(
        """
        SELECT
            COUNT(*)                                         AS total_records,
            MIN(recorded_at AT TIME ZONE 'Asia/Jakarta')     AS oldest_record,
            MAX(recorded_at AT TIME ZONE 'Asia/Jakarta')     AS newest_record
        FROM water_level_observations
        WHERE imei = %s
        """,
        (imei,)
    )
    if not row or not row["total_records"]:
        return {"total_records": 0, "oldest_record": None, "newest_record": None, "date_range_days": 0}

    oldest = row["oldest_record"]
    newest = row["newest_record"]
    days = 0
    if oldest and newest:
        if hasattr(oldest, 'days'):
            days = 0
        else:
            try:
                days = (newest - oldest).days
            except Exception:
                pass

    return {
        "total_records": row["total_records"],
        "oldest_record": oldest.isoformat() if hasattr(oldest, 'isoformat') else str(oldest),
        "newest_record": newest.isoformat() if hasattr(newest, 'isoformat') else str(newest),
        "date_range_days": days,
    }


def get_recent_fetch_logs(db_path: str, imei: str, limit: int = 20) -> List[Dict]:
    """Ambil log fetch terbaru untuk monitoring."""
    rows = execute_all(
        """
        SELECT fetched_at, status, rec, level_m, recorded_at, message
        FROM fetch_log
        WHERE imei = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (imei, limit)
    )
    result = []
    for r in rows:
        d = dict(r)
        if d.get("fetched_at") and hasattr(d["fetched_at"], "isoformat"):
            d["fetched_at"] = d["fetched_at"].isoformat()
        if d.get("recorded_at") and hasattr(d["recorded_at"], "isoformat"):
            d["recorded_at"] = d["recorded_at"].isoformat()
        result.append(d)
    return result


def rec_exists(db_path: str, rec: int) -> bool:
    """Cek apakah rec tertentu sudah ada di database."""
    row = execute_one(
        "SELECT 1 FROM water_level_observations WHERE rec = %s LIMIT 1",
        (rec,)
    )
    return row is not None


def get_latest_rec(db_path: str, imei: str) -> Optional[int]:
    """Ambil nilai rec terbesar untuk imei ini."""
    row = execute_one(
        "SELECT MAX(rec) AS max_rec FROM water_level_observations WHERE imei = %s",
        (imei,)
    )
    return row["max_rec"] if row and row["max_rec"] is not None else None


# ── Helper ────────────────────────────────────────────────────

def _format_obs_row(row: Dict) -> Dict:
    """Konversi datetime objects ke ISO8601 string untuk kompatibilitas."""
    for key in ("recorded_at", "fetched_at"):
        val = row.get(key)
        if val is not None and hasattr(val, "isoformat"):
            # Simpan dalam format +07:00 untuk kompatibilitas dengan luwes_service
            if hasattr(val, "tzinfo") and val.tzinfo is not None:
                row[key] = val.astimezone(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
            else:
                row[key] = val.isoformat()
    return row