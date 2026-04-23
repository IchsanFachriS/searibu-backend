"""
luwes_service.py — Updated untuk PostgreSQL
Perubahan: _db_path tidak lagi digunakan untuk koneksi (pg_db.py yang handle),
tapi tetap ada untuk backward-compat dengan luwes_routes.py yang import _db_path.
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
    get_stats,
    get_by_date_range,
)

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

LUWES_URL     = "http://data3.luwesinovasimandiri.com:8002/last"
LUWES_IMEI    = "869556066101370"
REQUEST_TIMEOUT = 15

# Dipertahankan agar luwes_routes.py yang import _db_path tidak error
_db_path: Optional[str] = ""


class LuwesAPIError(Exception):
    pass


def setup_luwes(db_path: str = ""):
    """
    Di PostgreSQL mode, db_path tidak digunakan untuk koneksi.
    Fungsi ini tetap ada agar app.py tidak perlu diubah.
    """
    global _db_path
    _db_path = db_path or ""
    logger.info("[luwes_service] PostgreSQL mode aktif")


def _require_db() -> str:
    """Kembalikan string kosong (pg_db.py tidak butuh path)."""
    return _db_path or ""


def _call_luwes_api(imei: str) -> Dict:
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

    if "error" in data and isinstance(data["error"], int):
        err_code = data["error"]
        messages = {
            1: "Unknown action",
            2: "Error encoding JSON di server",
            3: "Station not found — IMEI tidak terdaftar",
        }
        raise LuwesAPIError(f"Luwes API error {err_code}: {messages.get(err_code, str(err_code))}")

    return data


def _normalize_api_response(data: Dict) -> Dict:
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
    if not raw:
        return None
    formats = ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        except ValueError:
            continue
    logger.warning(f"Tidak bisa parse timestamp: {raw}")
    return raw


def fetch_and_store(imei: str = LUWES_IMEI) -> Dict:
    db       = _require_db()
    now_wib  = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    api_data = _call_luwes_api(imei)
    obs      = _normalize_api_response(api_data)

    rec = obs.get("rec")
    if rec is None:
        raise LuwesAPIError("Response API tidak mengandung field 'rec'")

    is_new = insert_observation(db, obs)
    status = "ok" if is_new else "duplicate"

    insert_fetch_log(db, {
        "fetched_at":  now_wib,
        "imei":        imei,
        "status":      status,
        "rec":         rec,
        "level_m":     obs.get("level_m"),
        "recorded_at": obs.get("recorded_at"),
        "message":     None,
    })

    return {"obs": obs, "is_new": is_new, "status": status}


def get_latest_level(imei: str = LUWES_IMEI) -> Dict:
    db = _require_db()
    latest = get_latest(db, imei)
    if latest:
        return latest
    result = fetch_and_store(imei)
    return result["obs"]


def get_history(
    imei: str = LUWES_IMEI,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict:
    db = _require_db()
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

    rows   = get_by_date_range(db, imei, start_str, end_str, limit=50000)
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