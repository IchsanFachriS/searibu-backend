"""Background scheduler for periodic Luwes telemetry fetching.

Runs a single daemon thread that polls the Luwes API every
FETCH_INTERVAL_SECONDS seconds and persists any new observations.
Historical records accumulate indefinitely; duplicates are silently
ignored.
"""

import threading
import logging
from datetime import datetime, timezone, timedelta

from .luwes_service import fetch_and_store, LUWES_IMEI, LuwesAPIError
from .luwes_db import get_stats

WIB = timezone(timedelta(hours=7))
logger = logging.getLogger(__name__)

FETCH_INTERVAL_SECONDS = 60

_stop_event = threading.Event()
_fetch_thread: threading.Thread | None = None
_db_path: str | None = None
_imei: str | None = None

_stats: dict = {
    "total_fetches": 0,
    "new_records": 0,
    "duplicates": 0,
    "errors": 0,
    "last_fetch_time": None,
    "last_new_rec_time": None,
    "last_error": None,
}
_stats_lock = threading.Lock()


def _inc(key: str) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + 1


def _set(key: str, value) -> None:
    with _stats_lock:
        _stats[key] = value


def _fetch_loop(imei: str, db_path: str) -> None:
    logger.info("Luwes fetch thread started (interval=%ds, IMEI=%s)", FETCH_INTERVAL_SECONDS, imei)

    while not _stop_event.is_set():
        now_wib = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        _set("last_fetch_time", now_wib)
        _inc("total_fetches")

        try:
            result = fetch_and_store(imei)
            if result["is_new"]:
                _inc("new_records")
                _set("last_new_rec_time", now_wib)
                logger.info(
                    "NEW rec=%s level=%.3fm recorded_at=%s",
                    result["obs"].get("rec"),
                    result["obs"].get("level_m", 0),
                    result["obs"].get("recorded_at"),
                )
            else:
                _inc("duplicates")
                logger.debug("DUP rec=%s", result["obs"].get("rec"))

        except LuwesAPIError as exc:
            _inc("errors")
            _set("last_error", str(exc))
            logger.warning("Luwes API error: %s", exc)
        except Exception as exc:
            _inc("errors")
            _set("last_error", str(exc))
            logger.error("Unexpected fetch error: %s", exc, exc_info=True)

        _stop_event.wait(timeout=FETCH_INTERVAL_SECONDS)

    logger.info("Luwes fetch thread stopped")


def start_scheduler(db_path: str, imei: str = LUWES_IMEI) -> None:
    """Start the background fetch thread.

    Should be called once from app.py at startup.
    """
    global _fetch_thread, _stop_event, _db_path, _imei

    if _fetch_thread and _fetch_thread.is_alive():
        logger.warning("Scheduler already running — skipping start")
        return

    _db_path = db_path
    _imei = imei
    _stop_event.clear()
    _fetch_thread = threading.Thread(
        target=_fetch_loop,
        args=(imei, db_path),
        daemon=True,
        name="LuwesFetchThread",
    )
    _fetch_thread.start()
    logger.info("Luwes scheduler started (interval=%ds)", FETCH_INTERVAL_SECONDS)


def stop_scheduler() -> None:
    """Stop the background fetch thread gracefully."""
    _stop_event.set()
    if _fetch_thread:
        _fetch_thread.join(timeout=10)
    logger.info("Luwes scheduler stopped")


def get_scheduler_status() -> dict:
    """Return current scheduler state and database statistics."""
    with _stats_lock:
        stats_copy = dict(_stats)

    db_stats = {}
    if _db_path is not None and _imei:
        try:
            db_stats = get_stats(_db_path, _imei)
        except Exception as exc:
            db_stats = {"error": str(exc)}

    return {
        "scheduler": {
            "running": _fetch_thread is not None and _fetch_thread.is_alive(),
            "imei": _imei,
            "fetch_interval_secs": FETCH_INTERVAL_SECONDS,
        },
        "counters": stats_copy,
        "db": db_stats,
    }


def trigger_fetch_now(imei: str = LUWES_IMEI) -> dict:
    """Force an immediate fetch outside the normal interval.

    Returns the result from fetch_and_store, or an error dict.
    """
    if _db_path is None:
        return {"error": "Scheduler not yet initialised"}
    try:
        return fetch_and_store(imei)
    except LuwesAPIError as exc:
        return {"error": str(exc)}