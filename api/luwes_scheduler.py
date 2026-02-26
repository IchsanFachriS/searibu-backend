"""
Luwes Scheduler — Background thread untuk fetch data Luwes secara periodik.

Strategi:
  - Satu FetchThread berjalan terus-menerus sebagai daemon
  - Fetch setiap FETCH_INTERVAL detik
  - Setiap data baru (rec baru) disimpan ke DB
  - Data lama TIDAK dihapus → terakumulasi sebagai history lengkap
  - Jika data duplikat (rec sama), diabaikan dan tidak error

Karena API Luwes hanya menyediakan endpoint /last (data terbaru saja),
tidak ada cara untuk backfill data historis yang sudah lewat.
Data akan terakumulasi sejak scheduler pertama kali dijalankan.
"""

import threading
import logging
from datetime import datetime, timezone, timedelta

from .luwes_service import fetch_and_store, LUWES_IMEI, LuwesAPIError
from .luwes_db import get_stats

WIB = timezone(timedelta(hours=7))
logger = logging.getLogger(__name__)

# ── Interval fetch (detik) ────────────────────────────────────
FETCH_INTERVAL_SECONDS = 60   # fetch tiap 1 menit

# ── Internal state ────────────────────────────────────────────
_stop_event   = threading.Event()
_fetch_thread: threading.Thread | None = None

_db_path: str | None = None
_imei:    str | None = None

# Counter untuk monitoring
_stats = {
    "total_fetches":     0,
    "new_records":       0,
    "duplicates":        0,
    "errors":            0,
    "last_fetch_time":   None,
    "last_new_rec_time": None,
    "last_error":        None,
}
_stats_lock = threading.Lock()


def _update_stats(key: str, value=None, increment: bool = False):
    with _stats_lock:
        if increment:
            _stats[key] = _stats.get(key, 0) + 1
        else:
            _stats[key] = value


# ══════════════════════════════════════════════════════════════
# FETCH LOOP
# ══════════════════════════════════════════════════════════════

def _fetch_loop(imei: str, db_path: str):
    """
    Main loop untuk FetchThread.
    Berjalan terus sampai _stop_event di-set.
    """
    logger.info(
        f"[Luwes Scheduler] FetchThread mulai — "
        f"interval={FETCH_INTERVAL_SECONDS}s, IMEI={imei}"
    )

    while not _stop_event.is_set():
        now_wib = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        _update_stats("last_fetch_time", now_wib)
        _update_stats("total_fetches", increment=True)

        try:
            result = fetch_and_store(imei)
            obs    = result["obs"]
            is_new = result["is_new"]

            if is_new:
                _update_stats("new_records", increment=True)
                _update_stats("last_new_rec_time", now_wib)
                logger.info(
                    f"[Fetch] NEW | rec={obs.get('rec')} | "
                    f"level={obs.get('level_m')}m | "
                    f"recorded_at={obs.get('recorded_at')}"
                )
            else:
                _update_stats("duplicates", increment=True)
                logger.debug(
                    f"[Fetch] DUP | rec={obs.get('rec')} | "
                    f"level={obs.get('level_m')}m"
                )

        except LuwesAPIError as exc:
            _update_stats("errors", increment=True)
            _update_stats("last_error", str(exc))
            logger.warning(f"[Fetch] API error: {exc}")

        except Exception as exc:
            _update_stats("errors", increment=True)
            _update_stats("last_error", str(exc))
            logger.error(f"[Fetch] Unexpected error: {exc}", exc_info=True)

        # Tunggu interval berikutnya (interruptible)
        _stop_event.wait(timeout=FETCH_INTERVAL_SECONDS)

    logger.info("[Luwes Scheduler] FetchThread berhenti.")


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

def start_scheduler(db_path: str, imei: str = LUWES_IMEI):
    """
    Mulai FetchThread.
    Panggil sekali dari app.py saat startup.

    Args:
        db_path : path ke luwes_raw.db
        imei    : IMEI stasiun yang dipantau
    """
    global _fetch_thread, _stop_event, _db_path, _imei

    if _fetch_thread and _fetch_thread.is_alive():
        logger.warning("[Luwes Scheduler] Sudah berjalan, skip start.")
        return

    _db_path = db_path
    _imei    = imei
    _stop_event.clear()

    _fetch_thread = threading.Thread(
        target=_fetch_loop,
        args=(imei, db_path),
        daemon=True,
        name="LuwesFetchThread",
    )
    _fetch_thread.start()
    logger.info(
        f"[Luwes Scheduler] FetchThread dimulai — "
        f"interval={FETCH_INTERVAL_SECONDS}s"
    )


def stop_scheduler():
    """
    Hentikan FetchThread secara graceful.
    Berguna untuk testing atau graceful shutdown.
    """
    _stop_event.set()
    if _fetch_thread:
        _fetch_thread.join(timeout=10)
    logger.info("[Luwes Scheduler] Dihentikan.")


def get_scheduler_status() -> dict:
    """
    Ambil status dan statistik scheduler untuk monitoring.
    
    Return:
        dict dengan info thread, counter, dan db stats
    """
    with _stats_lock:
        stats_copy = dict(_stats)

    is_running = _fetch_thread is not None and _fetch_thread.is_alive()

    db_stats = {}
    if _db_path and _imei:
        try:
            db_stats = get_stats(_db_path, _imei)
        except Exception as exc:
            db_stats = {"error": str(exc)}

    return {
        "scheduler": {
            "running":              is_running,
            "imei":                 _imei,
            "fetch_interval_secs":  FETCH_INTERVAL_SECONDS,
        },
        "counters": stats_copy,
        "db": db_stats,
    }


def trigger_fetch_now(imei: str = LUWES_IMEI) -> dict:
    """
    Paksa fetch sekarang (tanpa menunggu interval).
    Berguna untuk testing atau endpoint admin.
    
    Return: result dari fetch_and_store
    """
    if not _db_path:
        return {"error": "Scheduler belum diinisialisasi"}
    try:
        return fetch_and_store(imei)
    except LuwesAPIError as exc:
        return {"error": str(exc)}