"""Authentication database operations (PostgreSQL).

Tables managed:
    users — id, full_name, email, password_hash, salt, created_at, last_login

Schema is created via migrations/001_initial_schema.sql; this module only
performs DML (SELECT / INSERT / UPDATE).
"""

import hashlib
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from .pg_db import execute_one, execute_returning, execute_write

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))


def init_auth_db(db_path: str = None) -> None:
    """No-op — kept for backward-compatibility with app.py startup sequence."""
    logger.debug("auth_db: PostgreSQL mode — schema managed via migration SQL")


def setup_auth(db_path: str = None) -> None:
    """No-op — kept for backward-compatibility with app.py startup sequence."""


def _hash_password(password: str, salt: str) -> str:
    salted = f"{salt}{password}{salt}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def create_user(full_name: str, email: str, password: str) -> Dict:
    """Insert a new user and return their record.

    Raises:
        ValueError: if the email address is already registered.
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
            (full_name.strip(), email.lower().strip(), password_hash, salt),
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "unique" in msg or "duplicate" in msg:
            raise ValueError(f"Email '{email}' is already registered") from exc
        raise

    if not row:
        raise RuntimeError("INSERT succeeded but returned no row")

    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def verify_user(email: str, password: str) -> Optional[Dict]:
    """Verify credentials and return the user record, or None on failure."""
    row = execute_one(
        """
        SELECT id, full_name, email, password_hash, salt, created_at, last_login
        FROM users WHERE email = %s
        """,
        (email.lower().strip(),),
    )
    if not row:
        return None
    if _hash_password(password, row["salt"]) != row["password_hash"]:
        return None

    last_login = update_last_login(row["id"])
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": last_login,
    }


def get_user_by_email(email: str) -> Optional[Dict]:
    """Return a user record by email address, or None if not found."""
    row = execute_one(
        "SELECT id, full_name, email, created_at, last_login FROM users WHERE email = %s",
        (email.lower().strip(),),
    )
    if not row:
        return None
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": row["last_login"].isoformat() if row["last_login"] else None,
    }


def update_last_login(user_id: int) -> Optional[str]:
    """Update the last_login timestamp for a user and return its ISO string."""
    try:
        row = execute_returning(
            "UPDATE users SET last_login = NOW() WHERE id = %s RETURNING last_login",
            (user_id,),
        )
        if row and row["last_login"]:
            return row["last_login"].isoformat()
    except Exception as exc:
        logger.warning("update_last_login failed for user_id=%d: %s", user_id, exc)
    return None