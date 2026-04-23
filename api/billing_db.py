"""
billing_db.py — PostgreSQL version
Menggantikan implementasi SQLite sebelumnya.

API publik identik sehingga billing_routes.py tidak perlu diubah banyak.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from .pg_db import get_cursor, execute_one, execute_returning, execute_write

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

PLAN_CONFIG = {
    "pro_monthly": {"amount": 39000,  "days": 30},
    "pro_annual":  {"amount": 139000, "days": 365},
}


# ── No-op: tabel dibuat via SQL migration ────────────────────
def init_billing_db(db_path: str = None):
    """No-op di PostgreSQL mode."""
    logger.info("[billing_db] PostgreSQL mode — tabel sudah ada via migration SQL")


# ══════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════

def get_subscription(db_path: str, user_id: int) -> Dict:
    """
    Ambil subscription untuk user_id.
    Jika tidak ada, kembalikan default free plan.
    Auto-expire jika expires_at sudah lewat.
    """
    row = execute_one(
        "SELECT * FROM subscriptions WHERE user_id = %s",
        (user_id,)
    )
    if not row:
        return {"plan": "free", "status": "active", "expires_at": None, "user_id": user_id}

    sub = dict(row)

    # Auto-expire check
    exp = sub.get("expires_at")
    if exp and sub.get("status") == "active":
        now = datetime.now(timezone.utc)
        if hasattr(exp, "tzinfo"):
            exp_aware = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        else:
            exp_aware = datetime.fromisoformat(str(exp)).replace(tzinfo=timezone.utc)

        if exp_aware < now:
            execute_write(
                "UPDATE subscriptions SET status = 'expired', updated_at = NOW() WHERE user_id = %s",
                (user_id,)
            )
            sub["status"] = "expired"

    # Serialise datetimes
    for k in ("starts_at", "expires_at", "updated_at"):
        v = sub.get(k)
        if v and hasattr(v, "isoformat"):
            sub[k] = v.isoformat()

    return sub


def upsert_subscription(db_path: str, user_id: int, plan: str, days: int) -> Dict:
    """INSERT atau UPDATE subscription untuk user_id."""
    row = execute_returning(
        """
        INSERT INTO subscriptions (user_id, plan, status, starts_at, expires_at, updated_at)
        VALUES (%s, %s, 'active', NOW(), NOW() + INTERVAL '%s days', NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            plan       = EXCLUDED.plan,
            status     = 'active',
            starts_at  = EXCLUDED.starts_at,
            expires_at = EXCLUDED.expires_at,
            updated_at = EXCLUDED.updated_at
        RETURNING plan, status,
                  starts_at  AT TIME ZONE 'UTC' AS starts_at,
                  expires_at AT TIME ZONE 'UTC' AS expires_at
        """,
        (user_id, plan, days)
    )
    result = dict(row)
    for k in ("starts_at", "expires_at"):
        if result.get(k) and hasattr(result[k], "isoformat"):
            result[k] = result[k].isoformat()
    return result


# ══════════════════════════════════════════════════════════════
# PAYMENTS
# ══════════════════════════════════════════════════════════════

def create_payment(
    db_path: str, user_id: int, order_id: str,
    snap_token: str, plan: str, amount: int
) -> int:
    """Insert payment baru, return id."""
    row = execute_returning(
        """
        INSERT INTO payments
            (user_id, order_id, snap_token, plan, amount_idr, status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (user_id, order_id, snap_token, plan, amount)
    )
    return row["id"]


def settle_payment(
    db_path: str, order_id: str, midtrans_id: str,
    payment_type: str, raw: str
) -> Optional[Dict]:
    """Update payment ke status settlement, return full row."""
    import json
    # raw bisa berupa JSON string — simpan sebagai JSONB
    try:
        raw_data = json.loads(raw)
    except Exception:
        raw_data = raw

    row = execute_returning(
        """
        UPDATE payments SET
            status       = 'settlement',
            midtrans_id  = %s,
            payment_type = %s,
            settled_at   = NOW(),
            raw_webhook  = %s::JSONB
        WHERE order_id = %s AND status = 'pending'
        RETURNING *
        """,
        (midtrans_id, payment_type, json.dumps(raw_data) if isinstance(raw_data, dict) else raw, order_id)
    )
    if not row:
        return None
    result = dict(row)
    for k in ("created_at", "settled_at"):
        if result.get(k) and hasattr(result[k], "isoformat"):
            result[k] = result[k].isoformat()
    return result


def update_payment_status(db_path: str, order_id: str, status: str, raw: str):
    """Update status payment (pending/deny/cancel/expire)."""
    import json
    try:
        raw_data = json.loads(raw)
    except Exception:
        raw_data = raw

    execute_write(
        """
        UPDATE payments SET
            status      = %s,
            raw_webhook = %s::JSONB
        WHERE order_id = %s
        """,
        (status, json.dumps(raw_data) if isinstance(raw_data, dict) else raw, order_id)
    )


def is_pro(db_path: str, user_id: int) -> bool:
    """Cek apakah user memiliki plan Pro yang aktif."""
    sub = get_subscription(db_path, user_id)
    return sub.get("plan") in ("pro_monthly", "pro_annual") and sub.get("status") == "active"