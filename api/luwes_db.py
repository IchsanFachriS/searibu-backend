"""
Luwes DB — SQLite persistence untuk observasi water level.

Satu database utama:
  luwes_raw.db → semua data historis sejak stasiun aktif (tidak pernah dihapus)

Skema tabel water_level_observations:
  id           INTEGER PK AUTOINCREMENT
  rec          INTEGER UNIQUE   ← ID unik dari Luwes API, cegah duplikat
  station_id   INTEGER
  station_name TEXT
  imei         TEXT
  level_m      REAL
  recorded_at  TEXT             ← ISO8601 UTC (dari API) → dikonversi WIB +07:00
  fetched_at   TEXT             ← ISO8601 WIB +07:00

Index:
  idx_obs_recorded_at  → query range waktu cepat
  idx_obs_imei         → filter per stasiun
  idx_obs_rec          → lookup duplikat cepat
"""

import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

WIB = timezone(timedelta(hours=7))

_lock = threading.Lock()

_DDL_OBSERVATIONS = """
    CREATE TABLE IF NOT EXISTS water_level_observations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        rec          INTEGER UNIQUE,
        station_id   INTEGER,
        station_name TEXT,
        imei         TEXT,
        level_m      REAL,
        recorded_at  TEXT,
        fetched_at   TEXT
    )
"""

_DDL_IDX_RECORDED = """
    CREATE INDEX IF NOT EXISTS idx_obs_recorded_at
    ON water_level_observations(recorded_at)
"""

_DDL_IDX_IMEI = """
    CREATE INDEX IF NOT EXISTS idx_obs_imei
    ON water_level_observations(imei)
"""

_DDL_IDX_REC = """
    CREATE INDEX IF NOT EXISTS idx_obs_rec
    ON water_level_observations(rec)
"""

_DDL_FETCH_LOG = """
    CREATE TABLE IF NOT EXISTS fetch_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at   TEXT,
        imei         TEXT,
        status       TEXT,   -- 'ok' | 'duplicate' | 'error'
        rec          INTEGER,
        level_m      REAL,
        recorded_at  TEXT,
        message      TEXT
    )
"""

_DDL_IDX_FETCH_LOG = """
    CREATE INDEX IF NOT EXISTS idx_fetch_log_imei_time
    ON fetch_log(imei, fetched_at)
"""


# ══════════════════════════════════════════════════════════════
# KONEKSI
# ══════════════════════════════════════════════════════════════

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-4096")  # 4MB cache
    return conn


# ══════════════════════════════════════════════════════════════
# INISIALISASI
# ══════════════════════════════════════════════════════════════

def init_db(db_path: str):
    """Buat semua tabel dan index jika belum ada."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(_DDL_OBSERVATIONS)
            conn.execute(_DDL_IDX_RECORDED)
            conn.execute(_DDL_IDX_IMEI)
            conn.execute(_DDL_IDX_REC)
            conn.execute(_DDL_FETCH_LOG)
            conn.execute(_DDL_IDX_FETCH_LOG)
            conn.commit()
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════
# WRITE OPERATIONS
# ══════════════════════════════════════════════════════════════

def insert_observation(db_path: str, obs: Dict) -> bool:
    """
    Simpan satu observasi.
    Return True jika inserted baru, False jika duplikat (rec sudah ada).
    """
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute("""
                INSERT INTO water_level_observations
                    (rec, station_id, station_name, imei, level_m, recorded_at, fetched_at)
                VALUES
                    (:rec, :station_id, :station_name, :imei, :level_m, :recorded_at, :fetched_at)
            """, obs)
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()


def insert_fetch_log(db_path: str, log: Dict):
    """Simpan log satu fetch operation."""
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute("""
                INSERT INTO fetch_log
                    (fetched_at, imei, status, rec, level_m, recorded_at, message)
                VALUES
                    (:fetched_at, :imei, :status, :rec, :level_m, :recorded_at, :message)
            """, log)
            conn.commit()
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════
# READ OPERATIONS
# ══════════════════════════════════════════════════════════════

def get_latest(db_path: str, imei: str) -> Optional[Dict]:
    """Ambil observasi terbaru untuk imei ini."""
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT rec, station_id, station_name, imei,
                       level_m, recorded_at, fetched_at
                FROM water_level_observations
                WHERE imei = ?
                ORDER BY recorded_at DESC
                LIMIT 1
            """, (imei,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_by_date_range(
    db_path: str,
    imei: str,
    start: str,
    end: str,
    limit: int = 10000
) -> List[Dict]:
    """
    Ambil observasi dalam rentang tanggal.
    start, end: ISO8601 string, e.g. '2024-01-01T00:00:00+07:00'
    """
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT rec, station_id, station_name, imei,
                       level_m, recorded_at, fetched_at
                FROM water_level_observations
                WHERE imei = ?
                  AND recorded_at >= ?
                  AND recorded_at <= ?
                ORDER BY recorded_at ASC
                LIMIT ?
            """, (imei, start, end, limit))
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()


def get_today(db_path: str, imei: str) -> List[Dict]:
    """Ambil semua observasi hari ini (WIB)."""
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    start = f"{today}T00:00:00+07:00"
    end   = f"{today}T23:59:59+07:00"
    return get_by_date_range(db_path, imei, start, end)


def get_stats(db_path: str, imei: str) -> Dict:
    """
    Ambil statistik keseluruhan data untuk imei ini.
    Return: total_records, oldest_record, newest_record, date_range_days
    """
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT
                    COUNT(*)           AS total_records,
                    MIN(recorded_at)   AS oldest_record,
                    MAX(recorded_at)   AS newest_record
                FROM water_level_observations
                WHERE imei = ?
            """, (imei,))
            row = cursor.fetchone()
            if not row or row["total_records"] == 0:
                return {
                    "total_records": 0,
                    "oldest_record": None,
                    "newest_record": None,
                    "date_range_days": 0,
                }

            oldest = row["oldest_record"]
            newest = row["newest_record"]
            days = 0
            if oldest and newest:
                try:
                    # Parse untuk hitung selisih hari
                    fmt = "%Y-%m-%dT%H:%M:%S%z"
                    t1 = datetime.strptime(oldest, fmt)
                    t2 = datetime.strptime(newest, fmt)
                    days = (t2 - t1).days
                except Exception:
                    pass

            return {
                "total_records": row["total_records"],
                "oldest_record": oldest,
                "newest_record": newest,
                "date_range_days": days,
            }
        finally:
            conn.close()


def get_recent_fetch_logs(db_path: str, imei: str, limit: int = 20) -> List[Dict]:
    """Ambil log fetch terbaru untuk monitoring."""
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT fetched_at, status, rec, level_m, recorded_at, message
                FROM fetch_log
                WHERE imei = ?
                ORDER BY id DESC
                LIMIT ?
            """, (imei, limit))
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()


def rec_exists(db_path: str, rec: int) -> bool:
    """Cek apakah rec tertentu sudah ada di database."""
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute(
                "SELECT 1 FROM water_level_observations WHERE rec = ? LIMIT 1",
                (rec,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()


def get_latest_rec(db_path: str, imei: str) -> Optional[int]:
    """
    Ambil nilai rec terbesar (terbaru) untuk imei ini.
    Berguna untuk mendeteksi apakah ada data baru dari API.
    """
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT MAX(rec) as max_rec
                FROM water_level_observations
                WHERE imei = ?
            """, (imei,))
            row = cursor.fetchone()
            return row["max_rec"] if row and row["max_rec"] is not None else None
        finally:
            conn.close()