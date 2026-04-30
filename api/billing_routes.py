"""Billing Flask blueprint.

Endpoints:
    POST /api/create-payment             — create a Midtrans Snap token
    POST /api/midtrans-webhook           — receive Midtrans payment notifications
    GET  /api/subscription               — return the current subscription status
    POST /api/subscription/check-access  — feature gate check
"""

import os
import json
import uuid
import hashlib
import logging
import requests
from flask import Blueprint, jsonify, request

from .billing_db import (
    PLAN_CONFIG,
    create_payment,
    settle_payment,
    update_payment_status,
    upsert_subscription,
    get_subscription,
    init_billing_db,
)
from .auth_db import get_user_by_email, create_user

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__, url_prefix="/api")

_billing_db: str | None = None
_mt_server_key: str | None = None
_mt_is_production: bool = False

_PRO_ONLY_FEATURES = {"s104_export", "forecast_14d", "activity_full", "luwes_overlay"}


def setup_billing(db_path: str) -> None:
    """Initialise billing configuration. Call once at application startup."""
    global _billing_db, _mt_server_key, _mt_is_production
    _billing_db = db_path
    _mt_server_key = os.getenv("MIDTRANS_SERVER_KEY", "")
    _mt_is_production = os.getenv("MIDTRANS_ENV", "sandbox").lower() == "production"

    if not _mt_server_key:
        logger.warning("MIDTRANS_SERVER_KEY not set — payment creation will fail")

    init_billing_db(db_path)
    logger.info("Billing module ready (env=%s)", "production" if _mt_is_production else "sandbox")


def _db() -> str:
    if _billing_db is None:
        raise RuntimeError("Billing not initialised — call setup_billing() first")
    return _billing_db


def _snap_url() -> str:
    base = "app.midtrans.com" if _mt_is_production else "app.sandbox.midtrans.com"
    return f"https://{base}/snap/v1/transactions"


def _get_or_create_user(email: str) -> int:
    user = get_user_by_email(email)
    if user:
        return user["id"]
    new_user = create_user(email.split("@")[0].replace(".", " ").title(), email, uuid.uuid4().hex)
    return new_user["id"]


