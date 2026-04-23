#!/usr/bin/env python3
"""
migrate_sqlite_to_pg.py
Migrasi data dari SQLite (auth.db, luwes_raw.db, billing.db) ke PostgreSQL.

Jalankan SEKALI dari mesin lokal yang punya akses ke kedua database:
  1. Copy auth.db, luwes_raw.db, billing.db dari server ke lokal
  2. Set DATABASE_URL ke Supabase connection string
  3. python migrate_sqlite_to_pg.py

Prasyarat:
  pip install psycopg2-binary python-dotenv
"""

import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))


def get_pg_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("Set DATABASE_URL environment variable")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def get_sqlite_conn(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(f"SQLite file tidak ditemukan: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_users(sqlite_path: str, pg_conn):
    """Migrasi tabel users dari auth.db"""
    logger.info("═══ Migrasi users ═══")
    if not Path(sqlite_path).exists():
        logger.warning(f"  Lewati: {sqlite_path} tidak ditemukan")
        return

    sc = get_sqlite_conn(sqlite_path)
    rows = sc.execute(
        "SELECT id, full_name, email, password_hash, salt, created_at, last_login FROM users"
    ).fetchall()
    sc.close()

    if not rows:
        logger.info("  Tidak ada data users untuk dimigrasikan")
        return

    with pg_conn.cursor() as cur:
        inserted = 0
        skipped  = 0
        for r in rows:
            try:
                cur.execute(
                    """
                    INSERT INTO users (full_name, email, password_hash, salt, created_at, last_login)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (
                        r["full_name"], r["email"], r["password_hash"], r["salt"],
                        r["created_at"], r["last_login"],
                    )
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"  Error user {r['email']}: {e}")

    pg_conn.commit()
    logger.info(f"  ✅ {inserted} users dimigrasikan, {skipped} dilewati (sudah ada)")


def migrate_luwes(sqlite_path: str, pg_conn):
    """Migrasi water_level_observations & fetch_log dari luwes_raw.db"""
    logger.info("═══ Migrasi Luwes observations ═══")
    if not Path(sqlite_path).exists():
        logger.warning(f"  Lewati: {sqlite_path} tidak ditemukan")
        return

    sc = get_sqlite_conn(sqlite_path)

    # ── water_level_observations ──────────────────────────────
    rows = sc.execute(
        """
        SELECT rec, station_id, station_name, imei, level_m, recorded_at, fetched_at
        FROM water_level_observations
        ORDER BY recorded_at ASC
        """
    ).fetchall()
    logger.info(f"  {len(rows)} observasi ditemukan di SQLite")

    inserted = 0
    skipped  = 0
    BATCH    = 500

    with pg_conn.cursor() as cur:
        batch = []
        for r in rows:
            batch.append((
                r["rec"], r["station_id"], r["station_name"],
                r["imei"], r["level_m"], r["recorded_at"], r["fetched_at"],
            ))
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
                    skipped  += (len(batch) - cur.rowcount)
                    pg_conn.commit()
                    logger.info(f"    batch {inserted}/{len(rows)} ...")
                except Exception as e:
                    logger.error(f"  Batch error: {e}")
                    pg_conn.rollback()
                batch = []

        # Sisa batch
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
                pg_conn.commit()
            except Exception as e:
                logger.error(f"  Batch terakhir error: {e}")
                pg_conn.rollback()

    logger.info(f"  ✅ {inserted} observasi dimigrasikan, {skipped} dilewati")

    # ── fetch_log ─────────────────────────────────────────────
    log_rows = sc.execute(
        "SELECT fetched_at, imei, status, rec, level_m, recorded_at, message FROM fetch_log"
    ).fetchall()
    logger.info(f"  {len(log_rows)} fetch log ditemukan")
    sc.close()

    if log_rows:
        with pg_conn.cursor() as cur:
            batch = [(r["fetched_at"], r["imei"], r["status"], r["rec"],
                      r["level_m"], r["recorded_at"], r["message"]) for r in log_rows]
            try:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO fetch_log (fetched_at, imei, status, rec, level_m, recorded_at, message)
                    VALUES %s
                    """,
                    batch,
                )
                pg_conn.commit()
                logger.info(f"  ✅ {len(log_rows)} fetch log dimigrasikan")
            except Exception as e:
                logger.warning(f"  fetch_log migration warning: {e}")
                pg_conn.rollback()


def migrate_billing(sqlite_path: str, pg_conn):
    """Migrasi subscriptions & payments dari billing.db"""
    logger.info("═══ Migrasi Billing ═══")
    if not Path(sqlite_path).exists():
        logger.warning(f"  Lewati: {sqlite_path} tidak ditemukan")
        return

    sc = get_sqlite_conn(sqlite_path)

    # ── subscriptions ─────────────────────────────────────────
    # Kita butuh user_id mapping dari email ke PG user_id
    subs = sc.execute("SELECT * FROM subscriptions").fetchall()
    logger.info(f"  {len(subs)} subscriptions ditemukan")

    with pg_conn.cursor() as cur:
        for r in subs:
            user_id = r["user_id"]
            # Cek apakah user_id ada di PG users
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if not cur.fetchone():
                logger.warning(f"  Lewati subscription: user_id={user_id} tidak ada di PG")
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
                    (user_id, r["plan"], r["status"],
                     r["starts_at"], r["expires_at"], r["updated_at"])
                )
            except Exception as e:
                logger.error(f"  Error subscription user_id={user_id}: {e}")

    pg_conn.commit()
    logger.info(f"  ✅ Subscriptions dimigrasikan")

    # ── payments ──────────────────────────────────────────────
    payments = sc.execute("SELECT * FROM payments").fetchall()
    sc.close()
    logger.info(f"  {len(payments)} payments ditemukan")

    with pg_conn.cursor() as cur:
        for r in payments:
            user_id = r["user_id"]
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if not cur.fetchone():
                logger.warning(f"  Lewati payment: user_id={user_id} tidak ada di PG")
                continue
            # Parse raw_webhook — SQLite simpan sebagai TEXT
            raw_wh = r["raw_webhook"]
            if raw_wh:
                try:
                    raw_wh_json = json.loads(raw_wh)
                    raw_wh = json.dumps(raw_wh_json)
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
                    (user_id, r["order_id"], r["snap_token"], r["plan"], r["amount_idr"],
                     r["status"], r["midtrans_id"], r["payment_type"],
                     r["created_at"], r["settled_at"], raw_wh)
                )
            except Exception as e:
                logger.error(f"  Error payment {r['order_id']}: {e}")

    pg_conn.commit()
    logger.info(f"  ✅ Payments dimigrasikan")


