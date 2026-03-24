"""
TPXO Tide Prediction API — Flask
Backend utama sistem Searibu

Endpoints:
  GET  /                          → API index
  GET  /api/health                → Health check
  GET  /api/tide/prediction       → Prediksi pasut TPXO9
  GET  /api/luwes/level           → Data terbaru stasiun Luwes
  GET  /api/luwes/history         → History water level
  GET  /api/luwes/status          → Status scheduler
  POST /api/luwes/fetch           → Manual trigger fetch
  GET  /api/luwes/overlay         → Luwes RAW + TPXO overlay
  POST /api/auth/register         → Registrasi user
  POST /api/auth/login            → Login user
  GET  /api/s104/export           → S-104 HDF5 download (TPXO)
  GET  /api/s104/export/luwes     → S-104 HDF5 download (Luwes)
  GET  /api/s104/json             → S-104 JSON preview
  GET  /api/s104/metadata         → S-100/S-104 compliance info
  GET  /api/s104/validate         → Validasi file HDF5 S-104

Standar:
  IHO S-100 Universal Hydrographic Data Model Ed.5.2.0 (2024)
  IHO S-104 Water Level Information for Surface Navigation Ed.2.0.0 (2024)
"""

import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Path Setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Imports internal ──────────────────────────────────────────────────────────
from core.tpxo_predictor import TPXOPredictor
from api.luwes_routes    import luwes_bp
from api.luwes_service   import setup_luwes
from api.luwes_db        import init_db
from api.luwes_scheduler import start_scheduler
from api.auth_routes     import auth_bp, setup_auth
from api.auth_db         import init_auth_db
from api.s104_routes     import s104_bp, setup_s104

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── CORS ──────────────────────────────────────────────────────────────────────
_raw_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,https://searibu.vercel.app",
)
cors_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
CORS(
    app,
    resources={r"/api/*": {
        "origins": cors_origins,
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
    }},
)

