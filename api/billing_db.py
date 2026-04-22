"""
billing_db.py — SQLite tables for subscriptions and payments.

Tables:
  subscriptions → active plan per user
  payments      → Midtrans transaction log
"""

import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

_lock = threading.Lock()
WIB = timezone(timedelta(hours=7))

DDL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL UNIQUE,
    plan         TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'pro_monthly' | 'pro_annual'
    status       TEXT NOT NULL DEFAULT 'active', -- 'active' | 'expired' | 'cancelled'
    starts_at    TEXT,
    expires_at   TEXT,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    order_id        TEXT    NOT NULL UNIQUE,  -- our ID sent to Midtrans
    snap_token      TEXT,
    plan            TEXT    NOT NULL,
    amount_idr      INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    -- 'pending' | 'settlement' | 'expire' | 'cancel' | 'deny'
    midtrans_id     TEXT,   -- Midtrans transaction_id
    payment_type    TEXT,   -- gopay / va / card etc.
    created_at      TEXT    NOT NULL,
    settled_at      TEXT,
    raw_webhook     TEXT    -- full JSON from Midtrans webhook
);

CREATE INDEX IF NOT EXISTS idx_payments_order   ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_user    ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_user         ON subscriptions(user_id);
"""

PLAN_CONFIG = {
    "pro_monthly": {"amount": 39000, "days": 30},
    "pro_annual":  {"amount": 139000, "days": 365},
}


def init_billing_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        conn.executescript(DDL)
        conn.commit()
        conn.close()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")


# ── Subscriptions ──────────────────────────────────────────────

def get_subscription(db_path: str, user_id: int) -> Dict:
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row:
                return {"plan": "free", "status": "active",
                        "expires_at": None, "user_id": user_id}
            sub = dict(row)
            # Auto-expire check
            if sub["expires_at"] and sub["status"] == "active":
                exp = datetime.fromisoformat(sub["expires_at"])
                if exp < datetime.now(WIB):
                    conn.execute(
                        "UPDATE subscriptions SET status='expired', updated_at=? WHERE user_id=?",
                        (_now(), user_id)
                    )
                    conn.commit()
                    sub["status"] = "expired"
            return sub
        finally:
            conn.close()


def upsert_subscription(db_path: str, user_id: int,
                         plan: str, days: int) -> Dict:
    now = _now()
    starts_at = datetime.now(WIB)
    expires_at = (starts_at + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    starts_str = starts_at.strftime("%Y-%m-%dT%H:%M:%S+07:00")
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute("""
                INSERT INTO subscriptions (user_id, plan, status, starts_at, expires_at, updated_at)
                VALUES (?, ?, 'active', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan=excluded.plan, status='active',
                    starts_at=excluded.starts_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
            """, (user_id, plan, starts_str, expires_at, now))
            conn.commit()
            return {"plan": plan, "status": "active",
                    "starts_at": starts_str, "expires_at": expires_at}
        finally:
            conn.close()


# ── Payments ───────────────────────────────────────────────────

def create_payment(db_path: str, user_id: int, order_id: str,
                   snap_token: str, plan: str, amount: int) -> int:
    with _lock:
        conn = _connect(db_path)
        try:
            cur = conn.execute("""
                INSERT INTO payments
                    (user_id, order_id, snap_token, plan, amount_idr, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (user_id, order_id, snap_token, plan, amount, _now()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def settle_payment(db_path: str, order_id: str, midtrans_id: str,
                   payment_type: str, raw: str) -> Optional[Dict]:
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute("""
                UPDATE payments SET
                    status='settlement', midtrans_id=?, payment_type=?,
                    settled_at=?, raw_webhook=?
                WHERE order_id=? AND status='pending'
            """, (midtrans_id, payment_type, _now(), raw, order_id))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM payments WHERE order_id=?", (order_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def update_payment_status(db_path: str, order_id: str,
                           status: str, raw: str):
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE payments SET status=?, raw_webhook=? WHERE order_id=?",
                (status, raw, order_id)
            )
            conn.commit()
        finally:
            conn.close()


def is_pro(db_path: str, user_id: int) -> bool:
    sub = get_subscription(db_path, user_id)
    return sub.get("plan") in ("pro_monthly", "pro_annual") \
           and sub.get("status") == "active"