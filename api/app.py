"""
TPXO Tide Prediction API — Flask
Endpoint utama: GET /api/tide/prediction
Endpoint Luwes: GET /api/luwes/level, /history, /status, POST /fetch
"""

import os
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tpxo_predictor import TPXOPredictor
from api.luwes_routes import luwes_bp
from api.luwes_service import setup_luwes
from api.luwes_db import init_db
from api.luwes_scheduler import start_scheduler

app = Flask(__name__)

# ── CORS ─────────────────────────────────────────────────────
_raw_origins = os.getenv('CORS_ORIGINS', 'http://localhost:5173,http://localhost:3000')
cors_origins = [o.strip() for o in _raw_origins.split(',') if o.strip()]
CORS(app, resources={r"/api/*": {"origins": cors_origins, "methods": ["GET", "POST", "OPTIONS"]}})

app.register_blueprint(luwes_bp)

# ── Config ────────────────────────────────────────────────────
DB_PATH       = os.getenv('DATABASE_PATH', 'data/tpxo_seribu.db')
LUWES_DB_PATH = os.getenv('LUWES_DB_PATH', 'data/luwes_raw.db')

# ── Init Luwes DB ─────────────────────────────────────────────
init_db(LUWES_DB_PATH)
setup_luwes(LUWES_DB_PATH)

print("=" * 60)
print("  TPXO Tide Prediction API")
print(f"  TPXO Database : {DB_PATH}")
print(f"  Luwes DB      : {LUWES_DB_PATH}")
print(f"  CORS Origins  : {cors_origins}")

# ── Scheduler ─────────────────────────────────────────────────
_werkzeug_reloader_child = os.environ.get('WERKZEUG_RUN_MAIN') == 'false'
if not _werkzeug_reloader_child:
    start_scheduler(db_path=LUWES_DB_PATH)
    print(f"  Luwes Scheduler : aktif (fetch setiap 60s)")

# ── TPXO Predictor ───────────────────────────────────────────
predictor = TPXOPredictor(DB_PATH)
try:
    predictor.connect()
    print("  ✅ Database TPXO terkoneksi")
except Exception as e:
    print(f"  ❌ Gagal koneksi database TPXO: {e}")
    raise

print("=" * 60)


@app.route('/')
def index():
    return jsonify({
        'name': 'TPXO Tide Prediction API',
        'version': '2.1.0',
        'model': 'TPXO9-atlas-v5',
        'endpoints': {
            '/api/health':           'Health check',
            '/api/tide/prediction':  'GET lon, lat, start_date, end_date, [interval_hours]',
            '/api/luwes/level':      'GET water level terbaru',
            '/api/luwes/history':    'GET history harian',
            '/api/luwes/status':     'GET scheduler status',
            '/api/luwes/fetch':      'POST manual trigger fetch',
        }
    })


@app.route('/api/health')
def health():
    try:
        cursor = predictor.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM grid_points')
        grid_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM harmonic_constants')
        harm_count = cursor.fetchone()[0]
        return jsonify({
            'status': 'healthy',
            'grid_points': grid_count,
            'harmonic_constants': harm_count,
        })
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


@app.route('/api/tide/prediction')
def get_tide_prediction():
    try:
        lon = request.args.get('lon', type=float)
        lat = request.args.get('lat', type=float)
        if lon is None:
            return jsonify({'error': 'Parameter lon diperlukan'}), 400
        if lat is None:
            return jsonify({'error': 'Parameter lat diperlukan'}), 400
        if not (-180 <= lon <= 180):
            return jsonify({'error': 'lon harus antara -180 dan 180'}), 400
        if not (-90 <= lat <= 90):
            return jsonify({'error': 'lat harus antara -90 dan 90'}), 400

        now_utc = datetime.now(timezone.utc)
        start_date_str = request.args.get('start_date')
        if start_date_str:
            start_dt = _parse_date(start_date_str)
            if start_dt is None:
                return jsonify({'error': 'Format start_date tidak valid (gunakan YYYY-MM-DD)'}), 400
        else:
            start_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        end_date_str = request.args.get('end_date')
        if end_date_str:
            end_dt = _parse_date(end_date_str)
            if end_dt is None:
                return jsonify({'error': 'Format end_date tidak valid (gunakan YYYY-MM-DD)'}), 400
        else:
            from datetime import timedelta
            end_dt = start_dt + timedelta(days=7)

        min_allowed = now_utc.replace(year=now_utc.year - 1,
                                      hour=0, minute=0, second=0, microsecond=0)
        max_allowed = now_utc.replace(year=now_utc.year + 2,
                                      hour=23, minute=59, second=59, microsecond=0)

        if start_dt < min_allowed:
            return jsonify({'error': f'start_date terlalu jauh ke belakang (min: {min_allowed.date()})'}), 400
        if end_dt > max_allowed:
            return jsonify({'error': f'end_date terlalu jauh ke depan (maks: {max_allowed.date()})'}), 400
        if end_dt <= start_dt:
            return jsonify({'error': 'end_date harus setelah start_date'}), 400

        from datetime import timedelta
        if (end_dt - start_dt).days > 366:
            return jsonify({'error': 'Rentang maksimum 366 hari per request'}), 400

        interval_hours = request.args.get('interval_hours', default=1, type=int)
        if interval_hours not in (1, 3, 6):
            return jsonify({'error': 'interval_hours harus 1, 3, atau 6'}), 400

        result = predictor.predict(
            lon=lon, lat=lat,
            start_dt=start_dt, end_dt=end_dt,
            interval_hours=interval_hours,
        )
        return jsonify(result)

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.error(f"Error prediksi pasut: {e}", exc_info=True)
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500


def _parse_date(s: str):
    for fmt in ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S']:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint tidak ditemukan'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    print(f"\n🚀 Server berjalan di http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=debug)