"""Authentication Flask blueprint.

Endpoints:
    POST /api/auth/register  — register a new account
    POST /api/auth/login     — email + password login
    POST /api/auth/google    — Google OAuth sign-in / sign-up
"""

import re
import uuid
import logging
from flask import Blueprint, jsonify, request

from .auth_db import create_user, verify_user, get_user_by_email, update_last_login

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def setup_auth(db_path: str = None) -> None:
    """No-op — kept for backward-compatibility with app.py startup sequence."""


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


@auth_bp.route("/register", methods=["POST"])
def register():
    """Register a new user account.

    Request body (JSON):
        full_name (str), email (str), password (str, min 6 chars)
    """
    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not full_name or len(full_name) < 2:
        return jsonify({"error": "Name must be at least 2 characters"}), 400
    if not email or not _valid_email(email):
        return jsonify({"error": "Invalid email format"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        user = create_user(full_name, email, password)
        logger.info("New user registered: %s", email)
        return jsonify({"message": "Registration successful! Welcome to Searibu.", "user": user}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.error("Register error for %s: %s", email, exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate a user with email and password.

    Request body (JSON):
        email (str), password (str)
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email:
        return jsonify({"error": "Email is required"}), 400
    if not password:
        return jsonify({"error": "Password is required"}), 400

    try:
        user = verify_user(email, password)
        if not user:
            return jsonify({"error": "Incorrect email or password"}), 401
        logger.info("Login successful: %s", email)
        return jsonify({"message": f"Welcome back, {user['full_name']}!", "user": user}), 200
    except Exception as exc:
        logger.error("Login error for %s: %s", email, exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@auth_bp.route("/google", methods=["POST"])
def google_auth():
    """Sign in or register via Google OAuth.

    Looks up the user by email; creates a new account if none exists.

    Request body (JSON):
        email (str), full_name (str), google_id (str), avatar (str, optional)
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()

    if not email or not _valid_email(email):
        return jsonify({"error": "Invalid email address"}), 400

    if not full_name:
        full_name = email.split("@")[0].replace(".", " ").title()

    try:
        user = get_user_by_email(email)
        if user:
            last_login = update_last_login(user["id"])
            user["last_login"] = last_login
            logger.info("Google sign-in (existing user): %s", email)
            return jsonify({"message": f"Welcome back, {user['full_name']}!", "user": user}), 200

        new_user = create_user(full_name, email, uuid.uuid4().hex)
        logger.info("Google sign-in (new user): %s", email)
        return jsonify({"message": "Account created! Welcome to Searibu.", "user": new_user}), 201

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.error("Google auth error for %s: %s", email, exc, exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500