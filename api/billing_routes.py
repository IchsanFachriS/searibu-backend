"""Billing Flask blueprint — DUMMY MODE.

PENTING: File ini menggantikan billing_routes.py lama.
POST /api/create-payment langsung aktifkan subscription tanpa Midtrans.
"""

import logging
import uuid
from flask import Blueprint, jsonify, request

from .billing_db import (
    PLAN_CONFIG,
    upsert_subscription,
    get_subscription,
    init_billing_db,
)
from .auth_db import get_user_by_email, create_user

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__, url_prefix="/api")

_billing_db: str | None = None
_PRO_ONLY_FEATURES = {"forecast_14d", "activity_full", "luwes_overlay"}


def setup_billing(db_path: str) -> None:
    global _billing_db
    _billing_db = db_path
    init_billing_db(db_path)
    logger.info("Billing ready (DUMMY — no Midtrans, instant activation)")


def _db() -> str:
    if _billing_db is None:
        raise RuntimeError("Billing not initialised")
    return _billing_db


def _get_or_create_user(email: str) -> int:
    user = get_user_by_email(email)
    if user:
        return user["id"]
    new_user = create_user(
        email.split("@")[0].replace(".", " ").title(),
        email,
        uuid.uuid4().hex,
    )
    return new_user["id"]


@billing_bp.route("/create-payment", methods=["POST"])
def create_payment_endpoint():
    """DUMMY: langsung aktifkan Pro tanpa payment gateway."""
    data  = request.get_json(silent=True) or {}
    plan  = data.get("plan", "")
    email = (data.get("email") or "").strip().lower()

    if plan not in PLAN_CONFIG:
        return jsonify({"error": f"Unknown plan: {list(PLAN_CONFIG)}"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400

    cfg = PLAN_CONFIG[plan]

    try:
        user_id = _get_or_create_user(email)
    except Exception as exc:
        logger.error("User resolve failed %s: %s", email, exc)
        return jsonify({"error": "Failed to resolve user account"}), 500

    try:
        sub = upsert_subscription(_db(), user_id, plan, cfg["days"])
        logger.info("DUMMY payment OK: plan=%s user=%s days=%s", plan, email, cfg["days"])
        return jsonify({
            "success":      True,
            "plan":         plan,
            "message":      "Subscription activated (demo mode)",
            "subscription": sub,
        }), 200
    except Exception as exc:
        logger.error("upsert_subscription failed %s: %s", email, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@billing_bp.route("/midtrans-webhook", methods=["POST", "OPTIONS"])
def midtrans_webhook():
    if request.method == "OPTIONS":
        return "", 200
    return jsonify({"status": "ok", "message": "dummy mode — not used"}), 200


@billing_bp.route("/subscription", methods=["GET"])
def get_subscription_endpoint():
    user_id_raw = request.args.get("user_id")
    email = (request.args.get("email") or "").strip().lower()

    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except ValueError:
            return jsonify({"error": "user_id must be integer"}), 400
    elif email:
        user = get_user_by_email(email)
        if not user:
            return jsonify({"plan": "free", "status": "active", "expires_at": None})
        user_id = user["id"]
    else:
        return jsonify({"error": "Provide user_id or email"}), 400

    return jsonify(get_subscription(_db(), user_id))


@billing_bp.route("/subscription/check-access", methods=["POST"])
def check_access():
    data    = request.get_json(silent=True) or {}
    feature = data.get("feature", "")
    email   = (data.get("email") or "").strip().lower()
    user_id = data.get("user_id")

    if not user_id and email:
        user = get_user_by_email(email)
        user_id = user["id"] if user else None

    if not user_id:
        return jsonify({"allowed": False, "reason": "Not authenticated", "plan": "free"}), 200

    sub = get_subscription(_db(), int(user_id))
    pro_active = (
        sub.get("plan") in ("pro_monthly", "pro_annual")
        and sub.get("status") == "active"
    )

    if feature in _PRO_ONLY_FEATURES and not pro_active:
        return jsonify({
            "allowed": False,
            "reason": f"'{feature}' requires Pro",
            "plan": sub.get("plan", "free"),
        })

    return jsonify({"allowed": True, "reason": "ok", "plan": sub.get("plan", "free")})