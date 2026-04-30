"""
Endpoints:
  POST /api/create-payment              → buat Midtrans Snap token
  POST /api/midtrans-webhook            → terima notifikasi Midtrans
  GET  /api/subscription                → status subscription user
  POST /api/subscription/check-access  → feature gate check
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

    _mt_server_key    = os.getenv("MIDTRANS_SERVER_KEY", "")
    _mt_is_production = os.getenv("MIDTRANS_ENV", "sandbox").lower() == "production"

    if not _mt_server_key:
        logger.warning(
            "MIDTRANS_SERVER_KEY tidak di-set. Payment creation akan gagal."
        )

    init_billing_db(db_path)
    logger.info(
        f"Billing setup: env={'production' if _mt_is_production else 'sandbox'}"
    )


def _db() -> str:
    if not _billing_db:
        raise RuntimeError("Billing DB not initialised — call setup_billing() first.")
    return _billing_db


def _midtrans_snap_url() -> str:
    if _mt_is_production:
        return "https://app.midtrans.com/snap/v1/transactions"
    return "https://app.sandbox.midtrans.com/snap/v1/transactions"


def _get_or_create_user(email: str) -> int:
    """
    Ambil user_id berdasarkan email, atau buat akun guest jika belum ada.
    Langsung ke PostgreSQL via auth_db (tanpa db_path).
    """
    user = get_user_by_email(email)
    if user:
        return user["id"]

    # Buat akun minimal agar user bisa login kemudian via Google / password
    new_user = create_user(
        email.split("@")[0].replace(".", " ").title(),
        email,
        uuid.uuid4().hex,   # random password
    )
    return new_user["id"]


# ── POST /api/create-payment ──────────────────────────────────────────────────

@billing_bp.route("/create-payment", methods=["POST"])
def create_payment_endpoint():
    data  = request.get_json(silent=True) or {}
    plan  = data.get("plan", "")
    email = (data.get("email") or "").strip().lower()

    if plan not in PLAN_CONFIG:
        return jsonify({
            "error": f"Unknown plan: {plan}. Must be one of: {list(PLAN_CONFIG.keys())}"
        }), 400

    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required"}), 400

    if not _mt_server_key:
        logger.error("MIDTRANS_SERVER_KEY is not configured")
        return jsonify({
            "error": "Payment service is not configured. Please contact the administrator.",
            "hint":  "Set the MIDTRANS_SERVER_KEY environment variable."
        }), 503

    cfg      = PLAN_CONFIG[plan]
    amount   = cfg["amount"]
    order_id = f"searibu-{uuid.uuid4().hex[:16]}"

    try:
        user_id = _get_or_create_user(email)
    except Exception as e:
        logger.error(f"Failed to get/create user for {email}: {e}")
        return jsonify({"error": "Failed to resolve user account"}), 500

    payload = {
        "transaction_details": {
            "order_id":     order_id,
            "gross_amount": amount,
        },
        "customer_details": {
            "email": email,
        },
        "item_details": [
            {
                "id":       plan,
                "price":    amount,
                "quantity": 1,
                "name":     f"Searibu {plan.replace('_', ' ').title()}",
            }
        ],
        "credit_card": {
            "secure": True,
        },
    }

    snap_url = _midtrans_snap_url()
    logger.info(f"Creating payment: order_id={order_id}, plan={plan}, email={email}")

    try:
        resp = requests.post(
            snap_url,
            json=payload,
            auth=(_mt_server_key, ""),
            timeout=15,
        )

        if resp.status_code != 201:
            logger.error(f"Midtrans error {resp.status_code}: {resp.text[:500]}")
            try:
                mt_error = resp.json()
                detail = mt_error.get("error_messages", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            return jsonify({
                "error":       "Midtrans API returned an error",
                "status_code": resp.status_code,
                "detail":      detail,
            }), 502

        mt_data    = resp.json()
        snap_token = mt_data.get("token", "")

        if not snap_token:
            logger.error(f"No token in Midtrans response: {mt_data}")
            return jsonify({"error": "Midtrans did not return a payment token"}), 502

        create_payment(_db(), user_id, order_id, snap_token, plan, amount)

        logger.info(f"Payment created: order_id={order_id}")
        return jsonify({
            "snap_token":   snap_token,
            "order_id":     order_id,
            "redirect_url": mt_data.get("redirect_url"),
        })

    except requests.exceptions.Timeout:
        return jsonify({"error": "Payment gateway timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": "Cannot reach payment gateway. Please try again."}), 502
    except Exception as e:
        logger.error(f"Unexpected error creating payment: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── POST /api/midtrans-webhook ────────────────────────────────────────────────

@billing_bp.route("/midtrans-webhook", methods=["POST", "OPTIONS"])
def midtrans_webhook():
    if request.method == "OPTIONS":
        return "", 200

    body = request.get_json(silent=True) or {}
    raw  = json.dumps(body)

    order_id           = body.get("order_id", "")
    transaction_status = body.get("transaction_status", "")
    fraud_status       = body.get("fraud_status", "accept")
    midtrans_id        = body.get("transaction_id", "")
    payment_type       = body.get("payment_type", "")
    gross_amount       = body.get("gross_amount", "0")
    status_code        = body.get("status_code", "")
    signature_key      = body.get("signature_key", "")

    logger.info(
        f"Webhook: order_id={order_id}, status={transaction_status}, fraud={fraud_status}"
    )

    # Verifikasi signature
    if _mt_server_key and signature_key:
        raw_str  = f"{order_id}{status_code}{gross_amount}{_mt_server_key}"
        expected = hashlib.sha512(raw_str.encode("utf-8")).hexdigest()
        if signature_key.lower() != expected.lower():
            logger.warning(f"Webhook signature mismatch: order_id={order_id}")
            return jsonify({"error": "Invalid signature"}), 403
    elif not _mt_server_key:
        logger.warning("No server key — skipping webhook signature check")

    try:
        if transaction_status in ("settlement",) or (
            transaction_status == "capture" and fraud_status == "accept"
        ):
            payment = settle_payment(_db(), order_id, midtrans_id, payment_type, raw)
            if payment:
                cfg  = PLAN_CONFIG.get(payment["plan"], {})
                days = cfg.get("days", 30)
                upsert_subscription(_db(), payment["user_id"], payment["plan"], days)
                logger.info(
                    f"Subscription activated: user_id={payment['user_id']}, "
                    f"plan={payment['plan']}, days={days}"
                )
            else:
                logger.warning(f"settle_payment returned None for order_id={order_id}")

        elif transaction_status == "pending":
            update_payment_status(_db(), order_id, "pending", raw)

        elif transaction_status in ("deny", "cancel", "expire", "failure"):
            update_payment_status(_db(), order_id, transaction_status, raw)

        else:
            update_payment_status(_db(), order_id, transaction_status, raw)

    except Exception as e:
        logger.error(f"Error processing webhook for {order_id}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 200

    return jsonify({"status": "ok"})


# ── GET /api/subscription ─────────────────────────────────────────────────────

@billing_bp.route("/subscription", methods=["GET"])
def get_subscription_endpoint():
    user_id_raw = request.args.get("user_id")
    email       = (request.args.get("email") or "").strip().lower()

    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except ValueError:
            return jsonify({"error": "user_id must be an integer"}), 400

    elif email:
        user = get_user_by_email(email)
        if not user:
            return jsonify({
                "plan":       "free",
                "status":     "active",
                "expires_at": None,
            })
        user_id = user["id"]

    else:
        return jsonify({"error": "Provide either user_id or email query parameter"}), 400

    sub = get_subscription(_db(), user_id)
    return jsonify(sub)


# ── POST /api/subscription/check-access ──────────────────────────────────────

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
        return jsonify({
            "allowed": False,
            "reason":  "Not authenticated",
            "plan":    "free",
        }), 200

    pro_only = {"s104_export", "forecast_14d", "activity_full", "luwes_overlay"}
    sub = get_subscription(_db(), int(user_id))
    pro_active = (
        sub.get("plan") in ("pro_monthly", "pro_annual")
        and sub.get("status") == "active"
    )

    if feature in pro_only and not pro_active:
        return jsonify({
            "allowed": False,
            "reason":  f"Feature '{feature}' requires an active Pro subscription",
            "plan":    sub.get("plan", "free"),
        })

    return jsonify({
        "allowed": True,
        "reason":  "ok",
        "plan":    sub.get("plan", "free"),
    })