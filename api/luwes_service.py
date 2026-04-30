"""Luwes telemetry service.

Handles HTTP communication with the Luwes API and persistence of
water-level observations into PostgreSQL.
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from .luwes_db import insert_observation, insert_fetch_log, get_latest, get_stats, get_by_date_range

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

LUWES_URL = "http://data3.luwesinovasimandiri.com:8002/last"
LUWES_IMEI = "869556066101370"
REQUEST_TIMEOUT = 15

_db_path: str = ""


class LuwesAPIError(Exception):
    """Raised when the Luwes API returns an error or is unreachable."""


def setup_luwes(db_path: str = "") -> None:
    """Initialise the service. db_path is unused in PostgreSQL mode."""
    global _db_path
    _db_path = db_path or ""
    logger.info("luwes_service: PostgreSQL mode active")


def _require_db() -> str:
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
        raise LuwesAPIError(f"Cannot reach Luwes server: {exc}") from exc
    except Exception as exc:
        raise LuwesAPIError(f"Request error: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LuwesAPIError(f"Invalid JSON response: {raw[:200]}") from exc

    if isinstance(data.get("error"), int):
        codes = {1: "Unknown action", 2: "JSON encoding error", 3: "Station not found"}
        code = data["error"]
        raise LuwesAPIError(f"Luwes API error {code}: {codes.get(code, str(code))}")

    return data


def _normalize(data: Dict) -> Dict:
    return {
        "rec": data.get("rec"),
        "station_id": data.get("id"),
        "station_name": data.get("name"),
        "imei": data.get("imei"),
        "level_m": data.get("level_sensor"),
        "recorded_at": _to_wib(data.get("submitted_at")),
        "fetched_at": datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00"),
    }


def _to_wib(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        except ValueError:
            continue
    logger.warning("Cannot parse Luwes timestamp: %s", raw)
    return raw


def fetch_and_store(imei: str = LUWES_IMEI) -> Dict:
    """Fetch the latest observation from the Luwes API and persist it.

    Returns:
        dict with keys: obs (dict), is_new (bool), status (str).
    """
    db = _require_db()
    now_wib = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    api_data = _call_luwes_api(imei)
    obs = _normalize(api_data)

    if obs.get("rec") is None:
        raise LuwesAPIError("API response missing 'rec' field")

    is_new = insert_observation(db, obs)
    status = "ok" if is_new else "duplicate"

    insert_fetch_log(
        db,
        {
            "fetched_at": now_wib,
            "imei": imei,
            "status": status,
            "rec": obs.get("rec"),
            "level_m": obs.get("level_m"),
            "recorded_at": obs.get("recorded_at"),
            "message": None,
        },
    )
    return {"obs": obs, "is_new": is_new, "status": status}


def get_latest_level(imei: str = LUWES_IMEI) -> Dict:
    """Return the latest observation from the database, fetching live if empty."""
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
    """Return paginated history with statistics for a date range."""
    db = _require_db()
    now_wib = datetime.now(WIB)

    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=WIB) if end_date else now_wib
    start_dt = (
        datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=WIB)
        if start_date
        else end_dt - timedelta(days=7)
    )

    start_str = start_dt.strftime("%Y-%m-%dT00:00:00+07:00")
    end_str = end_dt.strftime("%Y-%m-%dT23:59:59+07:00")

    rows = get_by_date_range(db, imei, start_str, end_str, limit=50_000)
    levels = [r["level_m"] for r in rows if r.get("level_m") is not None]

    return {
        "imei": imei,
        "query_start": start_str,
        "query_end": end_str,
        "total_records": len(rows),
        "statistics": {
            "max_m": round(max(levels), 4) if levels else None,
            "min_m": round(min(levels), 4) if levels else None,
            "mean_m": round(sum(levels) / len(levels), 4) if levels else None,
            "latest_m": round(levels[-1], 4) if levels else None,
        },
        "db_stats": get_stats(db, imei),
        "data": rows,
    }