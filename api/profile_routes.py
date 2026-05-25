"""Profile and Admin Flask blueprint.

Endpoints:
    GET /api/profile              — get user profile (role, is_admin)
    PUT /api/profile/role         — update user role
    GET /api/admin/stats          — admin dashboard stats
    GET /api/admin/payments       — paginated payment list
"""

import logging
from flask import Blueprint, jsonify, request
from .pg_db import execute_one, execute_returning, execute_all

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__, url_prefix="/api")


def _require_admin(email: str) -> bool:
    row = execute_one(
        "SELECT is_admin FROM users WHERE email = %s",
        (email.lower().strip(),)
    )
    return bool(row and row["is_admin"])


@profile_bp.route("/profile", methods=["GET"])
def get_profile():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    row = execute_one(
        """
        SELECT id, full_name, email, role, is_admin, created_at, last_login
        FROM users WHERE email = %s
        """,
        (email,)
    )
    if not row:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "role":       row["role"],
        "is_admin":   row["is_admin"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": row["last_login"].isoformat() if row["last_login"] else None,
    })


@profile_bp.route("/profile/role", methods=["PUT"])
def update_role():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    role  = data.get("role", "")

    if not email:
        return jsonify({"error": "email required"}), 400
    if role not in ("general", "researcher"):
        return jsonify({"error": "role must be 'general' or 'researcher'"}), 400

    row = execute_returning(
        """
        UPDATE users SET role = %s WHERE email = %s
        RETURNING id, full_name, email, role, is_admin, created_at
        """,
        (role, email)
    )
    if not row:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "message": "Role updated successfully",
        "user": {
            "id":        row["id"],
            "full_name": row["full_name"],
            "email":     row["email"],
            "role":      row["role"],
            "is_admin":  row["is_admin"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    })


@profile_bp.route("/admin/stats", methods=["GET"])
def admin_stats():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    if not _require_admin(email):
        return jsonify({"error": "Forbidden — admin only"}), 403

    users_total = execute_one("SELECT COUNT(*) AS n FROM users")["n"]
    users_admin = execute_one(
        "SELECT COUNT(*) AS n FROM users WHERE is_admin = true"
    )["n"]
    users_new30 = execute_one(
        "SELECT COUNT(*) AS n FROM users WHERE created_at >= NOW() - INTERVAL '30 days'"
    )["n"]

    role_rows = execute_all(
        "SELECT role, COUNT(*) AS n FROM users GROUP BY role"
    )
    by_role = {r["role"]: r["n"] for r in (role_rows or [])}

    pro_active = execute_one(
        """
        SELECT COUNT(*) AS n FROM subscriptions
        WHERE plan IN ('pro_monthly', 'pro_annual') AND status = 'active'
        """
    )["n"]

    pay_row = execute_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(amount_idr), 0) AS total
        FROM payments WHERE status = 'settlement'
        """
    )

    return jsonify({
        "users": {
            "total":   users_total,
            "by_role": by_role,
            "admins":  users_admin,
            "new_30d": users_new30,
        },
        "subscriptions": {
            "pro_active": pro_active,
        },
        "payments": {
            "total_settled":    pay_row["n"],
            "total_revenue_idr": int(pay_row["total"]),
        },
    })


@profile_bp.route("/admin/payments", methods=["GET"])
def admin_payments():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    if not _require_admin(email):
        return jsonify({"error": "Forbidden — admin only"}), 403

    try:
        limit  = min(int(request.args.get("limit",  10)), 100)
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    total_row = execute_one("SELECT COUNT(*) AS n FROM payments")

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
            p.created_at,
            p.settled_at
        FROM payments p
        JOIN users u ON p.user_id = u.id
        ORDER BY p.created_at DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset)
    )

    payments = []
    for r in (rows or []):
        payments.append({
            "order_id":     r["order_id"],
            "email":        r["email"],
            "full_name":    r["full_name"],
            "plan":         r["plan"],
            "amount_idr":   r["amount_idr"],
            "status":       r["status"],
            "payment_type": r["payment_type"],
            "created_at":   r["created_at"].isoformat() if r["created_at"] else None,
            "settled_at":   r["settled_at"].isoformat() if r["settled_at"] else None,
        })

    return jsonify({
        "total":    total_row["n"],
        "payments": payments,
    })