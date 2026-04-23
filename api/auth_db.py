"""
auth_db.py — PostgreSQL version
Menggantikan implementasi SQLite sebelumnya.

Semua operasi database menggunakan pg_db.py (psycopg2 + connection pool).
API publik identik dengan versi SQLite sehingga auth_routes.py tidak perlu diubah.
"""

import hashlib
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from .pg_db import get_cursor, execute_one, execute_returning

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

# ── Fungsi ini sekarang no-op karena tabel dibuat via migration SQL ──────────
def init_auth_db(db_path: str = None):
    """
    Tidak melakukan apa-apa di versi PostgreSQL.
    Tabel sudah dibuat via migrations/001_initial_schema.sql di Supabase.
    Parameter db_path dipertahankan agar pemanggil lama (app.py) tidak error.
    """
    logger.info("[auth_db] PostgreSQL mode — tabel sudah ada via migration SQL")


# ── Fungsi ini juga no-op ─────────────────────────────────────────────────────
def setup_auth(db_path: str = None):
    """No-op di PostgreSQL mode. Dipanggil dari app.py, tidak melakukan apa-apa."""
    pass


def _hash_password(password: str, salt: str) -> str:
    """Hash password dengan SHA-256 + salt (identik dengan versi SQLite)."""
    salted = f"{salt}{password}{salt}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def create_user(db_path: str = None, full_name: str = "", email: str = "", password: str = "") -> Dict:
    """
    Buat user baru.
    Return: dict user (tanpa password_hash & salt)
    Raises: ValueError jika email sudah terdaftar
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
        return {
            "id":         row["id"],
            "full_name":  row["full_name"],
            "email":      row["email"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    except Exception as e:
        # psycopg2 raise UniqueViolation (subclass IntegrityError) jika duplicate
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise ValueError(f"Email '{email}' sudah terdaftar")
        raise


def verify_user(db_path: str = None, email: str = "", password: str = "") -> Optional[Dict]:
    """
    Verifikasi login.
    Return: dict user jika berhasil, None jika gagal.
    """
    row = execute_one(
        """
        SELECT id, full_name, email, password_hash, salt, created_at, last_login
        FROM users WHERE email = %s
        """,
        (email.lower().strip(),)
    )
    if not row:
        return None

    expected = _hash_password(password, row["salt"])
    if expected != row["password_hash"]:
        return None

    # Update last_login
    with get_cursor() as cur:
        cur.execute(
            "UPDATE users SET last_login = NOW() WHERE id = %s RETURNING last_login",
            (row["id"],)
        )
        updated = cur.fetchone()

    last_login = updated["last_login"].isoformat() if updated and updated["last_login"] else None

    return {
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": last_login,
    }


def get_user_by_email(db_path: str = None, email: str = "") -> Optional[Dict]:
    """Ambil user by email (tanpa password info)."""
    row = execute_one(
        """
        SELECT id, full_name, email, created_at, last_login
        FROM users WHERE email = %s
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