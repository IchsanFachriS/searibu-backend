"""Luwes water-level Flask blueprint.

Endpoints:
    GET  /api/luwes/level    — latest observation from the database
    GET  /api/luwes/history  — paginated history by date range
    GET  /api/luwes/status   — scheduler health and DB statistics
    POST /api/luwes/fetch    — trigger an immediate fetch
    GET  /api/luwes/overlay  — combined Luwes + TPXO data for a given date
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

from .luwes_service import get_latest_level, get_history, LUWES_IMEI, LuwesAPIError
from .luwes_scheduler import get_scheduler_status, trigger_fetch_now

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

luwes_bp = Blueprint("luwes", __name__, url_prefix="/api/luwes")


@luwes_bp.route("/level")
def api_get_level():
    imei = request.args.get("imei", LUWES_IMEI)
    try:
        return jsonify(get_latest_level(imei)), 200
    except LuwesAPIError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/history")
def api_get_history():
    imei = request.args.get("imei", LUWES_IMEI)
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    for param_name, param_val in (("start", start_date), ("end", end_date)):
        if param_val:
            try:
                datetime.strptime(param_val, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"Invalid '{param_name}' format. Use YYYY-MM-DD"}), 400

    try:
        return jsonify(get_history(imei, start_date, end_date)), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/status")
def api_get_status():
    try:
        return jsonify(get_scheduler_status()), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/fetch", methods=["POST"])
def api_trigger_fetch():
    imei = request.args.get("imei", LUWES_IMEI)
    result = trigger_fetch_now(imei)
    if "error" in result:
        return jsonify(result), 502
    return jsonify(result), 200


@luwes_bp.route("/overlay")
def api_get_overlay():
    """Return combined Luwes RAW observations and TPXO predictions for one day.

    Query params:
        lon  (float, required)
        lat  (float, required)
        date (YYYY-MM-DD, default: today WIB)
        imei (str, default: LUWES_IMEI)
    """
    lon_str = request.args.get("lon")
    lat_str = request.args.get("lat")
    date_str = request.args.get("date")
    imei = request.args.get("imei", LUWES_IMEI)

    if lon_str is None or lat_str is None:
        return jsonify({"error": "Parameters lon and lat are required"}), 400

    try:
        lon = float(lon_str)
        lat = float(lat_str)
    except ValueError:
        return jsonify({"error": "lon and lat must be numeric"}), 400

    if not (-180 <= lon <= 180):
        return jsonify({"error": "lon must be between -180 and 180"}), 400
    if not (-90 <= lat <= 90):
        return jsonify({"error": "lat must be between -90 and 90"}), 400

    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
    else:
        date_str = datetime.now(WIB).strftime("%Y-%m-%d")

    try:
        from .luwes_db import get_by_date_range

        rows = get_by_date_range(
            "",
            imei,
            f"{date_str}T00:00:00+07:00",
            f"{date_str}T23:59:59+07:00",
            limit=10_000,
        )
        luwes_obs = [{"recorded_at": r["recorded_at"], "level_m": r["level_m"]} for r in rows if r.get("level_m") is not None]
        levels = [o["level_m"] for o in luwes_obs]
        luwes_stats = (
            {"max_m": round(max(levels), 4), "min_m": round(min(levels), 4), "count": len(levels)}
            if levels
            else {"max_m": None, "min_m": None, "count": 0}
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to retrieve Luwes data: {exc}"}), 500

    tpxo_predictions = []
    tpxo_stats = {}
    tpxo_error = None

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from core.tpxo_predictor import TPXOPredictor

        db_path = os.getenv("DATABASE_PATH", "data/tpxo_seribu.db")
        if not Path(db_path).exists():
            tpxo_error = f"TPXO database not found at: {db_path}"
        else:
            predictor = TPXOPredictor(db_path)
            predictor.connect()
            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            result = predictor.predict(lon=lon, lat=lat, start_dt=start_dt, end_dt=start_dt + timedelta(days=1), interval_hours=1)
            tpxo_predictions = result.get("predictions", [])
            tpxo_stats = result.get("statistics", {})
            predictor.close()
    except FileNotFoundError as exc:
        tpxo_error = f"TPXO database not found: {exc}"
    except Exception as exc:
        tpxo_error = f"TPXO prediction failed: {exc}"

    return jsonify({
        "date": date_str,
        "imei": imei,
        "lon": lon,
        "lat": lat,
        "luwes_obs": luwes_obs,
        "tpxo": tpxo_predictions,
        "luwes_stats": luwes_stats,
        "tpxo_stats": tpxo_stats,
        "tpxo_error": tpxo_error,
    }), 200