"""
api/app.py — TPXO Tide Prediction API  v3.0.0
Perubahan dari v2.4.0: migrasi dari SQLite ke PostgreSQL (Supabase)

Semua operasi database sekarang menggunakan psycopg2 via pg_db.py.
- auth.db     → PostgreSQL tabel users
- luwes_raw.db → PostgreSQL tabel water_level_observations, fetch_log
- billing.db   → PostgreSQL tabel subscriptions, payments
- tpxo_seribu.db → TETAP SQLite (read-only, tidak perlu migrasi)

Environment variables yang diperlukan:
  DATABASE_URL        → PostgreSQL connection string (Supabase)
  DATABASE_PATH       → Path ke tpxo_seribu.db (tetap SQLite)
  LUWES_IMEI          → IMEI stasiun Luwes
  MIDTRANS_SERVER_KEY → Midtrans server key
  CORS_ORIGINS        → Allowed CORS origins
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tpxo_predictor import TPXOPredictor
from api.luwes_routes    import luwes_bp
from api.luwes_service   import setup_luwes
from api.luwes_db        import init_db
from api.luwes_scheduler import start_scheduler
from api.auth_routes     import auth_bp, setup_auth
from api.auth_db         import init_auth_db
from api.s104_routes     import s104_bp, setup_s104
from api.billing_routes  import billing_bp, setup_billing

# ── PostgreSQL pool ───────────────────────────────────────────
from api.pg_db import init_pool, close_pool

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

app = Flask(__name__)

# ── CORS ──────────────────────────────────────────────────────
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

app.register_blueprint(luwes_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(s104_bp)
app.register_blueprint(billing_bp)

# ── Config ────────────────────────────────────────────────────
_repo_data  = Path(__file__).parent.parent / "data"
DB_PATH     = os.getenv("DATABASE_PATH", str(_repo_data / "tpxo_seribu.db"))
LUWES_IMEI  = os.getenv("LUWES_IMEI", "869556066101370")

print("=" * 65)
print("  Searibu — TPXO Tide Prediction API  v3.0.0")
print(f"  TPXO Database  : {DB_PATH}  (SQLite, read-only)")
print(f"  App Database   : PostgreSQL (Supabase) via DATABASE_URL")
print(f"  Luwes IMEI     : {LUWES_IMEI}")
print(f"  CORS Origins   : {cors_origins}")

# ── 1. Init PostgreSQL connection pool ────────────────────────
try:
    init_pool(min_conn=1, max_conn=10)
    print("  ✅  PostgreSQL pool terkoneksi")
except Exception as e:
    logger.error(f"Gagal koneksi PostgreSQL: {e}")
    print(f"  ❌  Gagal koneksi PostgreSQL: {e}")
    print("      Pastikan DATABASE_URL sudah diset dengan benar")

# ── 2. Init modules (sekarang no-op untuk DB, tapi tetap setup service) ──
init_db()               # no-op di PG mode
setup_luwes("")         # luwes_service masih butuh setup (koneksi pg_db)
init_auth_db()          # no-op di PG mode
setup_auth()            # no-op di PG mode

# ── 3. Billing setup ──────────────────────────────────────────
setup_billing("")       # billing_db.py sekarang pakai pg_db
print("  ✅  Billing module siap (PostgreSQL)")

# ── 4. Scheduler ─────────────────────────────────────────────
_werkzeug_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "false"
if not _werkzeug_reloader_child:
    start_scheduler(db_path="", imei=LUWES_IMEI)
    print("  ✅  Luwes Scheduler aktif (fetch tiap 60 detik)")

# ── 5. TPXO Predictor (tetap SQLite) ─────────────────────────
predictor = TPXOPredictor(DB_PATH)
try:
    predictor.connect()
    print("  ✅  Database TPXO (SQLite) terkoneksi")
except FileNotFoundError as e:
    logger.error(f"Database TPXO tidak ditemukan: {e}")
    print(f"  ❌  Database TPXO tidak ditemukan: {DB_PATH}")
    predictor = None
except Exception as e:
    logger.error(f"Gagal koneksi database TPXO: {e}", exc_info=True)
    print(f"  ❌  Gagal koneksi database TPXO: {e}")
    predictor = None

# ── 6. S-104 ──────────────────────────────────────────────────
if predictor is not None:
    setup_s104(predictor, "", LUWES_IMEI)
    print("  ✅  S-104 exporter siap (IHO S-104 Ed.2.0.0)")
else:
    setup_s104(None, "", LUWES_IMEI)
    print("  ⚠️   S-104 exporter nonaktif")

print("=" * 65)


# ── Graceful shutdown ─────────────────────────────────────────
import atexit
@atexit.register
def _shutdown():
    close_pool()
    logger.info("PostgreSQL pool ditutup saat shutdown")


# ── Routes (identik dengan v2.4.0) ───────────────────────────

@app.route("/")
def index():
    return jsonify({
        "name":    "Searibu Marine Information API",
        "version": "3.0.0",
        "model":   "TPXO9-atlas-v5",
        "database": "PostgreSQL (Supabase)",
        "tpxo_ready": predictor is not None,
        "standards": {
            "S-100": "IHO Universal Hydrographic Data Model Ed.5.2.0",
            "S-104": "Water Level Information for Surface Navigation Ed.2.0.0",
        },
    })


@app.route("/api/health")
def health():
    from api.pg_db import get_cursor as _gc
    pg_ok = False
    try:
        with _gc() as cur:
            cur.execute("SELECT 1")
        pg_ok = True
    except Exception:
        pass

    if predictor is None:
        return jsonify({
            "status":      "degraded",
            "version":     "3.0.0",
            "postgresql":  pg_ok,
            "tpxo_db":     False,
            "message":     "TPXO database tidak tersedia",
            "luwes_scheduler": "running",
        }), 200

    try:
        cursor = predictor.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM grid_points")
        grid_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM harmonic_constants")
        harm_count = cursor.fetchone()[0]
        return jsonify({
            "status":              "healthy",
            "version":             "3.0.0",
            "postgresql":          pg_ok,
            "grid_points":         grid_count,
            "harmonic_constants":  harm_count,
            "s104_ready":          True,
        })
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


def _parse_date(s: str):
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _validate_lonlat(lon, lat):
    if lon is None:
        return None, None, ({"error": "Parameter lon diperlukan"}, 400)
    if lat is None:
        return None, None, ({"error": "Parameter lat diperlukan"}, 400)
    if not (-180 <= lon <= 180):
        return None, None, ({"error": "lon harus antara -180 dan 180"}, 400)
    if not (-90 <= lat <= 90):
        return None, None, ({"error": "lat harus antara -90 dan 90"}, 400)
    return lon, lat, None


@app.route("/api/tide/prediction")
def get_tide_prediction():
    if predictor is None:
        return jsonify({"error": "Database TPXO tidak tersedia."}), 503

    try:
        lon = request.args.get("lon", type=float)
        lat = request.args.get("lat", type=float)
        lon, lat, err = _validate_lonlat(lon, lat)
        if err:
            return jsonify(err[0]), err[1]

        now_utc = datetime.now(timezone.utc)
        start_date_str = request.args.get("start_date")
        if start_date_str:
            start_dt = _parse_date(start_date_str)
            if start_dt is None:
                return jsonify({"error": "Format start_date tidak valid"}), 400
        else:
            start_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        end_date_str = request.args.get("end_date")
        if end_date_str:
            end_dt = _parse_date(end_date_str)
            if end_dt is None:
                return jsonify({"error": "Format end_date tidak valid"}), 400
        else:
            end_dt = start_dt + timedelta(days=7)

        interval_minutes = request.args.get("interval_minutes", type=int)
        interval_hours   = request.args.get("interval_hours", default=1, type=int)

        if interval_minutes is not None:
            if interval_minutes < 1 or interval_minutes > 60:
                return jsonify({"error": "interval_minutes harus 1–60"}), 400
        else:
            if interval_hours not in (1, 3, 6):
                return jsonify({"error": "interval_hours harus 1, 3, atau 6"}), 400

        if end_dt <= start_dt:
            return jsonify({"error": "end_date harus setelah start_date"}), 400

        result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
        )
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Tide prediction error: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@app.route("/api/tide/prediction/minute")
def get_tide_prediction_minute():
    if predictor is None:
        return jsonify({"error": "Database TPXO tidak tersedia."}), 503

    try:
        lon = request.args.get("lon", type=float)
        lat = request.args.get("lat", type=float)
        lon, lat, err = _validate_lonlat(lon, lat)
        if err:
            return jsonify(err[0]), err[1]

        date_str = request.args.get("date")
        if date_str:
            try:
                wib_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Format date tidak valid (YYYY-MM-DD)"}), 400
        else:
            wib_date = datetime.now(WIB).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).replace(tzinfo=None)

        start_dt = datetime(
            wib_date.year, wib_date.month, wib_date.day, 0, 0, 0, tzinfo=timezone.utc
        ) - timedelta(hours=7)
        end_dt = start_dt + timedelta(hours=23, minutes=59)

        result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_minutes=1,
        )
        result["wib_info"] = {
            "wib_date":  wib_date.strftime("%Y-%m-%d"),
            "utc_start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "utc_end":   end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Tide minute prediction error: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint tidak ditemukan"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method tidak diizinkan"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    print(f"\n🚀  Server berjalan di http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)