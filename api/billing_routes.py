"""
billing_routes.py — Billing endpoints

POST /api/create-payment    → create Midtrans Snap token
POST /api/midtrans-webhook  → receive Midtrans notification
GET  /api/subscription      → current subscription status
"""

import os
import json
import uuid
import hashlib
import logging
import requests
from flask import Blueprint, jsonify, request
from .billing_db import (
    PLAN_CONFIG, create_payment, settle_payment,
    update_payment_status, upsert_subscription,
    get_subscription, init_billing_db, is_pro,
)
from .auth_db import get_user_by_email, create_user

logger = logging.getLogger(__name__)
billing_bp = Blueprint("billing", __name__, url_prefix="/api")

_billing_db: str | None = None
_mt_server_key: str | None = None
_mt_is_production: bool = False


def setup_billing(db_path: str):
    global _billing_db, _mt_server_key, _mt_is_production
    _billing_db = db_path
    _mt_server_key = os.getenv("MIDTRANS_SERVER_KEY", "")
    _mt_is_production = os.getenv("MIDTRANS_ENV", "sandbox") == "production"
    init_billing_db(db_path)


def _db():
    if not _billing_db:
        raise RuntimeError("Billing DB not initialised")
    return _billing_db


def _midtrans_snap_url():
    base = "api" if _mt_is_production else "app.sandbox"
    return f"https://{base}.midtrans.com/snap/v1/transactions"


def _get_or_create_user(email: str) -> int:
    """Return user_id, creating guest account if needed."""
    from .auth_db import get_user_by_email, create_user
    auth_db = os.getenv("AUTH_DB_PATH", "/data/auth.db")
    user = get_user_by_email(auth_db, email)
    if user:
        return user["id"]
    new = create_user(auth_db, email.split("@")[0].title(),
                      email, uuid.uuid4().hex)
    return new["id"]


# ── POST /api/create-payment ──────────────────────────────────

@billing_bp.route("/create-payment", methods=["POST"])
def create_payment_endpoint():
    data = request.get_json(silent=True) or {}
    plan  = data.get("plan", "")
    email = (data.get("email") or "").strip().lower()

    if plan not in PLAN_CONFIG:
        return jsonify({"error": f"Unknown plan: {plan}"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400

    cfg      = PLAN_CONFIG[plan]
    amount   = cfg["amount"]
    order_id = f"searibu-{uuid.uuid4().hex[:12]}"
    user_id  = _get_or_create_user(email)

    payload = {
        "transaction_details": {
            "order_id":      order_id,
            "gross_amount":  amount,
        },
        "customer_details": {"email": email},
        "item_details": [{
            "id":       plan,
            "price":    amount,
            "quantity": 1,
            "name":     f"Searibu {plan.replace('_', ' ').title()}",
        }],
        "credit_card": {"secure": True},
    }

    try:
        resp = requests.post(
            _midtrans_snap_url(),
            json=payload,
            auth=(_mt_server_key, ""),
            timeout=10,
        )
        resp.raise_for_status()
        mt_data    = resp.json()
        snap_token = mt_data.get("token", "")

        create_payment(_db(), user_id, order_id, snap_token, plan, amount)
        return jsonify({
            "snap_token": snap_token,
            "order_id":   order_id,
            "redirect_url": mt_data.get("redirect_url"),
        })
    except requests.exceptions.HTTPError as e:
        logger.error(f"Midtrans error: {e.response.text}")
        return jsonify({"error": "Midtrans API error", "detail": e.response.text}), 502
    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/midtrans-webhook ────────────────────────────────

@billing_bp.route("/midtrans-webhook", methods=["POST"])
def midtrans_webhook():
    """
    Midtrans sends a notification here when transaction status changes.
    Must be registered in Midtrans dashboard → Settings → Configuration.
    """
    body = request.get_json(silent=True) or {}
    raw  = json.dumps(body)

    order_id        = body.get("order_id", "")
    transaction_status = body.get("transaction_status", "")
    fraud_status    = body.get("fraud_status", "")
    midtrans_id     = body.get("transaction_id", "")
    payment_type    = body.get("payment_type", "")
    gross_amount    = body.get("gross_amount", "0")

    # ── Verify signature ──────────────────────────────────────
    status_code     = body.get("status_code", "")
    signature_key   = body.get("signature_key", "")
    expected_sig = hashlib.sha512(
        f"{order_id}{status_code}{gross_amount}{_mt_server_key}".encode()
    ).hexdigest()
    if signature_key and signature_key != expected_sig:
        logger.warning(f"Webhook signature mismatch for {order_id}")
        return jsonify({"error": "Invalid signature"}), 403

    logger.info(f"Webhook: {order_id} → {transaction_status}/{fraud_status}")

    # ── Handle settlement ─────────────────────────────────────
    if transaction_status == "settlement" or \
       (transaction_status == "capture" and fraud_status == "accept"):
        payment = settle_payment(_db(), order_id, midtrans_id, payment_type, raw)
        if payment:
            cfg = PLAN_CONFIG.get(payment["plan"], {})
            days = cfg.get("days", 30)
            upsert_subscription(_db(), payment["user_id"], payment["plan"], days)
            logger.info(f"Subscription activated: user={payment['user_id']} plan={payment['plan']}")
    else:
        # deny / cancel / expire / pending
        update_payment_status(_db(), order_id, transaction_status, raw)

    return jsonify({"status": "ok"})


# ── GET /api/subscription ─────────────────────────────────────

@billing_bp.route("/subscription", methods=["GET"])
def get_subscription_endpoint():
    """
    GET /api/subscription?user_id=<id>
    or  GET /api/subscription?email=<email>
    """
    auth_db = os.getenv("AUTH_DB_PATH", "/data/auth.db")

    user_id_raw = request.args.get("user_id")
    email       = (request.args.get("email") or "").strip().lower()

    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except ValueError:
            return jsonify({"error": "Invalid user_id"}), 400
    elif email:
        user = get_user_by_email(auth_db, email)
        if not user:
            return jsonify({"plan": "free", "status": "active",
                            "expires_at": None})
        user_id = user["id"]
    else:
        return jsonify({"error": "user_id or email required"}), 400

    sub = get_subscription(_db(), user_id)
    return jsonify(sub)


# ── GET /api/subscription/check-access ───────────────────────

@billing_bp.route("/subscription/check-access", methods=["POST"])
def check_access():
    """
    POST { user_id, feature }
    Features: 'export' | 'forecast_14d'
    Returns { allowed: bool, reason: str }
    """
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    feature = data.get("feature", "")

    if not user_id:
        return jsonify({"allowed": False, "reason": "Not authenticated"}), 401

    pro = is_pro(_db(), int(user_id))

    pro_only = {"export", "forecast_14d"}
    if feature in pro_only and not pro:
        return jsonify({
            "allowed": False,
            "reason": f"Feature '{feature}' requires Pro subscription"
        })
    return jsonify({"allowed": True, "reason": "ok"})