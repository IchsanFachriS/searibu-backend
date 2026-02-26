"""
Flask Routes — Luwes Water Level

Endpoints:
  GET /api/luwes/level          → data terbaru dari DB (atau fetch jika kosong)
  GET /api/luwes/history        → history data (query by date range)
  GET /api/luwes/status         → status scheduler + statistik DB
  POST /api/luwes/fetch         → trigger manual fetch sekarang (admin/debug)
"""

from flask import Blueprint, jsonify, request
from .luwes_service import (
    get_latest_level,
    get_history,
    LUWES_IMEI,
    LuwesAPIError,
)
from .luwes_scheduler import get_scheduler_status, trigger_fetch_now

luwes_bp = Blueprint("luwes", __name__, url_prefix="/api/luwes")


@luwes_bp.route("/level")
def api_get_level():
    """
    GET /api/luwes/level
    GET /api/luwes/level?imei=<custom_imei>

    Mengembalikan data water level terbaru dari DB.
    Jika DB kosong untuk IMEI ini, otomatis fetch dari API.

    Response 200:
    {
        "rec":          96853184,
        "station_id":   552,
        "station_name": "Stasiun APBS",
        "imei":         "869556066101370",
        "level_m":      0.986,
        "recorded_at":  "2026-02-20T13:08:00+07:00",
        "fetched_at":   "2026-02-20T13:10:00+07:00"
    }

    Response 502: { "error": "..." }
    """
    imei = request.args.get("imei", LUWES_IMEI)
    try:
        data = get_latest_level(imei)
        return jsonify(data), 200
    except LuwesAPIError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/history")
def api_get_history():
    """
    GET /api/luwes/history
    GET /api/luwes/history?imei=<imei>&start=YYYY-MM-DD&end=YYYY-MM-DD

    Mengembalikan history data water level dari DB.
    Default: 7 hari terakhir jika start/end tidak diberikan.
    Data diambil dari semua history yang tersimpan (tidak dibatasi hari ini saja).

    Query params:
      imei  : IMEI stasiun (opsional, default LUWES_IMEI)
      start : tanggal awal 'YYYY-MM-DD' WIB (opsional)
      end   : tanggal akhir 'YYYY-MM-DD' WIB (opsional)

    Response 200:
    {
        "imei":          "869556066101370",
        "query_start":   "2026-02-13T00:00:00+07:00",
        "query_end":     "2026-02-20T23:59:59+07:00",
        "total_records": 9856,
        "statistics": {
            "max_m":    1.432,
            "min_m":    0.124,
            "mean_m":   0.876,
            "latest_m": 1.102
        },
        "db_stats": {
            "total_records":    98560,
            "oldest_record":    "2024-01-01T08:00:00+07:00",
            "newest_record":    "2026-02-20T13:08:00+07:00",
            "date_range_days":  780
        },
        "data": [ {...}, ... ]
    }
    """
    imei       = request.args.get("imei", LUWES_IMEI)
    start_date = request.args.get("start")
    end_date   = request.args.get("end")

    # Validasi format tanggal jika ada
    if start_date:
        try:
            from datetime import datetime
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Format 'start' tidak valid. Gunakan YYYY-MM-DD"}), 400

    if end_date:
        try:
            from datetime import datetime
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Format 'end' tidak valid. Gunakan YYYY-MM-DD"}), 400

    try:
        data = get_history(imei, start_date, end_date)
        return jsonify(data), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/status")
def api_get_status():
    """
    GET /api/luwes/status

    Mengembalikan status scheduler dan statistik database.

    Response 200:
    {
        "scheduler": {
            "running":             true,
            "imei":                "869556066101370",
            "fetch_interval_secs": 60
        },
        "counters": {
            "total_fetches":     1440,
            "new_records":       720,
            "duplicates":        720,
            "errors":            0,
            "last_fetch_time":   "2026-02-20T13:10:00+07:00",
            "last_new_rec_time": "2026-02-20T13:08:00+07:00",
            "last_error":        null
        },
        "db": {
            "total_records":    98560,
            "oldest_record":    "2024-01-01T08:00:00+07:00",
            "newest_record":    "2026-02-20T13:08:00+07:00",
            "date_range_days":  780
        }
    }
    """
    try:
        status = get_scheduler_status()
        return jsonify(status), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/fetch", methods=["POST"])
def api_trigger_fetch():
    """
    POST /api/luwes/fetch
    POST /api/luwes/fetch?imei=<custom_imei>

    Trigger manual fetch dari Luwes API sekarang.
    Berguna untuk testing atau memastikan data terkini.

    Response 200:
    {
        "obs":    { ... data observasi ... },
        "is_new": true,
        "status": "ok"   -- atau "duplicate"
    }

    Response 502: { "error": "..." }
    """
    imei = request.args.get("imei", LUWES_IMEI)
    result = trigger_fetch_now(imei)
    if "error" in result:
        return jsonify(result), 502
    return jsonify(result), 200