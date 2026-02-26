"""
Luwes API Service — Fetch water level dari Luwes Server & simpan ke SQLite.

Endpoint Luwes yang digunakan:
  POST /last  → ambil data terbaru (satu record) untuk IMEI tertentu

Karena API hanya expose endpoint /last (data terbaru),
strategi pengumpulan history adalah:
  - Polling rutin setiap N detik
  - Setiap data baru (rec baru) langsung disimpan
  - Data tidak pernah dihapus → terakumulasi sebagai history lengkap

Semua timestamp internal: WIB (UTC+7, format ISO8601 +07:00)
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from .luwes_db import (
    insert_observation,
    insert_fetch_log,
    get_latest,
    get_latest_rec,
)

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

# ── Konfigurasi ────────────────────────────────────────────────
LUWES_URL  = "http://data3.luwesinovasimandiri.com:8002/last"
LUWES_IMEI = "869556066101370"

# Timeout koneksi ke Luwes server (detik)
REQUEST_TIMEOUT = 15

# Path DB — di-set saat setup_luwes() dipanggil
_db_path: Optional[str] = None


class LuwesAPIError(Exception):
    """Exception untuk error komunikasi dengan Luwes API."""
    pass


# ══════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════

def setup_luwes(db_path: str):
    """
    Set path database.
    Panggil sekali dari app.py saat startup, sebelum start_scheduler().
    """
    global _db_path
    _db_path = db_path


def _require_db() -> str:
    if not _db_path:
        raise RuntimeError(
            "Luwes DB belum diinisialisasi. Panggil setup_luwes() terlebih dahulu."
        )
    return _db_path


# ══════════════════════════════════════════════════════════════
# FETCH FROM API
# ══════════════════════════════════════════════════════════════

def _call_luwes_api(imei: str) -> Dict:
    """
    Panggil Luwes API endpoint /last.
    
    Return: dict response JSON dari API
    Raises: LuwesAPIError jika gagal
    """
    payload = urllib.parse.urlencode({"a": "stat", "imei": imei}).encode("utf-8")
    req = urllib.request.Request(
        LUWES_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise LuwesAPIError(f"Gagal menghubungi Luwes server: {exc}") from exc
    except Exception as exc:
        raise LuwesAPIError(f"Error request ke Luwes: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LuwesAPIError(f"Response bukan JSON valid: {raw[:200]}") from exc

    # Cek error code dari API
    if "error" in data and isinstance(data["error"], int):
        err_code = data["error"]
        messages = {
            1: "Unknown action — parameter 'a' tidak dikenali",
            2: "Error encoding JSON di sisi server",
            3: "Station not found — IMEI tidak terdaftar",
        }
        raise LuwesAPIError(
            f"Luwes API error {err_code}: {messages.get(err_code, f'Error code tidak dikenal: {err_code}')}"
        )

    return data


def _normalize_api_response(data: Dict) -> Dict:
    """
    Normalisasi response API Luwes ke format internal.
    
    Field API: id, imei, level_sensor, name, rec, submitted_at
    Field internal: station_id, imei, level_m, station_name, rec, recorded_at, fetched_at
    """
    recorded_at = _parse_timestamp_to_wib(data.get("submitted_at"))
    fetched_at  = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")

    return {
        "rec":          data.get("rec"),
        "station_id":   data.get("id"),
        "station_name": data.get("name"),
        "imei":         data.get("imei"),
        "level_m":      data.get("level_sensor"),
        "recorded_at":  recorded_at,
        "fetched_at":   fetched_at,
    }


def _parse_timestamp_to_wib(raw: Optional[str]) -> Optional[str]:
    """
    Parse timestamp dari API → WIB ISO8601 +07:00.
    API mengirimkan UTC (format: '2023-01-04T06:08:00Z').
    """
    if not raw:
        return None

    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        except ValueError:
            continue

    # Jika tidak bisa di-parse, kembalikan as-is
    logger.warning(f"Tidak bisa parse timestamp: {raw}")
    return raw


# ══════════════════════════════════════════════════════════════
# FETCH & STORE (dipanggil scheduler)
# ══════════════════════════════════════════════════════════════

def fetch_and_store(imei: str = LUWES_IMEI) -> Dict:
    """
    Fetch data terbaru dari Luwes API dan simpan ke DB jika baru.

    Return:
        dict dengan keys: obs (data observasi), is_new (bool), status (str)
    
    Raises: LuwesAPIError jika gagal komunikasi dengan API
    """
    db = _require_db()
    now_wib = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")

    # Panggil API
    api_data = _call_luwes_api(imei)
    obs = _normalize_api_response(api_data)

    rec = obs.get("rec")
    if rec is None:
        raise LuwesAPIError("Response API tidak mengandung field 'rec'")

    # Coba simpan ke DB
    is_new = insert_observation(db, obs)
    status = "ok" if is_new else "duplicate"

    # Log hasil fetch
    insert_fetch_log(db, {
        "fetched_at":  now_wib,
        "imei":        imei,
        "status":      status,
        "rec":         rec,
        "level_m":     obs.get("level_m"),
        "recorded_at": obs.get("recorded_at"),
        "message":     None,
    })

    return {
        "obs":    obs,
        "is_new": is_new,
        "status": status,
    }


# ══════════════════════════════════════════════════════════════
# ROUTE HANDLERS (dipanggil dari Flask routes)
# ══════════════════════════════════════════════════════════════

def get_latest_level(imei: str = LUWES_IMEI) -> Dict:
    """
    Ambil data terbaru dari DB.
    Jika DB kosong untuk imei ini, fetch dari API terlebih dahulu.
    """
    db = _require_db()
    latest = get_latest(db, imei)
    if latest:
        return latest

    # DB kosong → fetch sekali dari API
    result = fetch_and_store(imei)
    return result["obs"]


def get_history(
    imei: str = LUWES_IMEI,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict:
    """
    Ambil history data dari DB.
    
    Args:
        imei       : IMEI stasiun
        start_date : 'YYYY-MM-DD' (WIB), default: 7 hari lalu
        end_date   : 'YYYY-MM-DD' (WIB), default: hari ini
    
    Returns:
        dict {imei, start, end, total_records, statistics, data}
    """
    from .luwes_db import get_by_date_range, get_stats

    db = _require_db()

    # Default range: 7 hari terakhir
    now_wib = datetime.now(WIB)
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=WIB)
    else:
        end_dt = now_wib

    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=WIB)
    else:
        start_dt = end_dt - timedelta(days=7)

    start_str = start_dt.strftime("%Y-%m-%dT00:00:00+07:00")
    end_str   = end_dt.strftime("%Y-%m-%dT23:59:59+07:00")

    rows = get_by_date_range(db, imei, start_str, end_str, limit=50000)
    levels = [r["level_m"] for r in rows if r.get("level_m") is not None]

    stats_overall = get_stats(db, imei)

    return {
        "imei":          imei,
        "query_start":   start_str,
        "query_end":     end_str,
        "total_records": len(rows),
        "statistics": {
            "max_m":    round(max(levels), 4) if levels else None,
            "min_m":    round(min(levels), 4) if levels else None,
            "mean_m":   round(sum(levels) / len(levels), 4) if levels else None,
            "latest_m": round(levels[-1], 4) if levels else None,
        },
        "db_stats": stats_overall,
        "data": rows,
    }