# ── Register Blueprints ───────────────────────────────────────────────────────
app.register_blueprint(luwes_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(s104_bp)

# ── Config ────────────────────────────────────────────────────────────────────
# Lokasi file statis tpxo_seribu.db di dalam repo (/app/data/ di Railway)
_repo_data = Path(__file__).parent.parent / "data"

# Prioritas DATABASE_PATH:
#   1. Env var DATABASE_PATH (set manual di Railway jika mau override)
#   2. /app/data/tpxo_seribu.db (dari repo, selalu ada setelah commit)
DB_PATH = os.getenv(
    "DATABASE_PATH",
    str(_repo_data / "tpxo_seribu.db")
)

# Luwes & Auth DB disimpan di Volume /data agar persisten antar deploy
_volume_data = os.getenv("DATA_DIR", "/data")
os.makedirs(_volume_data, exist_ok=True)

LUWES_DB_PATH = os.getenv("LUWES_DB_PATH", f"{_volume_data}/luwes_raw.db")
AUTH_DB_PATH  = os.getenv("AUTH_DB_PATH",  f"{_volume_data}/auth.db")
LUWES_IMEI    = os.getenv("LUWES_IMEI",    "869556066101370")

# ── Init Databases ────────────────────────────────────────────────────────────
init_db(LUWES_DB_PATH)
setup_luwes(LUWES_DB_PATH)

init_auth_db(AUTH_DB_PATH)
setup_auth(AUTH_DB_PATH)

print("=" * 65)
print("  Searibu — TPXO Tide Prediction API  v2.2.0")
print(f"  TPXO Database  : {DB_PATH}")
print(f"  Luwes DB       : {LUWES_DB_PATH}")
print(f"  Auth DB        : {AUTH_DB_PATH}")
print(f"  Luwes IMEI     : {LUWES_IMEI}")
print(f"  CORS Origins   : {cors_origins}")

# ── Scheduler ─────────────────────────────────────────────────────────────────
_werkzeug_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "false"
if not _werkzeug_reloader_child:
    start_scheduler(db_path=LUWES_DB_PATH)
    print("  Luwes Scheduler : aktif (fetch tiap 60 detik)")

# ── TPXO Predictor ────────────────────────────────────────────────────────────
predictor = TPXOPredictor(DB_PATH)
try:
    predictor.connect()
    print("  ✅  Database TPXO terkoneksi")
except FileNotFoundError as e:
    # Jangan crash — server tetap berjalan, endpoint tide akan return 503
    logger.error(f"Database TPXO tidak ditemukan: {e}")
    print(f"  ❌  Database TPXO tidak ditemukan: {DB_PATH}")
    print("       Pastikan data/tpxo_seribu.db sudah di-commit ke repo.")
    predictor = None
except Exception as e:
    logger.error(f"Gagal koneksi database TPXO: {e}", exc_info=True)
    print(f"  ❌  Gagal koneksi database TPXO: {e}")
    predictor = None

# ── Setup S-104 ────────────────────────────────────────────────────────────────
if predictor is not None:
    setup_s104(predictor, LUWES_DB_PATH, LUWES_IMEI)
    print("  ✅  S-104 exporter siap (IHO S-104 Ed.2.0.0)")
else:
    setup_s104(None, LUWES_DB_PATH, LUWES_IMEI)
    print("  ⚠️   S-104 exporter nonaktif (database TPXO tidak tersedia)")

print("=" * 65)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Index
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "name":    "Searibu Marine Information API",
        "version": "2.2.0",
        "model":   "TPXO9-atlas-v5",
        "tpxo_ready": predictor is not None,
        "standards": {
            "S-100": "IHO Universal Hydrographic Data Model Ed.5.2.0",
            "S-104": "Water Level Information for Surface Navigation Ed.2.0.0",
        },
        "endpoints": {
            "GET  /api/health":             "Health check",
            "GET  /api/tide/prediction":    "?lon=&lat=&start_date=&end_date=&interval_hours=",
            "GET  /api/luwes/level":        "Data water level terbaru",
            "GET  /api/luwes/history":      "?start=YYYY-MM-DD&end=YYYY-MM-DD",
            "GET  /api/luwes/status":       "Status scheduler + statistik DB",
            "POST /api/luwes/fetch":        "Trigger manual fetch",
            "GET  /api/luwes/overlay":      "?date=&lon=&lat=",
            "POST /api/auth/register":      "{ full_name, email, password }",
            "POST /api/auth/login":         "{ email, password }",
            "GET  /api/s104/export":        "?lon=&lat=&date= → HDF5 TPXO",
            "GET  /api/s104/export/luwes":  "?date= → HDF5 Luwes",
            "GET  /api/s104/json":          "?lon=&lat=&date= → JSON preview",
            "GET  /api/s104/metadata":      "S-100/S-104 compliance info",
            "GET  /api/s104/validate":      "?path= → validasi HDF5",
        },
        "docs": "/api/openapi",
    })


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Health Check
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    if predictor is None:
        return jsonify({
            "status":  "degraded",
            "version": "2.2.0",
            "message": "Database TPXO tidak tersedia. Commit data/tpxo_seribu.db ke repo.",
            "luwes_scheduler": "running",
            "auth_db": "ok",
        }), 200

    try:
        cursor = predictor.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM grid_points")
        grid_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM harmonic_constants")
        harm_count = cursor.fetchone()[0]
        return jsonify({
            "status":             "healthy",
            "version":            "2.2.0",
            "grid_points":        grid_count,
            "harmonic_constants": harm_count,
            "s104_ready":         True,
            "standards": {
                "S-100": "Ed.5.2.0",
                "S-104": "Ed.2.0.0",
            },
        })
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: OpenAPI docs
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/openapi")
def openapi_spec():
    try:
        import yaml
        spec_path = Path(__file__).parent.parent / "openapi.yaml"
        if spec_path.exists():
            with open(spec_path, "r") as f:
                spec = yaml.safe_load(f)
            return jsonify(spec)
        return jsonify({"error": "openapi.yaml not found"}), 404
    except ImportError:
        return jsonify({
            "info": "OpenAPI spec tersedia di backend/openapi.yaml",
            "hint": "pip install pyyaml"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Tide Prediction
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/tide/prediction")
def get_tide_prediction():
    if predictor is None:
        return jsonify({
            "error": "Database TPXO tidak tersedia. Hubungi administrator."
        }), 503

    try:
        lon = request.args.get("lon", type=float)
        lat = request.args.get("lat", type=float)

        if lon is None:
            return jsonify({"error": "Parameter lon diperlukan"}), 400
        if lat is None:
            return jsonify({"error": "Parameter lat diperlukan"}), 400
        if not (-180 <= lon <= 180):
            return jsonify({"error": "lon harus antara -180 dan 180"}), 400
        if not (-90 <= lat <= 90):
            return jsonify({"error": "lat harus antara -90 dan 90"}), 400

        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)

        start_date_str = request.args.get("start_date")
        if start_date_str:
            start_dt = _parse_date(start_date_str)
            if start_dt is None:
                return jsonify({"error": "Format start_date tidak valid (gunakan YYYY-MM-DD)"}), 400
        else:
            start_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        end_date_str = request.args.get("end_date")
        if end_date_str:
            end_dt = _parse_date(end_date_str)
            if end_dt is None:
                return jsonify({"error": "Format end_date tidak valid (gunakan YYYY-MM-DD)"}), 400
        else:
            end_dt = start_dt + timedelta(days=7)

        min_allowed = now_utc.replace(year=now_utc.year - 1, hour=0, minute=0, second=0, microsecond=0)
        max_allowed = now_utc.replace(year=now_utc.year + 2, hour=23, minute=59, second=59, microsecond=0)

        if start_dt < min_allowed:
            return jsonify({"error": f"start_date terlalu jauh ke belakang (min: {min_allowed.date()})"}), 400
        if end_dt > max_allowed:
            return jsonify({"error": f"end_date terlalu jauh ke depan (maks: {max_allowed.date()})"}), 400
        if end_dt <= start_dt:
            return jsonify({"error": "end_date harus setelah start_date"}), 400
        if (end_dt - start_dt).days > 366:
            return jsonify({"error": "Rentang maksimum 366 hari per request"}), 400

        interval_hours = request.args.get("interval_hours", default=1, type=int)
        if interval_hours not in (1, 3, 6):
            return jsonify({"error": "interval_hours harus 1, 3, atau 6"}), 400

        result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=interval_hours,
        )
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Tide prediction error: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
def _parse_date(s: str):
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Error Handlers
# ─────────────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint tidak ditemukan"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method tidak diizinkan"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    print(f"\n🚀  Server berjalan di http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)