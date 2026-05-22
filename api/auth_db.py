"""Authentication database operations (PostgreSQL).

Tables managed:
    users — id, full_name, email, password_hash, salt, role, is_admin,
            created_at, last_login
"""

import hashlib
import os
import logging
from datetime import timezone, timedelta
from typing import Optional, Dict

from .pg_db import execute_one, execute_returning

logger  = logging.getLogger(__name__)
WIB     = timezone(timedelta(hours=7))

VALID_ROLES = {"general", "researcher"}


def init_auth_db(db_path: str = None) -> None:
    logger.debug("auth_db: PostgreSQL mode — schema managed via migration SQL")


def setup_auth(db_path: str = None) -> None:
    pass


def _hash_password(password: str, salt: str) -> str:
    salted = f"{salt}{password}{salt}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def _row_to_user(row) -> Dict:
    """Convert a DB row to a clean user dict."""
    return {
        "id":         row["id"],
        "full_name":  row["full_name"],
        "email":      row["email"],
        "role":       row["role"]     or "general",
        "is_admin":   bool(row["is_admin"]) if row["is_admin"] is not None else False,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login": row["last_login"].isoformat() if row.get("last_login") else None,
    }


def create_user(
    full_name: str,
    email:     str,
    password:  str,
    role:      str = "general",
) -> Dict:
    """Insert a new user and return their record.

    Raises:
        ValueError: if the email address is already registered.
    """
    if role not in VALID_ROLES:
        role = "general"

    salt          = os.urandom(32).hex()
    password_hash = _hash_password(password, salt)

    try:
        row = execute_returning(
            """
            INSERT INTO users (full_name, email, password_hash, salt, role)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, full_name, email, role, is_admin, created_at
            """,
            (full_name.strip(), email.lower().strip(), password_hash, salt, role),
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "unique" in msg or "duplicate" in msg:
            raise ValueError(f"Email '{email}' is already registered") from exc
        raise

    if not row:
        raise RuntimeError("INSERT succeeded but returned no row")

    return _row_to_user(row)


def verify_user(email: str, password: str) -> Optional[Dict]:
    """Verify credentials and return the user record, or None on failure."""
    row = execute_one(
        """
        SELECT id, full_name, email, password_hash, salt,
               role, is_admin, created_at, last_login
        FROM users WHERE email = %s
        """,
        (email.lower().strip(),),
    )
    if not row:
        return None
    if _hash_password(password, row["salt"]) != row["password_hash"]:
        return None

    last_login = update_last_login(row["id"])
    user = _row_to_user(row)
    user["last_login"] = last_login
    return user


def get_user_by_email(email: str) -> Optional[Dict]:
    """Return a user record by email, or None if not found."""
    row = execute_one(
        """
        SELECT id, full_name, email, role, is_admin, created_at, last_login
        FROM users WHERE email = %s
        """,
        (email.lower().strip(),),
    )
    return _row_to_user(row) if row else None


def update_last_login(user_id: int) -> Optional[str]:
    """Update last_login and return its ISO string."""
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