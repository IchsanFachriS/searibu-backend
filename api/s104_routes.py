"""
s104_routes.py — Flask Blueprint untuk endpoint IHO S-104

Endpoints:
  GET  /api/s104/export          → unduh file HDF5 S-104 (TPXO)
  GET  /api/s104/export/luwes    → unduh file HDF5 S-104 (Luwes observasi)
  GET  /api/s104/json            → preview JSON struktur S-104 (untuk frontend)
  GET  /api/s104/validate        → validasi file S-104 yang sudah ada
  GET  /api/s104/metadata        → metadata compliance S-104 untuk titik tertentu
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger(__name__)

s104_bp = Blueprint("s104", __name__, url_prefix="/api/s104")

_predictor  = None
_luwes_db   = None
_luwes_imei = None


def setup_s104(predictor, luwes_db_path: str, luwes_imei: str):
    global _predictor, _luwes_db, _luwes_imei
    _predictor  = predictor
    _luwes_db   = luwes_db_path
    _luwes_imei = luwes_imei


def _today_wib() -> str:
    return datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")


def _parse_params():
    """Parse & validasi parameter lon, lat, date dari request."""
    lon_str  = request.args.get("lon")
    lat_str  = request.args.get("lat")
    date_str = request.args.get("date", _today_wib())

    if lon_str is None or lat_str is None:
        return None, None, None, "Parameter lon dan lat wajib diisi"
    try:
        lon = float(lon_str)
        lat = float(lat_str)
    except ValueError:
        return None, None, None, "lon dan lat harus berupa angka"

    if not (-180 <= lon <= 180):
        return None, None, None, "lon harus antara -180 dan 180"
    if not (-90 <= lat <= 90):
        return None, None, None, "lat harus antara -90 dan 90"
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None, None, None, "Format date tidak valid, gunakan YYYY-MM-DD"

    return lon, lat, date_str, None


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/s104/export  —  S-104 HDF5 dari TPXO (astronomicalPrediction)
# ─────────────────────────────────────────────────────────────────────────────
@s104_bp.route("/export")
def export_s104():
    """
    Unduh file HDF5 IHO S-104 Ed.2.0.0 untuk prediksi pasut TPXO.

    Query params:
      lon   : longitude (float, wajib)
      lat   : latitude  (float, wajib)
      date  : YYYY-MM-DD (default: hari ini WIB)

    Response:
      application/x-hdf  →  file .h5 untuk diunduh
    """
    lon, lat, date_str, err = _parse_params()
    if err:
        return jsonify({"error": err}), 400

    if _predictor is None:
        return jsonify({"error": "Predictor belum diinisialisasi"}), 503

    try:
        from .s104_exporter import export_s104_tpxo
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt   = start_dt + timedelta(days=1)

        result = _predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=1,
        )

        grid = result.get("grid", {})
        path = export_s104_tpxo(
            predictions     = result["predictions"],
            grid_lat        = grid.get("lat", lat),
            grid_lon        = grid.get("lon", lon),
            grid_distance_km= grid.get("distance_km", 0.0),
            date_str        = date_str,
        )

        filename = f"searibu_s104_tpxo_{date_str}_{abs(lat):.3f}_{lon:.3f}.h5"
        return send_file(
            path,
            as_attachment   = True,
            download_name   = filename,
            mimetype        = "application/x-hdf",
        )

    except Exception as e:
        logger.error(f"S-104 TPXO export error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/s104/export/luwes  —  S-104 HDF5 dari Luwes (observed)
# ─────────────────────────────────────────────────────────────────────────────
@s104_bp.route("/export/luwes")
def export_s104_luwes_endpoint():
    """
    Unduh file HDF5 IHO S-104 Ed.2.0.0 untuk data observasi stasiun Luwes.

    Query params:
      date  : YYYY-MM-DD (default: hari ini WIB)
      apply_tol : 'true'/'false' (default: true) — koreksi TOL -2.156 m
    """
    date_str  = request.args.get("date", _today_wib())
    apply_tol = request.args.get("apply_tol", "true").lower() != "false"

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Format date tidak valid"}), 400

    if _luwes_db is None:
        return jsonify({"error": "Luwes DB belum diinisialisasi"}), 503

    try:
        from .s104_exporter import export_s104_luwes
        from .luwes_db import get_by_date_range

        start_str = f"{date_str}T00:00:00+07:00"
        end_str   = f"{date_str}T23:59:59+07:00"
        rows      = get_by_date_range(_luwes_db, _luwes_imei, start_str, end_str, 10000)

        if not rows:
            return jsonify({
                "error": f"Tidak ada data observasi Luwes untuk {date_str}",
                "hint":  "Data hanya tersedia sejak scheduler pertama kali dijalankan"
            }), 404

        path = export_s104_luwes(
            observations = rows,
            station_meta = {
                "imei": _luwes_imei,
                "lat":  -5.7439,
                "lon":  106.6128,
                "name": "Luwes Tidal Station — Kepulauan Seribu",
            },
            date_str  = date_str,
            apply_tol = apply_tol,
        )

        filename = f"searibu_s104_luwes_{date_str}.h5"
        return send_file(
            path,
            as_attachment = True,
            download_name = filename,
            mimetype      = "application/x-hdf",
        )

    except Exception as e:
        logger.error(f"S-104 Luwes export error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/s104/json  —  preview JSON (untuk frontend React)
# ─────────────────────────────────────────────────────────────────────────────
@s104_bp.route("/json")
def s104_json():
    """
    Preview struktur data S-104 dalam format JSON (tidak menghasilkan HDF5).
    Digunakan oleh frontend untuk menampilkan compliance badge dan statistik.
    """
    lon, lat, date_str, err = _parse_params()
    if err:
        return jsonify({"error": err}), 400

    if _predictor is None:
        return jsonify({"error": "Predictor belum diinisialisasi"}), 503

    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt   = start_dt + timedelta(days=1)
        result   = _predictor.predict(lon=lon, lat=lat, start_dt=start_dt, end_dt=end_dt)
        preds    = result["predictions"]
        stats    = result["statistics"]
        grid     = result["grid"]

        return jsonify({
            "s100_compliance": {
                "productSpecification": "INT.IHO.S-104.2.0",
                "edition":             "2.0.0",
                "horizontalCRS":       4326,
                "horizontalCRSName":   "WGS 84 (EPSG:4326)",
                "verticalDatum":       12,
                "verticalDatumName":   "Mean Sea Level (MSL)",
                "verticalCoordinateBase": 2,
                "dataDynamicity":      1,
                "dataDynamicityLabel": "astronomicalPrediction",
                "producer":            "Searibu — ITB Geodesy and Geomatics Engineering",
                "model":               "TPXO9-atlas-v5",
                "method":              "Harmonic Analysis (Schureman 1958, Foreman 1977)",
                "constituents":        result.get("metadata", {}).get("n_constituents", 15),
                "references": [
                    "IHO S-100 Ed.5.2.0 (2024)",
                    "IHO S-104 Ed.2.0.0 (2024)",
                    "Amanda et al. (2023) ITB Capstone — S-100 Process Design",
                ]
            },
            "request": {
                "lon": lon, "lat": lat, "date": date_str,
                "timeRecordInterval": 3600,
            },
            "grid": grid,
            "statistics": stats,
            "waterLevelFeatures": [
                {
                    "DateTime":          p["time"],
                    "waterLevelHeight":  p["height"],
                    "waterLevelTrend":   _trend_label(p["height"],
                                            preds[i-1]["height"] if i > 0 else p["height"]),
                }
                for i, p in enumerate(preds)
            ],
        })

    except Exception as e:
        logger.error(f"S-104 JSON preview error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def _trend_label(current: float, prev: float) -> str:
    diff = current - prev
    if diff > 0.1:  return "increasing"
    if diff < -0.1: return "decreasing"
    return "steady"


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/s104/validate  —  validasi file HDF5 S-104
# ─────────────────────────────────────────────────────────────────────────────
@s104_bp.route("/validate")
def validate_s104():
    """
    Validasi struktur file HDF5 S-104 yang sudah ada.
    Query param: path (path absolut file .h5)
    """
    file_path = request.args.get("path")
    if not file_path:
        return jsonify({"error": "Parameter path wajib diisi"}), 400
    if not os.path.exists(file_path):
        return jsonify({"error": f"File tidak ditemukan: {file_path}"}), 404

    try:
        from .s104_exporter import validate_s104_file
        result = validate_s104_file(file_path)
        code = 200 if result["status"] == "valid" else 422
        return jsonify(result), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/s104/metadata  —  metadata compliance untuk titik tertentu
# ─────────────────────────────────────────────────────────────────────────────
@s104_bp.route("/metadata")
def s104_metadata():
    """Info compliance S-100/S-104 tanpa generate file."""
    return jsonify({
        "standard":      "IHO S-100 Universal Hydrographic Data Model",
        "productSpec":   "S-104 Water Level Information for Surface Navigation",
        "edition":       "2.0.0",
        "adoptedDate":   "December 2024",
        "encoding":      "HDF5 (Hierarchical Data Format version 5)",
        "horizontalCRS": "EPSG:4326 (WGS 84)",
        "verticalDatum": "MSL (Mean Sea Level) — IHO code 12",
        "dataSources": {
            "tpxo": {
                "type":          "astronomicalPrediction",
                "dataDynamicity": 1,
                "model":         "TPXO9-atlas-v5 (Oregon State University)",
                "constituents":  15,
                "interval":      "PT1H",
            },
            "luwes": {
                "type":          "observed",
                "dataDynamicity": 3,
                "station":       "Luwes Telemetry — Pushidrosal",
                "imei":          "869556066101370",
                "interval":      "PT5M (nominal)",
                "tolCorrection": "-2.156 m (Transfer of Level to MSL)",
            },
        },
        "references": {
            "S-100": "https://iho.int/uploads/user/pubs/standards/s-100/S-100_5.2.0_Final_Clean.pdf",
            "S-104": "https://iho.int/en/s-100-based-product-specifications",
            "s100py": "https://s100py.readthedocs.io",
            "epsg4326": "https://epsg.io/4326",
            "itbReference": "Amanda et al. (2023) ITB Capstone Design Project — S-100 Process Design",
        },
        "endpoints": {
            "exportTPXO":   "GET /api/s104/export?lon=&lat=&date=",
            "exportLuwes":  "GET /api/s104/export/luwes?date=",
            "previewJSON":  "GET /api/s104/json?lon=&lat=&date=",
            "validate":     "GET /api/s104/validate?path=",
        }
    })