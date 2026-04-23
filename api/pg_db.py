"""
pg_db.py — PostgreSQL connection layer (Supabase)

Menggantikan seluruh SQLite connection di:
  - auth_db.py       → tabel users
  - luwes_db.py      → tabel water_level_observations, fetch_log
  - billing_db.py    → tabel subscriptions, payments

Menggunakan psycopg2 dengan connection pooling via psycopg2.pool.
DATABASE_URL disediakan oleh environment variable (Supabase / Railway).

Format DATABASE_URL:
  postgresql://[user]:[password]@[host]:[port]/[dbname]?sslmode=require

Supabase connection string format (Session pooler port 5432):
  postgresql://postgres.[project-ref]:[password]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
"""

import os
import logging
import threading
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool

logger = logging.getLogger(__name__)

# ── Connection pool (singleton) ───────────────────────────────
_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_database_url() -> str:
    """Ambil DATABASE_URL dari environment, raise jika tidak ada."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable tidak ditemukan. "
            "Set di Railway / Supabase / .env"
        )
    # Supabase kadang pakai prefix 'postgres://' — psycopg2 butuh 'postgresql://'
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def init_pool(min_conn: int = 1, max_conn: int = 10):
    """
    Inisialisasi connection pool.
    Dipanggil sekali saat startup app.py.
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        db_url = _get_database_url()
        _pool = pool.ThreadedConnectionPool(
            min_conn, max_conn,
            dsn=db_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info(f"[PG] Connection pool dibuat (min={min_conn}, max={max_conn})")


def get_pool() -> pool.ThreadedConnectionPool:
    if _pool is None:
        init_pool()
    return _pool


@contextmanager
def get_conn():
    """
    Context manager untuk mendapatkan koneksi dari pool.
    Otomatis commit jika tidak ada error, rollback jika ada.
    Selalu kembalikan koneksi ke pool setelah selesai.

    Contoh:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


@contextmanager
def get_cursor():
    """
    Context manager yang langsung memberikan cursor.
    Lebih praktis untuk operasi sederhana.

    Contoh:
        with get_cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            yield cur


def execute_one(query: str, params=None):
    """Jalankan query, kembalikan satu baris (dict) atau None."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def execute_all(query: str, params=None):
    """Jalankan query, kembalikan semua baris (list of dict)."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def execute_write(query: str, params=None) -> int:
    """
    Jalankan INSERT/UPDATE/DELETE.
    Kembalikan rowcount.
    """
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount


def execute_returning(query: str, params=None):
    """
    Jalankan INSERT ... RETURNING atau UPDATE ... RETURNING.
    Kembalikan baris pertama.
    """
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def close_pool():
    """Tutup semua koneksi di pool. Dipanggil saat shutdown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("[PG] Connection pool ditutup")