import os
import re
from flask import Blueprint, jsonify, request
from .auth_db import create_user, verify_user

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# Path DB di-set dari app.py via setup_auth()
_auth_db_path: str | None = None


def setup_auth(db_path: str):
    global _auth_db_path
    _auth_db_path = db_path


def _require_db() -> str:
    if not _auth_db_path:
        raise RuntimeError("Auth DB belum diinisialisasi. Panggil setup_auth() dulu.")
    return _auth_db_path


def _is_valid_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


@auth_bp.route("/register", methods=["POST"])
def register():
    """
    POST /api/auth/register
    Body JSON: { full_name, email, password }

    Response 201: { message, user: { id, full_name, email, created_at } }
    Response 400: { error: "..." }
    Response 409: { error: "Email sudah terdaftar" }
    """
    data = request.get_json(silent=True) or {}

    full_name = (data.get("full_name") or "").strip()
    email     = (data.get("email") or "").strip()
    password  = data.get("password") or ""

    # Validasi input
    if not full_name:
        return jsonify({"error": "Nama lengkap tidak boleh kosong"}), 400
    if len(full_name) < 2:
        return jsonify({"error": "Nama terlalu pendek (minimal 2 karakter)"}), 400
    if not email:
        return jsonify({"error": "Email tidak boleh kosong"}), 400
    if not _is_valid_email(email):
        return jsonify({"error": "Format email tidak valid"}), 400
    if not password:
        return jsonify({"error": "Password tidak boleh kosong"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password minimal 6 karakter"}), 400

    try:
        db = _require_db()
        user = create_user(db, full_name, email, password)
        return jsonify({
            "message": "Registrasi berhasil! Selamat datang di Searibu.",
            "user": user,
        }), 201

    except ValueError as exc:
        # Email duplikat
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500


@auth_bp.route("/login", methods=["POST"])
def login():
    """
    POST /api/auth/login
    Body JSON: { email, password }

    Response 200: { message, user: { id, full_name, email, ... } }
    Response 401: { error: "Email atau password salah" }
    Response 400: { error: "..." }
    """
    data = request.get_json(silent=True) or {}

    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email:
        return jsonify({"error": "Email tidak boleh kosong"}), 400
    if not password:
        return jsonify({"error": "Password tidak boleh kosong"}), 400

    try:
        db = _require_db()
        user = verify_user(db, email, password)

        if not user:
            return jsonify({"error": "Email atau password salah"}), 401

        return jsonify({
            "message": f"Selamat datang kembali, {user['full_name']}!",
            "user": user,
        }), 200

    except Exception as exc:
        return jsonify({"error": f"Internal server error: {exc}"}), 500