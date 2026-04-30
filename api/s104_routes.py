"""IHO S-104 Flask blueprint.

Endpoints:
    GET /api/s104/export        — download S-104 HDF5 (TPXO astronomical prediction)
    GET /api/s104/export/luwes  — download S-104 HDF5 (Luwes observed data)
    GET /api/s104/json          — JSON preview of S-104 water level data
    GET /api/s104/validate      — validate an existing S-104 HDF5 file
    GET /api/s104/metadata      — S-100/S-104 compliance metadata
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger(__name__)

s104_bp = Blueprint("s104", __name__, url_prefix="/api/s104")

_predictor = None
_luwes_db: str | None = None
_luwes_imei: str | None = None


def setup_s104(predictor, luwes_db_path: str, luwes_imei: str) -> None:
    """Inject dependencies. Call once at application startup."""
    global _predictor, _luwes_db, _luwes_imei
    _predictor = predictor
    _luwes_db = luwes_db_path
    _luwes_imei = luwes_imei


def _today_wib() -> str:
    return datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")


def _parse_params():
    """Parse and validate lon, lat, date query parameters.

    Returns (lon, lat, date_str, error_message).
    """
    lon_str = request.args.get("lon")
    lat_str = request.args.get("lat")
    date_str = request.args.get("date", _today_wib())

    if lon_str is None or lat_str is None:
        return None, None, None, "Parameters lon and lat are required"
    try:
        lon, lat = float(lon_str), float(lat_str)
    except ValueError:
        return None, None, None, "lon and lat must be numeric"
    if not (-180 <= lon <= 180):
        return None, None, None, "lon must be between -180 and 180"
    if not (-90 <= lat <= 90):
        return None, None, None, "lat must be between -90 and 90"
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None, None, None, "Invalid date format — use YYYY-MM-DD"

    return lon, lat, date_str, None


def _wib_date(iso_utc: str) -> str:
    dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=7)).strftime("%Y-%m-%d")


@s104_bp.route("/export")
def export_s104():
    """Download an IHO S-104 HDF5 file for TPXO astronomical predictions."""
    lon, lat, date_str, err = _parse_params()
    if err:
        return jsonify({"error": err}), 400
    if _predictor is None:
        return jsonify({"error": "Predictor not initialised"}), 503

    try:
        from .s104_exporter import export_s104_tpxo

        prev_day = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        start_dt = datetime.strptime(prev_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(next_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        result = _predictor.predict(lon=lon, lat=lat, start_dt=start_dt, end_dt=end_dt, interval_hours=1)
        filtered = [p for p in result["predictions"] if _wib_date(p["time"]) == date_str]
        grid = result.get("grid", {})

        path = export_s104_tpxo(
            predictions=filtered,
            grid_lat=grid.get("lat", lat),
            grid_lon=grid.get("lon", lon),
            grid_distance_km=grid.get("distance_km", 0.0),
            date_str=date_str,
        )
        filename = f"searibu_s104_tpxo_{date_str}_{abs(lat):.3f}_{lon:.3f}.h5"
        return send_file(path, as_attachment=True, download_name=filename, mimetype="application/x-hdf")

    except Exception as exc:
        logger.error("S-104 TPXO export error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@s104_bp.route("/export/luwes")
def export_s104_luwes_endpoint():
    """Download an IHO S-104 HDF5 file for Luwes observed water level data.

    Query params:
        date      (YYYY-MM-DD, default: today WIB)
        apply_tol (true/false, default: true)
    """
    date_str = request.args.get("date", _today_wib())
    apply_tol = request.args.get("apply_tol", "true").lower() != "false"

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format — use YYYY-MM-DD"}), 400

    if _luwes_db is None:
        return jsonify({"error": "Luwes database not initialised"}), 503

    try:
        from .s104_exporter import export_s104_luwes
        from .luwes_db import get_by_date_range

        rows = get_by_date_range(
            _luwes_db,
            _luwes_imei,
            f"{date_str}T00:00:00+07:00",
            f"{date_str}T23:59:59+07:00",
            10_000,
        )
        if not rows:
            return jsonify({"error": f"No Luwes observations found for {date_str}"}), 404

        path = export_s104_luwes(
            observations=rows,
            station_meta={"imei": _luwes_imei, "lat": -5.7439, "lon": 106.6128, "name": "Luwes Tidal Station — Kepulauan Seribu"},
            date_str=date_str,
            apply_tol=apply_tol,
        )
        return send_file(path, as_attachment=True, download_name=f"searibu_s104_luwes_{date_str}.h5", mimetype="application/x-hdf")

    except Exception as exc:
        logger.error("S-104 Luwes export error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@s104_bp.route("/json")
def s104_json():
    """Return an S-104 water level dataset as JSON for frontend consumption."""
    lon, lat, date_str, err = _parse_params()
    if err:
        return jsonify({"error": err}), 400
    if _predictor is None:
        return jsonify({"error": "Predictor not initialised"}), 503

    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        result = _predictor.predict(lon=lon, lat=lat, start_dt=start_dt, end_dt=start_dt + timedelta(days=1))
        preds = result["predictions"]

        return jsonify({
            "s100_compliance": {
                "productSpecification": "INT.IHO.S-104.2.0",
                "edition": "2.0.0",
                "horizontalCRS": 4326,
                "horizontalCRSName": "WGS 84 (EPSG:4326)",
                "verticalDatum": 12,
                "verticalDatumName": "Mean Sea Level (MSL)",
                "verticalCoordinateBase": 2,
                "dataDynamicity": 1,
                "dataDynamicityLabel": "astronomicalPrediction",
                "producer": "Searibu — ITB Geodesy and Geomatics Engineering",
                "model": "TPXO9-atlas-v5",
                "method": "Harmonic Analysis (Schureman 1958, Foreman 1977)",
                "constituents": result.get("metadata", {}).get("n_constituents", 15),
                "references": ["IHO S-100 Ed.5.2.0 (2024)", "IHO S-104 Ed.2.0.0 (2024)"],
            },
            "request": {"lon": lon, "lat": lat, "date": date_str, "timeRecordInterval": 3600},
            "grid": result.get("grid"),
            "statistics": result.get("statistics"),
            "waterLevelFeatures": [
                {
                    "DateTime": p["time"],
                    "waterLevelHeight": p["height"],
                    "waterLevelTrend": _trend_label(p["height"], preds[i - 1]["height"] if i > 0 else p["height"]),
                }
                for i, p in enumerate(preds)
            ],
        })

    except Exception as exc:
        logger.error("S-104 JSON error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


def _trend_label(current: float, prev: float) -> str:
    diff = current - prev
    if diff > 0.1:
        return "increasing"
    if diff < -0.1:
        return "decreasing"
    return "steady"


@s104_bp.route("/validate")
def validate_s104():
    """Validate an existing S-104 HDF5 file.

    Query params:
        path (str, required) — absolute path to the .h5 file
    """
    file_path = request.args.get("path")
    if not file_path:
        return jsonify({"error": "Parameter 'path' is required"}), 400
    if not os.path.exists(file_path):
        return jsonify({"error": f"File not found: {file_path}"}), 404

    try:
        from .s104_exporter import validate_s104_file
        result = validate_s104_file(file_path)
        return jsonify(result), 200 if result["status"] == "valid" else 422
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@s104_bp.route("/metadata")
def s104_metadata():
    """Return S-100/S-104 compliance metadata for this system."""
    return jsonify({
        "standard": "IHO S-100 Universal Hydrographic Data Model",
        "productSpec": "S-104 Water Level Information for Surface Navigation",
        "edition": "2.0.0",
        "adoptedDate": "December 2024",
        "encoding": "HDF5 (Hierarchical Data Format version 5)",
        "horizontalCRS": "EPSG:4326 (WGS 84)",
        "verticalDatum": "MSL (Mean Sea Level) — IHO code 12",
        "dataSources": {
            "tpxo": {
                "type": "astronomicalPrediction",
                "dataDynamicity": 1,
                "model": "TPXO9-atlas-v5 (Oregon State University)",
                "constituents": 15,
                "interval": "PT1H",
            },
            "luwes": {
                "type": "observed",
                "dataDynamicity": 3,
                "station": "Luwes Telemetry — Pushidrosal",
                "imei": "869556066101370",
                "interval": "PT5M (nominal)",
                "tolCorrection": "-2.156 m (Transfer of Level to MSL)",
            },
        },
        "references": {
            "S-100": "https://iho.int/uploads/user/pubs/standards/s-100/S-100_5.2.0_Final_Clean.pdf",
            "S-104": "https://iho.int/en/s-100-based-product-specifications",
            "s100py": "https://s100py.readthedocs.io",
        },
        "endpoints": {
            "exportTPXO": "GET /api/s104/export?lon=&lat=&date=",
            "exportLuwes": "GET /api/s104/export/luwes?date=",
            "previewJSON": "GET /api/s104/json?lon=&lat=&date=",
            "validate": "GET /api/s104/validate?path=",
        },
    })