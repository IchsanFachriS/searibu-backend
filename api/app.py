"""Searibu Marine Information API — application entry point."""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

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
from api.profile_routes  import profile_bp
from api.admin_routes    import admin_bp          # NEW
from api.pg_db           import init_pool, close_pool

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

app = Flask(__name__)

_raw_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,https://searibu.vercel.app",
)
cors_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
CORS(
    app,
    resources={r"/api/*": {
        "origins":      cors_origins,
        "methods":      ["GET", "POST", "PUT", "OPTIONS"],
        "allow_headers":["Content-Type", "Authorization"],
    }},
)

app.register_blueprint(luwes_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(s104_bp)
app.register_blueprint(billing_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(admin_bp)                  # NEW

_repo_data = Path(__file__).parent.parent / "data"
DB_PATH    = os.getenv("DATABASE_PATH", str(_repo_data / "tpxo_seribu.db"))
LUWES_IMEI = os.getenv("LUWES_IMEI", "869556066101370")

logger.info("Starting Searibu API v3.2.0")
logger.info("TPXO database : %s", DB_PATH)
logger.info("CORS origins  : %s", cors_origins)

try:
    init_pool(min_conn=1, max_conn=10)
    logger.info("PostgreSQL pool connected")
except Exception as exc:
    logger.error("PostgreSQL connection failed: %s", exc)

init_db()
setup_luwes("")
init_auth_db()
setup_auth()
setup_billing("")

predictor = None
try:
    predictor = TPXOPredictor(DB_PATH)
    predictor.connect()
    logger.info("TPXO database connected")
except FileNotFoundError:
    logger.error("TPXO database not found: %s", DB_PATH)
except Exception as exc:
    logger.error("TPXO database connection failed: %s", exc)

setup_s104(predictor, "", LUWES_IMEI)

_werkzeug_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "false"
if not _werkzeug_reloader_child:
    start_scheduler(db_path="", imei=LUWES_IMEI)
    logger.info("Luwes scheduler started")

import atexit

@atexit.register
def _shutdown():
    close_pool()
    logger.info("PostgreSQL pool closed on shutdown")


@app.route("/")
def index():
    return jsonify({
        "name":      "Searibu Marine Information API",
        "version":   "3.2.0",
        "model":     "TPXO9-atlas-v5",
        "database":  "PostgreSQL (Supabase)",
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
            "status":    "degraded",
            "version":   "3.2.0",
            "postgresql": pg_ok,
            "tpxo_db":   False,
            "message":   "TPXO database unavailable",
        }), 200

    try:
        cursor = predictor.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM grid_points")
        grid_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM harmonic_constants")
        harm_count = cursor.fetchone()[0]
        return jsonify({
            "status":             "healthy",
            "version":            "3.2.0",
            "postgresql":         pg_ok,
            "grid_points":        grid_count,
            "harmonic_constants": harm_count,
            "s104_ready":         True,
        })
    except Exception as exc:
        return jsonify({"status": "unhealthy", "error": str(exc)}), 500


def _parse_date(s: str):
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@app.route("/api/tide/prediction")
def get_tide_prediction():
    if predictor is None:
        return jsonify({"error": "TPXO database unavailable"}), 503

    try:
        lon = request.args.get("lon", type=float)
        lat = request.args.get("lat", type=float)
        if lon is None:
            return jsonify({"error": "Parameter lon is required"}), 400
        if lat is None:
            return jsonify({"error": "Parameter lat is required"}), 400
        if not (-180 <= lon <= 180):
            return jsonify({"error": "lon must be between -180 and 180"}), 400
        if not (-90 <= lat <= 90):
            return jsonify({"error": "lat must be between -90 and 90"}), 400

        now_utc = datetime.now(timezone.utc)

        start_date_str = request.args.get("start_date")
        start_dt = (
            _parse_date(start_date_str) if start_date_str
            else now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        if start_date_str and start_dt is None:
            return jsonify({"error": "Invalid start_date format"}), 400

        end_date_str = request.args.get("end_date")
        end_dt = (
            _parse_date(end_date_str) if end_date_str
            else start_dt + __import__("datetime").timedelta(days=7)
        )
        if end_date_str and end_dt is None:
            return jsonify({"error": "Invalid end_date format"}), 400

        if end_dt <= start_dt:
            return jsonify({"error": "end_date must be after start_date"}), 400

        interval_minutes = request.args.get("interval_minutes", type=int)
        interval_hours   = request.args.get("interval_hours", default=1, type=int)

        if interval_minutes is not None:
            if not (1 <= interval_minutes <= 60):
                return jsonify({"error": "interval_minutes must be 1-60"}), 400
        elif interval_hours not in (1, 3, 6):
            return jsonify({"error": "interval_hours must be 1, 3, or 6"}), 400

        return jsonify(predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
        ))

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.error("Tide prediction error: %s", exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(_):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    logger.info("Running on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)