def verify_migration(pg_conn):
    """Verifikasi hasil migrasi."""
    logger.info("═══ Verifikasi ═══")
    with pg_conn.cursor() as cur:
        for table in ["users", "water_level_observations", "fetch_log", "subscriptions", "payments"]:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            n = cur.fetchone()["n"]
            logger.info(f"  {table}: {n:,} baris")


def main():
    # Paths ke SQLite databases — sesuaikan dengan lokasi file di mesin Anda
    AUTH_DB    = os.getenv("AUTH_DB_PATH",    "data/auth.db")
    LUWES_DB   = os.getenv("LUWES_DB_PATH",   "data/luwes_raw.db")
    BILLING_DB = os.getenv("BILLING_DB_PATH", "data/billing.db")

    logger.info("Memulai migrasi SQLite → PostgreSQL (Supabase)")
    logger.info(f"  auth.db:       {AUTH_DB}")
    logger.info(f"  luwes_raw.db:  {LUWES_DB}")
    logger.info(f"  billing.db:    {BILLING_DB}")

    pg = get_pg_conn()
    try:
        migrate_users(AUTH_DB, pg)
        migrate_luwes(LUWES_DB, pg)
        migrate_billing(BILLING_DB, pg)
        verify_migration(pg)
        logger.info("✅ Migrasi selesai!")
    except Exception as e:
        logger.error(f"❌ Migrasi gagal: {e}", exc_info=True)
        pg.rollback()
        sys.exit(1)
    finally:
        pg.close()


if __name__ == "__main__":
    main()