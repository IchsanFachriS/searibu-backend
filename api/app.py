"""
TPXO Tide Prediction API — Flask  v2.4.0
Backend utama sistem Searibu

Perubahan v2.4.0 (satu-satunya perubahan dari v2.3.0):
  Endpoint /api/tide/prediction/minute sekarang mengembalikan prediksi
  yang selaras dengan hari WIB (UTC+7).

  SEBELUMNYA (v2.3.0):
    date_str → start_dt = date 00:00:00 UTC  → WIB 07:00 hari itu
               end_dt   = start_dt + 23h59m  → WIB 06:59 hari berikutnya
    Masalah: WIB 00:00-06:59 hari ini = UTC 17:00-23:59 hari sebelumnya
             TIDAK ada dalam satu request. Frontend harus fetch 2 request
             dari hari berbeda → dua epoch harmonik berbeda → DISKONTINUITAS
             fisik di titik junction ~07:00 WIB → grafik patah/kink.

  SEKARANG (v2.4.0):
    date_str → start_dt = (date-1) 17:00:00 UTC  = date 00:00:00 WIB
               end_dt   = date     16:59:00 UTC  = date 23:59:00 WIB
    Satu request tunggal → satu epoch harmonik → 1440 titik
    fisik kontinu sempurna dari WIB 00:00 sampai 23:59.

  Frontend hanya perlu satu fetch, tidak perlu merge, tidak ada junction.

Kenapa 26 Maret sebelumnya mulus?
  Cache v2.7.0 (single-fetch, 1 epoch) masih tersimpan untuk tanggal itu.
  Semua tanggal lain cache-nya ditulis oleh v2.8.0/v2.9.0 (2-epoch merge)
  sehingga ada kink. Fix ini menghilangkan masalah di semua tanggal.

Endpoints (tidak berubah dari v2.3.0):
  GET  /
  GET  /api/health
  GET  /api/tide/prediction
  GET  /api/tide/prediction/minute  ← HANYA endpoint ini yang berubah
  GET  /api/luwes/level
  GET  /api/luwes/history
  GET  /api/luwes/status
  POST /api/luwes/fetch
  GET  /api/luwes/overlay
  POST /api/auth/register
  POST /api/auth/login
  GET  /api/s104/export
  GET  /api/s104/export/luwes
  GET  /api/s104/json
  GET  /api/s104/metadata
  GET  /api/s104/validate

Standar:
  IHO S-100 Universal Hydrographic Data Model Ed.5.2.0 (2024)
  IHO S-104 Water Level Information for Surface Navigation Ed.2.0.0 (2024)
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
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

# ── Timezone WIB ──────────────────────────────────────────────────────────────
WIB = timezone(timedelta(hours=7))

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
_repo_data = Path(__file__).parent.parent / "data"
DB_PATH = os.getenv("DATABASE_PATH", str(_repo_data / "tpxo_seribu.db"))

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
print("  Searibu — TPXO Tide Prediction API  v2.4.0")
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
    logger.error(f"Database TPXO tidak ditemukan: {e}")
    print(f"  ❌  Database TPXO tidak ditemukan: {DB_PATH}")
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
        "version": "2.4.0",
        "model":   "TPXO9-atlas-v5",
        "tpxo_ready": predictor is not None,
        "standards": {
            "S-100": "IHO Universal Hydrographic Data Model Ed.5.2.0",
            "S-104": "Water Level Information for Surface Navigation Ed.2.0.0",
        },
        "endpoints": {
            "GET  /api/health":                   "Health check",
            "GET  /api/tide/prediction":           "?lon=&lat=&start_date=&end_date=&interval_hours= | &interval_minutes=",
            "GET  /api/tide/prediction/minute":    "?lon=&lat=&date=YYYY-MM-DD → per-menit WIB-aligned (1440 titik, 00:00–23:59 WIB)",
            "GET  /api/luwes/level":               "Data water level terbaru",
            "GET  /api/luwes/history":             "?start=YYYY-MM-DD&end=YYYY-MM-DD",
            "GET  /api/luwes/status":              "Status scheduler + statistik DB",
            "POST /api/luwes/fetch":               "Trigger manual fetch",
            "GET  /api/luwes/overlay":             "?date=&lon=&lat=",
            "POST /api/auth/register":             "{ full_name, email, password }",
            "POST /api/auth/login":                "{ email, password }",
            "GET  /api/s104/export":               "?lon=&lat=&date= → HDF5 TPXO",
            "GET  /api/s104/export/luwes":         "?date= → HDF5 Luwes",
            "GET  /api/s104/json":                 "?lon=&lat=&date= → JSON preview",
            "GET  /api/s104/metadata":             "S-100/S-104 compliance info",
            "GET  /api/s104/validate":             "?path= → validasi HDF5",
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
            "version": "2.4.0",
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
            "version":            "2.4.0",
            "grid_points":        grid_count,
            "harmonic_constants": harm_count,
            "s104_ready":         True,
            "features": {
                "per_minute_prediction":       True,
                "wib_aligned_minute_endpoint": True,
                "max_range_years":             5,
            },
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
# HELPER: parse date string
# ─────────────────────────────────────────────────────────────────────────────
def _parse_date(s: str):
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: validasi lon/lat
# ─────────────────────────────────────────────────────────────────────────────
def _validate_lonlat(lon, lat):
    """Return (lon, lat, error_response_or_None)."""
    if lon is None:
        return None, None, ({"error": "Parameter lon diperlukan"}, 400)
    if lat is None:
        return None, None, ({"error": "Parameter lat diperlukan"}, 400)
    if not (-180 <= lon <= 180):
        return None, None, ({"error": "lon harus antara -180 dan 180"}, 400)
    if not (-90 <= lat <= 90):
        return None, None, ({"error": "lat harus antara -90 dan 90"}, 400)
    return lon, lat, None


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Tide Prediction (jam atau menit, hingga 5 tahun)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/tide/prediction")
def get_tide_prediction():
    """
    Prediksi pasut TPXO9.

    Parameter:
      lon           : longitude (float, wajib)
      lat           : latitude  (float, wajib)
      start_date    : YYYY-MM-DD (default hari ini)
      end_date      : YYYY-MM-DD (default 7 hari dari start)
      interval_hours: 1, 3, atau 6 (default 1) — diabaikan jika interval_minutes diset
      interval_minutes: 1–60 — jika diset, menggantikan interval_hours

    Batasan:
      - interval_hours  : rentang maks 5 tahun (1826 hari)
      - interval_minutes: rentang maks 1 hari (gunakan /minute untuk kemudahan)
    """
    if predictor is None:
        return jsonify({"error": "Database TPXO tidak tersedia. Hubungi administrator."}), 503

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

        interval_minutes = request.args.get("interval_minutes", type=int)
        interval_hours   = request.args.get("interval_hours", default=1, type=int)

        if interval_minutes is not None:
            if interval_minutes < 1 or interval_minutes > 60:
                return jsonify({"error": "interval_minutes harus antara 1 dan 60"}), 400
            max_days = 31
            if (end_dt - start_dt).days > max_days:
                return jsonify({
                    "error": f"Untuk interval menit, rentang maksimum {max_days} hari. "
                             f"Gunakan /api/tide/prediction/minute untuk 1 hari penuh."
                }), 400
        else:
            if interval_hours not in (1, 3, 6):
                return jsonify({"error": "interval_hours harus 1, 3, atau 6"}), 400
            max_days = 1826
            if (end_dt - start_dt).days > max_days:
                return jsonify({"error": f"Rentang maksimum {max_days} hari (~5 tahun) per request"}), 400

        if end_dt <= start_dt:
            return jsonify({"error": "end_date harus setelah start_date"}), 400

        min_allowed = now_utc.replace(year=now_utc.year - 10, hour=0, minute=0, second=0, microsecond=0)
        max_allowed = now_utc.replace(year=now_utc.year + 6, hour=23, minute=59, second=59, microsecond=0)
        if start_dt < min_allowed:
            return jsonify({"error": f"start_date terlalu jauh ke belakang (min: {min_allowed.date()})"}), 400
        if end_dt > max_allowed:
            return jsonify({"error": f"end_date terlalu jauh ke depan (maks: {max_allowed.date()})"}), 400

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


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: Tide Prediction — Per Menit, WIB-Aligned (v2.4.0)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/tide/prediction/minute")
def get_tide_prediction_minute():
    """
    Prediksi pasut per menit untuk 1 hari WIB penuh (1440 titik).
    Dioptimalkan untuk rendering grafik interaktif di frontend.

    Parameter:
      lon  : longitude (float, wajib)
      lat  : latitude  (float, wajib)
      date : YYYY-MM-DD sebagai hari WIB (default hari ini WIB)

    ════════════════════════════════════════════════════════════
    FIX v2.4.0 — WIB-Aligned window
    ════════════════════════════════════════════════════════════
    Masalah v2.3.0:
      start_dt = date 00:00:00 UTC  → WIB 07:00 hari itu
      end_dt   = date 23:59:00 UTC  → WIB 06:59 hari berikutnya
      
      WIB 00:00–06:59 hari ini = UTC 17:00–23:59 hari sebelumnya
      → tidak ada dalam satu request.
      
      Frontend harus merge dua request → dua epoch harmonik berbeda
      → diskontinuitas fisik di titik junction → grafik patah/kink.

    Solusi v2.4.0:
      start_dt = (date-1) 17:00:00 UTC  = date 00:00:00 WIB
      end_dt   = date     16:59:00 UTC  = date 23:59:00 WIB
      
      Satu request → satu epoch harmonik → 1440 titik kontinu.
      Frontend cukup 1 fetch, tidak perlu merge.

    Contoh date=2026-03-31:
      start_dt = 2026-03-30T17:00:00Z  = 2026-03-31T00:00 WIB  ✓
      end_dt   = 2026-03-31T16:59:00Z  = 2026-03-31T23:59 WIB  ✓

    Response timestamps tetap UTC (suffix Z), seperti sebelumnya.
    Frontend mengkonversi ke WIB (+7 jam) untuk display.
    ════════════════════════════════════════════════════════════
    """
    if predictor is None:
        return jsonify({"error": "Database TPXO tidak tersedia."}), 503

    try:
        lon = request.args.get("lon", type=float)
        lat = request.args.get("lat", type=float)
        lon, lat, err = _validate_lonlat(lon, lat)
        if err:
            return jsonify(err[0]), err[1]

        # Parse ?date= sebagai hari WIB
        date_str = request.args.get("date")
        if date_str:
            try:
                wib_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Format date tidak valid (gunakan YYYY-MM-DD)"}), 400
        else:
            # Default: hari ini dalam WIB
            wib_date = datetime.now(WIB).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).replace(tzinfo=None)

        # ── KUNCI FIX: hitung UTC window yang setara dengan WIB 00:00–23:59 ──
        #
        # WIB 00:00 = UTC (date - 1 hari) 17:00:00
        # WIB 23:59 = UTC date 16:59:00
        #
        # Implementasi: ambil midnight UTC dari wib_date, geser -7 jam
        # sehingga menghasilkan (wib_date - 1 hari) 17:00 UTC
        start_dt = datetime(
            wib_date.year, wib_date.month, wib_date.day,
            0, 0, 0,
            tzinfo=timezone.utc
        ) - timedelta(hours=7)
        # start_dt = (wib_date - 1 hari) T17:00:00Z = wib_date T00:00:00 WIB

        end_dt = start_dt + timedelta(hours=23, minutes=59)
        # end_dt = wib_date T16:59:00Z = wib_date T23:59:00 WIB

        result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt,
            end_dt=end_dt,
            interval_minutes=1,
        )

        # Tambah info timezone ke response agar frontend bisa verifikasi
        result["wib_info"] = {
            "wib_date":  wib_date.strftime("%Y-%m-%d"),
            "utc_start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "utc_end":   end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note":      "UTC start = WIB 00:00, UTC end = WIB 23:59. +7 untuk konversi ke WIB.",
        }

        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Tide minute prediction error: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


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