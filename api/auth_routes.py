"""
auth_routes.py — Updated
Adds:
  POST /api/auth/google  → Google OAuth sign-in (create or find user)

All existing endpoints (/register, /login) unchanged.
"""

import os
import re
from flask import Blueprint, jsonify, request
from .auth_db import create_user, verify_user, get_user_by_email

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

_auth_db_path: str | None = None


def setup_auth(db_path: str = None):
    pass


def _require_db() -> str:
    if not _auth_db_path:
        raise RuntimeError("Auth DB not initialised. Call setup_auth() first.")
    return _auth_db_path


def _is_valid_email(email: str) -> bool:
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


# ── POST /api/auth/register ───────────────────────────────────────────────

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email     = (data.get("email") or "").strip()
    password  = data.get("password") or ""

    if not full_name or len(full_name) < 2:
        return jsonify({"error": "Nama minimal 2 karakter"}), 400
    if not email or not _is_valid_email(email):
        return jsonify({"error": "Format email tidak valid"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Password minimal 6 karakter"}), 400

    try:
        db   = _require_db()
        user = create_user(db, full_name, email, password)
        return jsonify({"message": "Registrasi berhasil! Selamat datang di Searibu.", "user": user}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


# ── POST /api/auth/login ──────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
def login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email:
        return jsonify({"error": "Email tidak boleh kosong"}), 400
    if not password:
        return jsonify({"error": "Password tidak boleh kosong"}), 400

    try:
        db   = _require_db()
        user = verify_user(db, email, password)
        if not user:
            return jsonify({"error": "Email atau password salah"}), 401
        return jsonify({"message": f"Selamat datang kembali, {user['full_name']}!", "user": user}), 200
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


# ── POST /api/auth/google ─────────────────────────────────────────────────

@auth_bp.route("/google", methods=["POST"])
def google_auth():
    """
    POST /api/auth/google
    Body: { email, full_name, google_id, avatar? }

    Find existing user by email or create one with a random password.
    Returns the same user dict as /login.
    """
    import uuid
    data      = request.get_json(silent=True) or {}
    email     = (data.get("email") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()

    if not email or not _is_valid_email(email):
        return jsonify({"error": "Email tidak valid"}), 400
    if not full_name:
        full_name = email.split("@")[0].title()

    try:
        db   = _require_db()
        user = get_user_by_email(db, email)
        if user:
            # Update last_login
            from .auth_db import verify_user as _v
            # We can't verify with Google password — just return the user directly
            from datetime import datetime, timezone, timedelta
            import sqlite3
            WIB = timezone(timedelta(hours=7))
            now = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
            try:
                import sqlite3 as _sq
                conn = _sq.connect(db, check_same_thread=False)
                conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user["id"]))
                conn.commit()
                conn.close()
                user["last_login"] = now
            except Exception:
                pass
            return jsonify({
                "message": f"Selamat datang kembali, {user['full_name']}!",
                "user": user,
            }), 200
        else:
            # Create new user with random password (Google users never use password login)
            new_user = create_user(db, full_name, email, uuid.uuid4().hex)
            return jsonify({
                "message": "Akun berhasil dibuat!",
                "user": new_user,
            }), 201

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500