"""Migrate data from SQLite databases to PostgreSQL (Supabase).

Run once from a local machine that has access to both databases:
    1. Copy auth.db, luwes_raw.db, billing.db from the server to this machine.
    2. Set DATABASE_URL in the environment (or .env file).
    3. python scripts/migrate_sqlite_to_pg.py

Environment variables:
    DATABASE_URL     PostgreSQL connection string (Supabase)
    AUTH_DB_PATH     Path to auth.db       (default: data/auth.db)
    LUWES_DB_PATH    Path to luwes_raw.db  (default: data/luwes_raw.db)
    BILLING_DB_PATH  Path to billing.db    (default: data/billing.db)

Prerequisites:
    pip install psycopg2-binary python-dotenv
"""

import os
import sys
import json
import sqlite3
import logging
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ── Connection helpers ────────────────────────────────────────────────────────

def _pg_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _sqlite_conn(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(f"SQLite file not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ── Migration functions ───────────────────────────────────────────────────────

def migrate_users(sqlite_path: str, pg) -> None:
    logger.info("Migrating users from %s", sqlite_path)
    if not Path(sqlite_path).exists():
        logger.warning("Skipping — file not found: %s", sqlite_path)
        return

    sc = _sqlite_conn(sqlite_path)
    rows = sc.execute(
        "SELECT id, full_name, email, password_hash, salt, created_at, last_login FROM users"
    ).fetchall()
    sc.close()

    if not rows:
        logger.info("No users to migrate")
        return

    inserted = skipped = 0
    with pg.cursor() as cur:
        for r in rows:
            try:
                cur.execute(
                    """
                    INSERT INTO users (full_name, email, password_hash, salt, created_at, last_login)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (r["full_name"], r["email"], r["password_hash"], r["salt"],
                     r["created_at"], r["last_login"]),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.error("Error migrating user %s: %s", r["email"], exc)

    pg.commit()
    logger.info("Users: %d inserted, %d skipped (already exist)", inserted, skipped)


def migrate_luwes(sqlite_path: str, pg) -> None:
    logger.info("Migrating Luwes observations from %s", sqlite_path)
    if not Path(sqlite_path).exists():
        logger.warning("Skipping — file not found: %s", sqlite_path)
        return

    sc = _sqlite_conn(sqlite_path)
    rows = sc.execute(
        """
        SELECT rec, station_id, station_name, imei, level_m, recorded_at, fetched_at
        FROM water_level_observations
        ORDER BY recorded_at ASC
        """
    ).fetchall()
    logger.info("Found %d observations in SQLite", len(rows))

    BATCH = 500
    inserted = skipped = 0
    with pg.cursor() as cur:
        batch = []
        for r in rows:
            batch.append((r["rec"], r["station_id"], r["station_name"],
                          r["imei"], r["level_m"], r["recorded_at"], r["fetched_at"]))
            if len(batch) >= BATCH:
                try:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO water_level_observations
                            (rec, station_id, station_name, imei, level_m, recorded_at, fetched_at)
                        VALUES %s
                        ON CONFLICT (rec) DO NOTHING
                        """,
                        batch,
                    )
                    inserted += cur.rowcount
                    skipped += len(batch) - cur.rowcount
                    pg.commit()
                    logger.info("  %d / %d rows processed ...", inserted + skipped, len(rows))
                except Exception as exc:
                    logger.error("Batch error: %s", exc)
                    pg.rollback()
                batch = []

        if batch:
            try:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO water_level_observations
                        (rec, station_id, station_name, imei, level_m, recorded_at, fetched_at)
                    VALUES %s
                    ON CONFLICT (rec) DO NOTHING
                    """,
                    batch,
                )
                inserted += cur.rowcount
                pg.commit()
            except Exception as exc:
                logger.error("Final batch error: %s", exc)
                pg.rollback()

    logger.info("Observations: %d inserted, %d skipped", inserted, skipped)

    log_rows = sc.execute(
        "SELECT fetched_at, imei, status, rec, level_m, recorded_at, message FROM fetch_log"
    ).fetchall() if Path(sqlite_path).exists() else []
    sc.close()

    if log_rows:
        with pg.cursor() as cur:
            try:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO fetch_log (fetched_at, imei, status, rec, level_m, recorded_at, message)
                    VALUES %s
                    """,
                    [(r["fetched_at"], r["imei"], r["status"], r["rec"],
                      r["level_m"], r["recorded_at"], r["message"]) for r in log_rows],
                )
                pg.commit()
                logger.info("Fetch log: %d entries migrated", len(log_rows))
            except Exception as exc:
                logger.warning("Fetch log migration warning (non-critical): %s", exc)
                pg.rollback()


def migrate_billing(sqlite_path: str, pg) -> None:
    logger.info("Migrating billing data from %s", sqlite_path)
    if not Path(sqlite_path).exists():
        logger.warning("Skipping — file not found: %s", sqlite_path)
        return

    sc = _sqlite_conn(sqlite_path)

    subs = sc.execute("SELECT * FROM subscriptions").fetchall()
    logger.info("Found %d subscriptions in SQLite", len(subs))

    with pg.cursor() as cur:
        for r in subs:
            cur.execute("SELECT id FROM users WHERE id = %s", (r["user_id"],))
            if not cur.fetchone():
                logger.warning("Skipping subscription — user_id=%d not in PostgreSQL", r["user_id"])
                continue
            try:
                cur.execute(
                    """
                    INSERT INTO subscriptions (user_id, plan, status, starts_at, expires_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        plan       = EXCLUDED.plan,
                        status     = EXCLUDED.status,
                        starts_at  = EXCLUDED.starts_at,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (r["user_id"], r["plan"], r["status"],
                     r["starts_at"], r["expires_at"], r["updated_at"]),
                )
            except Exception as exc:
                logger.error("Error migrating subscription user_id=%d: %s", r["user_id"], exc)

    pg.commit()
    logger.info("Subscriptions migrated")

    payments = sc.execute("SELECT * FROM payments").fetchall()
    sc.close()
    logger.info("Found %d payments in SQLite", len(payments))

    with pg.cursor() as cur:
        for r in payments:
            cur.execute("SELECT id FROM users WHERE id = %s", (r["user_id"],))
            if not cur.fetchone():
                logger.warning("Skipping payment — user_id=%d not in PostgreSQL", r["user_id"])
                continue
            raw_wh = r["raw_webhook"]
            if raw_wh:
                try:
                    raw_wh = json.dumps(json.loads(raw_wh))
                except Exception:
                    pass
            try:
                cur.execute(
                    """
                    INSERT INTO payments
                        (user_id, order_id, snap_token, plan, amount_idr,
                         status, midtrans_id, payment_type,
                         created_at, settled_at, raw_webhook)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB)
                    ON CONFLICT (order_id) DO NOTHING
                    """,
                    (r["user_id"], r["order_id"], r["snap_token"], r["plan"], r["amount_idr"],
                     r["status"], r["midtrans_id"], r["payment_type"],
                     r["created_at"], r["settled_at"], raw_wh),
                )
            except Exception as exc:
                logger.error("Error migrating payment %s: %s", r["order_id"], exc)

    pg.commit()
    logger.info("Payments migrated")


def verify(pg) -> None:
    logger.info("Verification:")
    with pg.cursor() as cur:
        for table in ("users", "water_level_observations", "fetch_log", "subscriptions", "payments"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            n = cur.fetchone()["n"]
            logger.info("  %-32s %d rows", table, n)


def main() -> None:
    auth_db = os.getenv("AUTH_DB_PATH", "data/auth.db")
    luwes_db = os.getenv("LUWES_DB_PATH", "data/luwes_raw.db")
    billing_db = os.getenv("BILLING_DB_PATH", "data/billing.db")

    logger.info("Starting SQLite → PostgreSQL migration")

    pg = _pg_conn()
    try:
        migrate_users(auth_db, pg)
        migrate_luwes(luwes_db, pg)
        migrate_billing(billing_db, pg)
        verify(pg)
        logger.info("Migration complete")
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        pg.rollback()
        sys.exit(1)
    finally:
        pg.close()


if __name__ == "__main__":
    main()