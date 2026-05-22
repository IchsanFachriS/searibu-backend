"""Admin Flask blueprint.

Endpoints:
    GET /api/admin/stats    — user counts, role breakdown, pro subscribers
    GET /api/admin/payments — recent payment list (paginated)

All endpoints require the requesting user to have is_admin = TRUE.
Admin check is done via ?email= query param (same pattern as billing).
"""

import logging
from flask import Blueprint, jsonify, request
from .pg_db import execute_one, execute_all

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def _require_admin(email: str) -> bool:
    """Return True if the given email belongs to an admin user."""
    if not email:
        return False
    row = execute_one(
        "SELECT is_admin FROM users WHERE email = %s",
        (email.lower().strip(),),
    )
    return bool(row and row["is_admin"])


@admin_bp.route("/stats", methods=["GET"])
def get_stats():
    """Return aggregate platform statistics.

    Query params:
        email (str, required) — must belong to an admin user
    """
    email = (request.args.get("email") or "").strip().lower()
    if not _require_admin(email):
        return jsonify({"error": "Forbidden — admin access required"}), 403

    try:
        # Total users
        total_row = execute_one("SELECT COUNT(*) AS n FROM users")
        total_users = int(total_row["n"]) if total_row else 0

        # Users by role
        role_rows = execute_all(
            "SELECT role, COUNT(*) AS n FROM users GROUP BY role ORDER BY role"
        )
        role_breakdown = {r["role"] or "general": int(r["n"]) for r in role_rows}

        # Admin count
        admin_row = execute_one(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = TRUE"
        )
        admin_count = int(admin_row["n"]) if admin_row else 0

        # Active Pro subscribers
        pro_row = execute_one(
            """
            SELECT COUNT(*) AS n FROM subscriptions
            WHERE plan IN ('pro_monthly', 'pro_annual')
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > NOW())
            """
        )
        pro_active = int(pro_row["n"]) if pro_row else 0

        # New users last 30 days
        new_row = execute_one(
            "SELECT COUNT(*) AS n FROM users WHERE created_at >= NOW() - INTERVAL '30 days'"
        )
        new_users_30d = int(new_row["n"]) if new_row else 0

        # Total settled payments & revenue
        revenue_row = execute_one(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(amount_idr), 0) AS total
            FROM payments WHERE status = 'settlement'
            """
        )
        total_payments  = int(revenue_row["n"])     if revenue_row else 0
        total_revenue   = int(revenue_row["total"]) if revenue_row else 0

        return jsonify({
            "users": {
                "total":      total_users,
                "by_role":    role_breakdown,
                "admins":     admin_count,
                "new_30d":    new_users_30d,
            },
            "subscriptions": {
                "pro_active": pro_active,
            },
            "payments": {
                "total_settled": total_payments,
                "total_revenue_idr": total_revenue,
            },
        }), 200

    except Exception as exc:
        logger.error("Admin stats error: %s", exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@admin_bp.route("/payments", methods=["GET"])
def get_payments():
    """Return recent payments list.

    Query params:
        email  (str, required) — must belong to an admin user
        limit  (int, default 20, max 100)
        offset (int, default 0)
    """
    email = (request.args.get("email") or "").strip().lower()
    if not _require_admin(email):
        return jsonify({"error": "Forbidden — admin access required"}), 403

    try:
        limit  = min(int(request.args.get("limit",  20)), 100)
        offset = max(int(request.args.get("offset",  0)),   0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    try:
        rows = execute_all(
            """
            SELECT
                p.order_id,
                u.email,
                u.full_name,
                p.plan,
                p.amount_idr,
                p.status,
                p.payment_type,
                p.created_at  AT TIME ZONE 'Asia/Jakarta' AS created_at,
                p.settled_at  AT TIME ZONE 'Asia/Jakarta' AS settled_at
            FROM payments p
            JOIN users u ON u.id = p.user_id
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )

        result = []
        for r in rows:
            result.append({
                "order_id":    r["order_id"],
                "email":       r["email"],
                "full_name":   r["full_name"],
                "plan":        r["plan"],
                "amount_idr":  r["amount_idr"],
                "status":      r["status"],
                "payment_type": r["payment_type"],
                "created_at":  r["created_at"].isoformat()  if r["created_at"]  else None,
                "settled_at":  r["settled_at"].isoformat()  if r["settled_at"]  else None,
            })

        # Total count for pagination
        count_row = execute_one("SELECT COUNT(*) AS n FROM payments")
        total = int(count_row["n"]) if count_row else 0

        return jsonify({
            "payments": result,
            "total":    total,
            "limit":    limit,
            "offset":   offset,
        }), 200

    except Exception as exc:
        logger.error("Admin payments error: %s", exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500