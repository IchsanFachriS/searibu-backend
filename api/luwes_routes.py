"""
Flask Routes — Luwes Water Level

Endpoints:
  GET /api/luwes/level          → data terbaru dari DB (atau fetch jika kosong)
  GET /api/luwes/history        → history data (query by date range), RAW tanpa preprocessing
  GET /api/luwes/status         → status scheduler + statistik DB
  POST /api/luwes/fetch         → trigger manual fetch sekarang (admin/debug)
  GET /api/luwes/overlay        → data luwes RAW + prediksi TPXO untuk tanggal & lokasi tertentu
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

    Mengembalikan data water level terbaru dari DB (RAW, tanpa preprocessing).
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

    Mengembalikan history data water level dari DB (RAW, tanpa preprocessing/smoothing).
    Default: 7 hari terakhir jika start/end tidak diberikan.
    """
    imei       = request.args.get("imei", LUWES_IMEI)
    start_date = request.args.get("start")
    end_date   = request.args.get("end")

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
    """GET /api/luwes/status — status scheduler + statistik database."""
    try:
        status = get_scheduler_status()
        return jsonify(status), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@luwes_bp.route("/fetch", methods=["POST"])
def api_trigger_fetch():
    """POST /api/luwes/fetch — trigger manual fetch dari Luwes API sekarang."""
    imei = request.args.get("imei", LUWES_IMEI)
    result = trigger_fetch_now(imei)
    if "error" in result:
        return jsonify(result), 502
    return jsonify(result), 200


@luwes_bp.route("/overlay")
def api_get_overlay():
    """
    GET /api/luwes/overlay?date=YYYY-MM-DD&lon=<lon>&lat=<lat>&imei=<imei>

    Mengembalikan data gabungan:
      - luwes_obs : observasi RAW dari stasiun Luwes untuk tanggal tersebut
      - tpxo      : prediksi TPXO untuk lokasi & tanggal yang sama (interval 1 jam)

    Query params:
      date  : tanggal (YYYY-MM-DD), default hari ini WIB
      lon   : longitude lokasi TPXO (float, wajib)
      lat   : latitude lokasi TPXO  (float, wajib)
      imei  : IMEI stasiun Luwes (opsional, default LUWES_IMEI)

    Response 200:
    {
        "date":       "2026-03-10",
        "imei":       "869556066101370",
        "lon":        106.58,
        "lat":        -5.60,
        "luwes_obs":  [{"recorded_at": "...", "level_m": 1.234}, ...],
        "tpxo":       [{"time": "...", "height": 0.456}, ...],
        "luwes_stats": {"max_m": ..., "min_m": ..., "count": ...},
        "tpxo_stats":  {"max": ..., "min": ..., "mean": ..., "range": ...}
    }
    """
    import os
    import sys
    from pathlib import Path
    from datetime import datetime, timezone, timedelta

    WIB = timezone(timedelta(hours=7))

    # ── Parse params ──────────────────────────────────────────
    lon_str  = request.args.get("lon")
    lat_str  = request.args.get("lat")
    date_str = request.args.get("date")
    imei     = request.args.get("imei", LUWES_IMEI)

    if lon_str is None or lat_str is None:
        return jsonify({"error": "Parameter lon dan lat wajib diisi"}), 400

    try:
        lon = float(lon_str)
        lat = float(lat_str)
    except ValueError:
        return jsonify({"error": "lon dan lat harus berupa angka"}), 400

    if not (-180 <= lon <= 180):
        return jsonify({"error": "lon harus antara -180 dan 180"}), 400
    if not (-90 <= lat <= 90):
        return jsonify({"error": "lat harus antara -90 dan 90"}), 400

    # Tanggal
    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Format date tidak valid. Gunakan YYYY-MM-DD"}), 400
    else:
        date_str = datetime.now(WIB).strftime("%Y-%m-%d")

    # ── Fetch Luwes RAW ──────────────────────────────────────
    try:
        from .luwes_db import get_by_date_range, _connect
        import os as _os
        luwes_db = _os.getenv("LUWES_DB_PATH", "data/luwes_raw.db")

        start_str = f"{date_str}T00:00:00+07:00"
        end_str   = f"{date_str}T23:59:59+07:00"

        from .luwes_service import _db_path as luwes_db_path
        db = luwes_db_path or luwes_db

        rows = get_by_date_range(db, imei, start_str, end_str, limit=10000)
        luwes_obs = [
            {"recorded_at": r["recorded_at"], "level_m": r["level_m"]}
            for r in rows if r.get("level_m") is not None
        ]

        if luwes_obs:
            levels = [o["level_m"] for o in luwes_obs]
            luwes_stats = {
                "max_m":  round(max(levels), 4),
                "min_m":  round(min(levels), 4),
                "count":  len(levels),
            }
        else:
            luwes_stats = {"max_m": None, "min_m": None, "count": 0}

    except Exception as exc:
        return jsonify({"error": f"Gagal ambil data Luwes: {exc}"}), 500

    # ── Fetch TPXO ───────────────────────────────────────────
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from core.tpxo_predictor import TPXOPredictor

        db_path = _os.getenv("DATABASE_PATH", "data/tpxo_seribu.db")
        predictor = TPXOPredictor(db_path)
        predictor.connect()

        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # end_dt = next day 00:00:00 — backend requires end_date strictly after start_date
        end_dt   = start_dt + timedelta(days=1)

        tpxo_result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=1,
        )
        tpxo_predictions = tpxo_result.get("predictions", [])
        tpxo_stats       = tpxo_result.get("statistics", {})

    except Exception as exc:
        return jsonify({"error": f"Gagal ambil prediksi TPXO: {exc}"}), 500

    return jsonify({
        "date":        date_str,
        "imei":        imei,
        "lon":         lon,
        "lat":         lat,
        "luwes_obs":   luwes_obs,
        "tpxo":        tpxo_predictions,
        "luwes_stats": luwes_stats,
        "tpxo_stats":  tpxo_stats,
    }), 200