"""Profile Flask blueprint.

Endpoints:
    GET  /api/profile        — return current user profile including role
    PUT  /api/profile/role   — update user role
"""

import logging
from flask import Blueprint, jsonify, request
from .auth_db import get_user_by_email
from .pg_db import execute_returning, execute_one

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__, url_prefix="/api/profile")

VALID_ROLES = {"general", "researcher"}


def get_user_with_role(email: str):
    """Return user record including role field."""
    row = execute_one(
        "SELECT id, full_name, email, role, created_at, last_login FROM users WHERE email = %s",
        (email.lower().strip(),),
    )
    if not row:
        return None
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "role": row["role"] or "general",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": row["last_login"].isoformat() if row["last_login"] else None,
    }


@profile_bp.route("", methods=["GET"])
def get_profile():
    """Return user profile including role.

    Query params:
        email (str, required)
    """
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email parameter is required"}), 400

    user = get_user_with_role(email)
    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(user), 200


@profile_bp.route("/role", methods=["PUT"])
def update_role():
    """Update user role.

    Request body (JSON):
        email (str), role (str: 'general' | 'researcher')
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    role = (data.get("role") or "").strip().lower()

    if not email:
        return jsonify({"error": "email is required"}), 400
    if role not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    try:
        row = execute_returning(
            "UPDATE users SET role = %s WHERE email = %s RETURNING id, full_name, email, role",
            (role, email),
        )
        if not row:
            return jsonify({"error": "User not found"}), 404

        logger.info("Role updated: email=%s role=%s", email, role)
        return jsonify({
            "message": "Role updated successfully",
            "user": {
                "id": row["id"],
                "full_name": row["full_name"],
                "email": row["email"],
                "role": row["role"],
            },
        }), 200

    except Exception as exc:
        logger.error("Role update error for %s: %s", email, exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500