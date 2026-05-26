"""Billing database operations (PostgreSQL).

FIX: upsert_subscription menggunakan (%s * INTERVAL '1 day') 
     mengganti INTERVAL '%s days' yang tidak bisa disubstitusi psycopg2.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from .pg_db import execute_one, execute_returning, execute_write

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

PLAN_CONFIG = {
    "pro_monthly": {"amount": 39_000, "days": 30},
    "pro_annual":  {"amount": 139_000, "days": 365},
}


def init_billing_db(db_path: str = None) -> None:
    logger.debug("billing_db: PostgreSQL mode — schema managed via migration SQL")


def get_subscription(db_path: str, user_id: int) -> Dict:
    row = execute_one("SELECT * FROM subscriptions WHERE user_id = %s", (user_id,))
    if not row:
        return {"plan": "free", "status": "active", "expires_at": None, "user_id": user_id}

    sub = dict(row)
    exp = sub.get("expires_at")
    if exp and sub.get("status") == "active":
        now = datetime.now(timezone.utc)
        exp_aware = exp if getattr(exp, "tzinfo", None) else exp.replace(tzinfo=timezone.utc)
        if exp_aware < now:
            execute_write(
                "UPDATE subscriptions SET status = 'expired', updated_at = NOW() WHERE user_id = %s",
                (user_id,),
            )
            sub["status"] = "expired"

    for key in ("starts_at", "expires_at", "updated_at"):
        val = sub.get(key)
        if val and hasattr(val, "isoformat"):
            sub[key] = val.isoformat()

    return sub


def upsert_subscription(db_path: str, user_id: int, plan: str, days: int) -> Dict:
    """Insert or update subscription.

    FIX: menggunakan (%s * INTERVAL '1 day') bukan INTERVAL '%s days'
    karena psycopg2 tidak mensubstitusi %s di dalam string literal SQL.
    """
    row = execute_returning(
        """
        INSERT INTO subscriptions (user_id, plan, status, starts_at, expires_at, updated_at)
        VALUES (
            %s, %s, 'active',
            NOW(),
            NOW() + (%s * INTERVAL '1 day'),
            NOW()
        )
        ON CONFLICT (user_id) DO UPDATE SET
            plan       = EXCLUDED.plan,
            status     = 'active',
            starts_at  = EXCLUDED.starts_at,
            expires_at = EXCLUDED.expires_at,
            updated_at = EXCLUDED.updated_at
        RETURNING
            plan,
            status,
            starts_at  AT TIME ZONE 'UTC' AS starts_at,
            expires_at AT TIME ZONE 'UTC' AS expires_at
        """,
        (user_id, plan, days),
    )
    result = dict(row)
    for key in ("starts_at", "expires_at"):
        if result.get(key) and hasattr(result[key], "isoformat"):
            result[key] = result[key].isoformat()
    return result


def create_payment(
    db_path: str,
    user_id: int,
    order_id: str,
    snap_token: str,
    plan: str,
    amount: int,
) -> int:
    row = execute_returning(
        """
        INSERT INTO payments (user_id, order_id, snap_token, plan, amount_idr, status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (user_id, order_id, snap_token, plan, amount),
    )
    return row["id"]


def settle_payment(
    db_path: str,
    order_id: str,
    midtrans_id: str,
    payment_type: str,
    raw: str,
) -> Optional[Dict]:
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
        (midtrans_id, payment_type, json.dumps(raw_data) if isinstance(raw_data, dict) else raw, order_id),
    )
    if not row:
        return None

    result = dict(row)
    for key in ("created_at", "settled_at"):
        if result.get(key) and hasattr(result[key], "isoformat"):
            result[key] = result[key].isoformat()
    return result


def update_payment_status(db_path: str, order_id: str, status: str, raw: str) -> None:
    try:
        raw_data = json.loads(raw)
    except Exception:
        raw_data = raw

    execute_write(
        "UPDATE payments SET status = %s, raw_webhook = %s::JSONB WHERE order_id = %s",
        (status, json.dumps(raw_data) if isinstance(raw_data, dict) else raw, order_id),
    )


def is_pro(db_path: str, user_id: int) -> bool:
    sub = get_subscription(db_path, user_id)
    return sub.get("plan") in ("pro_monthly", "pro_annual") and sub.get("status") == "active"