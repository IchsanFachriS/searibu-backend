"""
auth_routes.py — Fixed v2

Perubahan dari versi sebelumnya:
  - Hapus _auth_db_path dan _require_db() — tidak dibutuhkan di PostgreSQL mode
  - /register dan /login langsung panggil auth_db tanpa db_path
  - /google tidak lagi import sqlite3; update last_login via auth_db.update_last_login()
  - setup_auth() tetap ada sebagai no-op agar app.py tidak perlu diubah

Endpoints:
  POST /api/auth/register  → daftar akun baru
  POST /api/auth/login     → login email + password
  POST /api/auth/google    → Google OAuth sign-in (create or find user)
"""

import re
import uuid
import logging
from flask import Blueprint, jsonify, request

from .auth_db import (
    create_user,
    verify_user,
    get_user_by_email,
    update_last_login,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def setup_auth(db_path: str = None):
    """No-op — dipertahankan agar app.py tidak perlu diubah."""
    pass


def _is_valid_email(email: str) -> bool:
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


# ── POST /api/auth/register ───────────────────────────────────────────────────

@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Daftar akun baru dengan email + password.
    Body JSON: { full_name, email, password }
    """
    data      = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email     = (data.get("email") or "").strip()
    password  = data.get("password") or ""

    # Validasi input
    if not full_name or len(full_name) < 2:
        return jsonify({"error": "Nama minimal 2 karakter"}), 400
    if not email or not _is_valid_email(email):
        return jsonify({"error": "Format email tidak valid"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Password minimal 6 karakter"}), 400

    try:
        user = create_user(full_name, email, password)
        logger.info(f"[auth] User baru terdaftar: {email}")
        return jsonify({
            "message": "Registrasi berhasil! Selamat datang di Searibu.",
            "user": user,
        }), 201

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    except Exception as exc:
        logger.error(f"[auth] register error untuk {email}: {exc}", exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


# ── POST /api/auth/login ──────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Login dengan email + password.
    Body JSON: { email, password }
    """
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email:
        return jsonify({"error": "Email tidak boleh kosong"}), 400
    if not password:
        return jsonify({"error": "Password tidak boleh kosong"}), 400

    try:
        user = verify_user(email, password)
        if not user:
            return jsonify({"error": "Email atau password salah"}), 401

        logger.info(f"[auth] Login berhasil: {email}")
        return jsonify({
            "message": f"Selamat datang kembali, {user['full_name']}!",
            "user": user,
        }), 200

    except Exception as exc:
        logger.error(f"[auth] login error untuk {email}: {exc}", exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500


# ── POST /api/auth/google ─────────────────────────────────────────────────────

@auth_bp.route("/google", methods=["POST"])
def google_auth():
    """
    Google OAuth sign-in.
    Cari user by email; jika belum ada, buat akun baru.
    Last login selalu diupdate ke PostgreSQL.

    Body JSON: { email, full_name, google_id, avatar? }
    """
    data      = request.get_json(silent=True) or {}
    email     = (data.get("email") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()

    if not email or not _is_valid_email(email):
        return jsonify({"error": "Email tidak valid"}), 400

    # Fallback nama jika tidak dikirim
    if not full_name:
        full_name = email.split("@")[0].replace(".", " ").title()

    try:
        user = get_user_by_email(email)

        if user:
            # User sudah ada — update last_login di PostgreSQL
            last_login = update_last_login(user["id"])
            user["last_login"] = last_login
            logger.info(f"[auth] Google sign-in (existing user): {email}")
            return jsonify({
                "message": f"Selamat datang kembali, {user['full_name']}!",
                "user": user,
            }), 200

        else:
            # User baru — buat akun dengan random password
            # (Google users tidak pernah pakai password login)
            random_password = uuid.uuid4().hex
            new_user = create_user(full_name, email, random_password)
            logger.info(f"[auth] Google sign-in (new user): {email}")
            return jsonify({
                "message": "Akun berhasil dibuat! Selamat datang di Searibu.",
                "user": new_user,
            }), 201

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    except Exception as exc:
        logger.error(f"[auth] google_auth error untuk {email}: {exc}", exc_info=True)
        return jsonify({"error": f"Internal server error: {exc}"}), 500