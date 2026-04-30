"""PostgreSQL connection layer using psycopg2 thread-safe connection pool.

Serves as the single database access point for all modules:
- auth_db.py      → users table
- luwes_db.py     → water_level_observations, fetch_log tables
- billing_db.py   → subscriptions, payments tables

DATABASE_URL format (Supabase session pooler):
    postgresql://postgres.[ref]:[password]@[host]:5432/postgres?sslmode=require
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

_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Configure it in Railway / Supabase / .env"
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def init_pool(min_conn: int = 1, max_conn: int = 10) -> None:
    """Initialise the connection pool. Call once at application startup."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        _pool = pool.ThreadedConnectionPool(
            min_conn,
            max_conn,
            dsn=_get_database_url(),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        logger.info("PostgreSQL connection pool initialised (min=%d, max=%d)", min_conn, max_conn)


def get_pool() -> pool.ThreadedConnectionPool:
    if _pool is None:
        init_pool()
    return _pool


@contextmanager
def get_conn():
    """Yield a connection from the pool, committing or rolling back on exit."""
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
    """Yield a cursor from a pooled connection."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            yield cur


def execute_one(query: str, params=None):
    """Execute a query and return the first row as a dict, or None."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def execute_all(query: str, params=None):
    """Execute a query and return all rows as a list of dicts."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def execute_write(query: str, params=None) -> int:
    """Execute an INSERT / UPDATE / DELETE and return the row count."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount


def execute_returning(query: str, params=None):
    """Execute an INSERT/UPDATE … RETURNING statement and return the first row."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def close_pool() -> None:
    """Close all connections in the pool. Call on application shutdown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL connection pool closed")