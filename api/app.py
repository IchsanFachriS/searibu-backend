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
# ── Config ── (GANTI bagian ini)
DATA_DIR       = os.getenv("DATA_DIR", "/data")
DB_PATH        = os.getenv("DATABASE_PATH",   f"{DATA_DIR}/tpxo_seribu.db")
LUWES_DB_PATH  = os.getenv("LUWES_DB_PATH",   f"{DATA_DIR}/luwes_raw.db")
AUTH_DB_PATH   = os.getenv("AUTH_DB_PATH",    f"{DATA_DIR}/auth.db")
LUWES_IMEI     = os.getenv("LUWES_IMEI",      "869556066101370")

# Pastikan /data directory ada (Railway Volume kadang butuh ini)
os.makedirs(DATA_DIR, exist_ok=True)

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
except Exception as e:
    print(f"  ❌  Gagal koneksi database TPXO: {e}")
    raise

# ── Setup S-104 (butuh predictor sudah connect) ───────────────────────────────
setup_s104(predictor, LUWES_DB_PATH, LUWES_IMEI)
print("  ✅  S-104 exporter siap (IHO S-104 Ed.2.0.0)")
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
        "standards": {
            "S-100": "IHO Universal Hydrographic Data Model Ed.5.2.0",
            "S-104": "Water Level Information for Surface Navigation Ed.2.0.0",
        },
        "endpoints": {
            # ── System ──────────────────────────────────────────────────
            "GET  /api/health":
                "Health check — status DB dan jumlah grid/harmonik",

            # ── Tidal Prediction ─────────────────────────────────────────
            "GET  /api/tide/prediction":
                "?lon=&lat=&start_date=&end_date=&interval_hours= → prediksi pasut TPXO9",

            # ── Luwes ────────────────────────────────────────────────────
            "GET  /api/luwes/level":
                "Data water level terbaru dari stasiun Luwes",
            "GET  /api/luwes/history":
                "?start=YYYY-MM-DD&end=YYYY-MM-DD → history water level",
            "GET  /api/luwes/status":
                "Status scheduler + statistik database Luwes",
            "POST /api/luwes/fetch":
                "Trigger manual fetch dari Luwes API",
            "GET  /api/luwes/overlay":
                "?date=&lon=&lat= → Luwes RAW + prediksi TPXO overlay",

            # ── Auth ──────────────────────────────────────────────────────
            "POST /api/auth/register":
                "{ full_name, email, password } → registrasi user baru",
            "POST /api/auth/login":
                "{ email, password } → login",

            # ── IHO S-104 ────────────────────────────────────────────────
            "GET  /api/s104/export":
                "?lon=&lat=&date= → unduh HDF5 S-104 Ed.2.0.0 (TPXO, dataDynamicity=1)",
            "GET  /api/s104/export/luwes":
                "?date=&apply_tol= → unduh HDF5 S-104 (Luwes, dataDynamicity=3)",
            "GET  /api/s104/json":
                "?lon=&lat=&date= → preview JSON S-104 untuk frontend",
            "GET  /api/s104/metadata":
                "Informasi compliance S-100/S-104 sistem Searibu",
            "GET  /api/s104/validate":
                "?path= → validasi struktur file HDF5 S-104",
        },
        "docs": "/api/openapi",
    })


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Health Check
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    try:
        cursor = predictor.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM grid_points")
        grid_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM harmonic_constants")
        harm_count = cursor.fetchone()[0]
        return jsonify({
            "status":              "healthy",
            "version":             "2.2.0",
            "grid_points":         grid_count,
            "harmonic_constants":  harm_count,
            "s104_ready":          True,
            "standards": {
                "S-100": "Ed.5.2.0",
                "S-104": "Ed.2.0.0",
            },
        })
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: OpenAPI docs endpoint (serve openapi.yaml as JSON)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/openapi")
def openapi_spec():
    """Serve OpenAPI 3.0 specification."""
    try:
        import yaml
        spec_path = Path(__file__).parent.parent / "openapi.yaml"
        if spec_path.exists():
            with open(spec_path, "r") as f:
                spec = yaml.safe_load(f)
            return jsonify(spec)
        else:
            return jsonify({"error": "openapi.yaml not found"}), 404
    except ImportError:
        # PyYAML tidak tersedia — kembalikan info saja
        return jsonify({
            "info": "OpenAPI 3.0 spec tersedia di backend/openapi.yaml",
            "hint": "Install PyYAML: pip install pyyaml"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Tide Prediction
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/tide/prediction")
def get_tide_prediction():
    """
    Prediksi pasang surut TPXO9-atlas-v5 (15 konstituen harmonik).

    Query params:
      lon            : longitude WGS84 (wajib)
      lat            : latitude WGS84 (wajib)
      start_date     : YYYY-MM-DD (default: hari ini UTC)
      end_date       : YYYY-MM-DD (default: start + 7 hari)
      interval_hours : 1 | 3 | 6 (default: 1)

    Datum vertikal output: MSL (Mean Sea Level) — sesuai S-104 §8.4
    """
    try:
        # ── Parse parameters ───────────────────────────────────────────
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

        now_utc = datetime.now(timezone.utc)

        # start_date
        start_date_str = request.args.get("start_date")
        if start_date_str:
            start_dt = _parse_date(start_date_str)
            if start_dt is None:
                return jsonify({"error": "Format start_date tidak valid (gunakan YYYY-MM-DD)"}), 400
        else:
            start_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        # end_date
        from datetime import timedelta
        end_date_str = request.args.get("end_date")
        if end_date_str:
            end_dt = _parse_date(end_date_str)
            if end_dt is None:
                return jsonify({"error": "Format end_date tidak valid (gunakan YYYY-MM-DD)"}), 400
        else:
            end_dt = start_dt + timedelta(days=7)

        # Batas waktu
        min_allowed = now_utc.replace(
            year=now_utc.year - 1,
            hour=0, minute=0, second=0, microsecond=0,
        )
        max_allowed = now_utc.replace(
            year=now_utc.year + 2,
            hour=23, minute=59, second=59, microsecond=0,
        )

        if start_dt < min_allowed:
            return jsonify({"error": f"start_date terlalu jauh ke belakang (min: {min_allowed.date()})"}), 400
        if end_dt > max_allowed:
            return jsonify({"error": f"end_date terlalu jauh ke depan (maks: {max_allowed.date()})"}), 400
        if end_dt <= start_dt:
            return jsonify({"error": "end_date harus setelah start_date"}), 400
        if (end_dt - start_dt).days > 366:
            return jsonify({"error": "Rentang maksimum 366 hari per request"}), 400

        # interval_hours
        interval_hours = request.args.get("interval_hours", default=1, type=int)
        if interval_hours not in (1, 3, 6):
            return jsonify({"error": "interval_hours harus 1, 3, atau 6"}), 400

        # ── Execute prediction ─────────────────────────────────────────
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
    """Parse string tanggal ke datetime UTC."""
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
    print(f"📄  OpenAPI spec  : http://localhost:{port}/api/openapi")
    print(f"📦  S-104 export  : http://localhost:{port}/api/s104/export?lon=106.58&lat=-5.60&date=2026-03-18\n")
    app.run(host="0.0.0.0", port=port, debug=debug)