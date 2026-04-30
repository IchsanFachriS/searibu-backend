"""
auth_db.py — PostgreSQL version (fixed v2)

Semua operasi menggunakan pg_db.py (psycopg2 + connection pool).
Parameter db_path DIHAPUS dari semua fungsi publik — tidak relevan
di PostgreSQL mode.

Perubahan dari versi sebelumnya:
  - Hapus parameter db_path dari create_user, verify_user, get_user_by_email
  - Tambah fungsi update_last_login() yang dipakai google_auth
  - Tidak ada import sqlite3 sama sekali
"""

import hashlib
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from .pg_db import execute_one, execute_returning, execute_write

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))


# ── No-op stubs (dipanggil dari app.py, tidak melakukan apa-apa) ─────────────

def init_auth_db(db_path: str = None):
    """No-op di PostgreSQL mode. Tabel dibuat via migrations/001_initial_schema.sql."""
    logger.info("[auth_db] PostgreSQL mode — tabel sudah ada via migration SQL")


def setup_auth(db_path: str = None):
    """No-op di PostgreSQL mode."""
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    salted = f"{salt}{password}{salt}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────

def create_user(full_name: str, email: str, password: str) -> Dict:
    """
    Buat user baru di PostgreSQL.

    Return : dict user (id, full_name, email, created_at)
    Raises : ValueError jika email sudah terdaftar
    """
    salt = os.urandom(32).hex()
    password_hash = _hash_password(password, salt)

    try:
        row = execute_returning(
            """
            INSERT INTO users (full_name, email, password_hash, salt)
            VALUES (%s, %s, %s, %s)
            RETURNING id, full_name, email, created_at
            """,
            (full_name.strip(), email.lower().strip(), password_hash, salt)
        )
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            raise ValueError(f"Email '{email}' sudah terdaftar")
        logger.error(f"[auth_db] create_user error: {e}")
        raise

    if not row:
        raise RuntimeError("INSERT berhasil tapi tidak mengembalikan baris")

    return {
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def verify_user(email: str, password: str) -> Optional[Dict]:
    """
    Verifikasi email + password.
    Return : dict user jika cocok, None jika tidak.
    """
    row = execute_one(
        """
        SELECT id, full_name, email, password_hash, salt, created_at, last_login
        FROM users
        WHERE email = %s
        """,
        (email.lower().strip(),)
    )
    if not row:
        return None

    if _hash_password(password, row["salt"]) != row["password_hash"]:
        return None

    last_login = update_last_login(row["id"])

    return {
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": last_login,
    }


def get_user_by_email(email: str) -> Optional[Dict]:
    """
    Ambil user berdasarkan email (tanpa password info).
    Return : dict user, atau None jika tidak ada.
    """
    row = execute_one(
        """
        SELECT id, full_name, email, created_at, last_login
        FROM users
        WHERE email = %s
        """,
        (email.lower().strip(),)
    )
    if not row:
        return None
    return {
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": row["last_login"].isoformat() if row["last_login"] else None,
    }


def update_last_login(user_id: int) -> Optional[str]:
    """
    Update kolom last_login ke waktu sekarang (UTC).
    Dipakai oleh verify_user dan google_auth.
    Return : ISO string last_login, atau None jika gagal.
    """
    try:
        row = execute_returning(
            "UPDATE users SET last_login = NOW() WHERE id = %s RETURNING last_login",
            (user_id,)
        )
        if row and row["last_login"]:
            return row["last_login"].isoformat()
    except Exception as e:
        logger.warning(f"[auth_db] update_last_login failed for user_id={user_id}: {e}")
    return None