"""
Auth DB — SQLite persistence untuk user accounts.

Tabel:
  users → menyimpan data akun pengguna

Fitur:
  - Password di-hash pakai SHA-256 + salt
  - Email unik (tidak boleh duplikat)
  - Timestamps: created_at, last_login
"""

import sqlite3
import hashlib
import os
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

WIB = timezone(timedelta(hours=7))
_lock = threading.Lock()

_DDL_USERS = """
    CREATE TABLE IF NOT EXISTS users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name    TEXT NOT NULL,
        email        TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        salt         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        last_login   TEXT
    )
"""

_DDL_IDX_EMAIL = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
    ON users(email)
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_auth_db(db_path: str):
    """Buat tabel users jika belum ada."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(_DDL_USERS)
            conn.execute(_DDL_IDX_EMAIL)
            conn.commit()
        finally:
            conn.close()


def _hash_password(password: str, salt: str) -> str:
    """Hash password dengan SHA-256 + salt."""
    salted = f"{salt}{password}{salt}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def create_user(db_path: str, full_name: str, email: str, password: str) -> Dict:
    """
    Buat user baru.
    Return: dict user (tanpa password_hash & salt)
    Raises: ValueError jika email sudah terdaftar
    """
    salt = os.urandom(32).hex()
    password_hash = _hash_password(password, salt)
    now = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")

    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO users (full_name, email, password_hash, salt, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (full_name.strip(), email.lower().strip(), password_hash, salt, now))
            conn.commit()
            user_id = cursor.lastrowid
            return {
                "id": user_id,
                "full_name": full_name.strip(),
                "email": email.lower().strip(),
                "created_at": now,
            }
        except sqlite3.IntegrityError:
            raise ValueError(f"Email '{email}' sudah terdaftar")
        finally:
            conn.close()


def verify_user(db_path: str, email: str, password: str) -> Optional[Dict]:
    """
    Verifikasi login.
    Return: dict user jika berhasil, None jika gagal.
    """
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT id, full_name, email, password_hash, salt, created_at
                FROM users WHERE email = ?
            """, (email.lower().strip(),))
            row = cursor.fetchone()
            if not row:
                return None

            expected = _hash_password(password, row["salt"])
            if expected != row["password_hash"]:
                return None

            # Update last_login
            now = datetime.now(WIB).strftime("%Y-%m-%dT%H:%M:%S+07:00")
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (now, row["id"])
            )
            conn.commit()

            return {
                "id": row["id"],
                "full_name": row["full_name"],
                "email": row["email"],
                "created_at": row["created_at"],
                "last_login": now,
            }
        finally:
            conn.close()


def get_user_by_email(db_path: str, email: str) -> Optional[Dict]:
    """Ambil user by email (tanpa password info)."""
    with _lock:
        conn = _connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT id, full_name, email, created_at, last_login
                FROM users WHERE email = ?
            """, (email.lower().strip(),))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()