@billing_bp.route("/create-payment", methods=["POST"])
def create_payment_endpoint():
    data = request.get_json(silent=True) or {}
    plan = data.get("plan", "")
    email = (data.get("email") or "").strip().lower()

    if plan not in PLAN_CONFIG:
        return jsonify({"error": f"Unknown plan '{plan}'. Valid: {list(PLAN_CONFIG)}"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required"}), 400
    if not _mt_server_key:
        return jsonify({"error": "Payment service is not configured"}), 503

    cfg = PLAN_CONFIG[plan]
    order_id = f"searibu-{uuid.uuid4().hex[:16]}"

    try:
        user_id = _get_or_create_user(email)
    except Exception as exc:
        logger.error("Failed to resolve user for %s: %s", email, exc)
        return jsonify({"error": "Failed to resolve user account"}), 500

    payload = {
        "transaction_details": {"order_id": order_id, "gross_amount": cfg["amount"]},
        "customer_details": {"email": email},
        "item_details": [{"id": plan, "price": cfg["amount"], "quantity": 1, "name": f"Searibu {plan.replace('_', ' ').title()}"}],
        "credit_card": {"secure": True},
    }

    try:
        resp = requests.post(_snap_url(), json=payload, auth=(_mt_server_key, ""), timeout=15)

        if resp.status_code != 201:
            try:
                detail = resp.json().get("error_messages", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            logger.error("Midtrans error %d: %s", resp.status_code, detail)
            return jsonify({"error": "Midtrans API error", "status_code": resp.status_code, "detail": detail}), 502

        mt_data = resp.json()
        snap_token = mt_data.get("token", "")
        if not snap_token:
            return jsonify({"error": "Midtrans did not return a payment token"}), 502

        create_payment(_db(), user_id, order_id, snap_token, plan, cfg["amount"])
        logger.info("Payment created: order_id=%s plan=%s email=%s", order_id, plan, email)
        return jsonify({"snap_token": snap_token, "order_id": order_id, "redirect_url": mt_data.get("redirect_url")})

    except requests.exceptions.Timeout:
        return jsonify({"error": "Payment gateway timed out"}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot reach payment gateway"}), 502
    except Exception as exc:
        logger.error("Unexpected payment error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@billing_bp.route("/midtrans-webhook", methods=["POST", "OPTIONS"])
def midtrans_webhook():
    if request.method == "OPTIONS":
        return "", 200

    body = request.get_json(silent=True) or {}
    raw = json.dumps(body)

    order_id = body.get("order_id", "")
    transaction_status = body.get("transaction_status", "")
    fraud_status = body.get("fraud_status", "accept")
    midtrans_id = body.get("transaction_id", "")
    payment_type = body.get("payment_type", "")
    gross_amount = body.get("gross_amount", "0")
    status_code = body.get("status_code", "")
    signature_key = body.get("signature_key", "")

    logger.info("Webhook: order_id=%s status=%s fraud=%s", order_id, transaction_status, fraud_status)

    if _mt_server_key and signature_key:
        expected = hashlib.sha512(
            f"{order_id}{status_code}{gross_amount}{_mt_server_key}".encode()
        ).hexdigest()
        if signature_key.lower() != expected.lower():
            logger.warning("Webhook signature mismatch: order_id=%s", order_id)
            return jsonify({"error": "Invalid signature"}), 403
    elif not _mt_server_key:
        logger.warning("No server key — skipping webhook signature verification")

    try:
        if transaction_status == "settlement" or (transaction_status == "capture" and fraud_status == "accept"):
            payment = settle_payment(_db(), order_id, midtrans_id, payment_type, raw)
            if payment:
                days = PLAN_CONFIG.get(payment["plan"], {}).get("days", 30)
                upsert_subscription(_db(), payment["user_id"], payment["plan"], days)
                logger.info("Subscription activated: user_id=%s plan=%s", payment["user_id"], payment["plan"])
        elif transaction_status == "pending":
            update_payment_status(_db(), order_id, "pending", raw)
        elif transaction_status in ("deny", "cancel", "expire", "failure"):
            update_payment_status(_db(), order_id, transaction_status, raw)
        else:
            update_payment_status(_db(), order_id, transaction_status, raw)
    except Exception as exc:
        logger.error("Webhook processing error for %s: %s", order_id, exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 200

    return jsonify({"status": "ok"})


@billing_bp.route("/subscription", methods=["GET"])
def get_subscription_endpoint():
    user_id_raw = request.args.get("user_id")
    email = (request.args.get("email") or "").strip().lower()

    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except ValueError:
            return jsonify({"error": "user_id must be an integer"}), 400
    elif email:
        user = get_user_by_email(email)
        if not user:
            return jsonify({"plan": "free", "status": "active", "expires_at": None})
        user_id = user["id"]
    else:
        return jsonify({"error": "Provide either user_id or email query parameter"}), 400

    return jsonify(get_subscription(_db(), user_id))


@billing_bp.route("/subscription/check-access", methods=["POST"])
def check_access():
    data = request.get_json(silent=True) or {}
    feature = data.get("feature", "")
    email = (data.get("email") or "").strip().lower()
    user_id = data.get("user_id")

    if not user_id and email:
        user = get_user_by_email(email)
        user_id = user["id"] if user else None

    if not user_id:
        return jsonify({"allowed": False, "reason": "Not authenticated", "plan": "free"}), 200

    sub = get_subscription(_db(), int(user_id))
    pro_active = sub.get("plan") in ("pro_monthly", "pro_annual") and sub.get("status") == "active"

    if feature in _PRO_ONLY_FEATURES and not pro_active:
        return jsonify({"allowed": False, "reason": f"'{feature}' requires a Pro subscription", "plan": sub.get("plan", "free")})

    return jsonify({"allowed": True, "reason": "ok", "plan": sub.get("plan", "free")})