"""
Flask Routes — Luwes Water Level (PostgreSQL version)

Endpoints:
  GET /api/luwes/level          → data terbaru dari DB
  GET /api/luwes/history        → history data (query by date range)
  GET /api/luwes/status         → status scheduler + statistik DB
  POST /api/luwes/fetch         → trigger manual fetch sekarang
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
    try:
        status = get_scheduler_status()
        return jsonify(status), 200
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
    """
    GET /api/luwes/overlay?date=YYYY-MM-DD&lon=<lon>&lat=<lat>&imei=<imei>

    Mengembalikan data gabungan:
      - luwes_obs : observasi RAW dari stasiun Luwes untuk tanggal tersebut
      - tpxo      : prediksi TPXO untuk lokasi & tanggal yang sama (interval 1 jam)
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

    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Format date tidak valid. Gunakan YYYY-MM-DD"}), 400
    else:
        date_str = datetime.now(WIB).strftime("%Y-%m-%d")

    # ── Fetch Luwes RAW dari PostgreSQL ──────────────────────
    try:
        # PostgreSQL mode: db_path = "" (diabaikan oleh pg_db)
        from .luwes_db import get_by_date_range

        start_str = f"{date_str}T00:00:00+07:00"
        end_str   = f"{date_str}T23:59:59+07:00"

        rows = get_by_date_range("", imei, start_str, end_str, limit=10000)
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

    # ── Fetch TPXO (tetap SQLite) ─────────────────────────────
    tpxo_predictions = []
    tpxo_stats       = {}
    tpxo_error       = None

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from core.tpxo_predictor import TPXOPredictor

        db_path = os.getenv("DATABASE_PATH", "data/tpxo_seribu.db")

        if not Path(db_path).exists():
            tpxo_error = f"File TPXO tidak ditemukan di path: {db_path}"
        else:
            predictor = TPXOPredictor(db_path)
            predictor.connect()

            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt   = start_dt + timedelta(days=1)

            tpxo_result      = predictor.predict(
                lon=lon, lat=lat,
                start_dt=start_dt, end_dt=end_dt,
                interval_hours=1,
            )
            tpxo_predictions = tpxo_result.get("predictions", [])
            tpxo_stats       = tpxo_result.get("statistics", {})
            predictor.close()

    except FileNotFoundError as exc:
        tpxo_error = f"File TPXO tidak ditemukan: {exc}"
    except Exception as exc:
        tpxo_error = f"Gagal ambil prediksi TPXO: {exc}"

    return jsonify({
        "date":        date_str,
        "imei":        imei,
        "lon":         lon,
        "lat":         lat,
        "luwes_obs":   luwes_obs,
        "tpxo":        tpxo_predictions,
        "luwes_stats": luwes_stats,
        "tpxo_stats":  tpxo_stats,
        "tpxo_error":  tpxo_error,   # None jika sukses, string error jika gagal
    }), 200