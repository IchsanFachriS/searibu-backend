"""
billing_routes.py — Billing endpoints (Fixed)

Fixes applied:
  1. Midtrans Snap URL: production uses 'app.midtrans.com' not 'api.midtrans.com'
  2. Webhook signature: gross_amount must be exact string from notification body
  3. _get_or_create_user: uses configured _auth_db_path instead of hardcoded path
  4. setup_billing: stores auth_db path for use across routes
  5. Added explicit OPTIONS handling for CORS preflight on webhook
  6. create-payment: validates server key exists before calling Midtrans
  7. Subscription endpoint: returns proper plan + status for frontend gating

POST /api/create-payment    → create Midtrans Snap token
POST /api/midtrans-webhook  → receive Midtrans notification (no auth required)
GET  /api/subscription      → current subscription status
POST /api/subscription/check-access → feature gate check
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
_auth_db: str | None = None
_mt_server_key: str | None = None
_mt_is_production: bool = False


def setup_billing(db_path: str):
    global _billing_db, _auth_db, _mt_server_key, _mt_is_production
    _billing_db = db_path

    # Auth DB path — derive from billing db path or env
    _auth_db = os.getenv("AUTH_DB_PATH", os.path.join(os.path.dirname(db_path), "auth.db"))

    _mt_server_key = os.getenv("MIDTRANS_SERVER_KEY", "")
    _mt_is_production = os.getenv("MIDTRANS_ENV", "sandbox").lower() == "production"

    if not _mt_server_key:
        logger.warning(
            "MIDTRANS_SERVER_KEY environment variable is not set. "
            "Payment creation will fail."
        )

    init_billing_db(db_path)
    logger.info(
        f"Billing setup: db={db_path}, env={'production' if _mt_is_production else 'sandbox'}"
    )


def _db() -> str:
    if not _billing_db:
        raise RuntimeError("Billing DB not initialised — call setup_billing() first.")
    return _billing_db


def _auth_db_path() -> str:
    if not _auth_db:
        raise RuntimeError("Auth DB path not set — call setup_billing() first.")
    return _auth_db


def _midtrans_snap_url() -> str:
    """
    Correct Midtrans Snap v1 endpoint URLs:
      Sandbox:    https://app.sandbox.midtrans.com/snap/v1/transactions
      Production: https://app.midtrans.com/snap/v1/transactions

    The old code used 'api.midtrans.com' for production which is WRONG.
    Both environments use 'app.*' subdomain for Snap.
    """
    if _mt_is_production:
        return "https://app.midtrans.com/snap/v1/transactions"
    return "https://app.sandbox.midtrans.com/snap/v1/transactions"


def _get_or_create_user(email: str) -> int:
    """
    Return user_id, creating a guest account if the user does not yet exist.
    Uses the configured auth DB path, not a hardcoded value.
    """
    auth_db = _auth_db_path()
    user = get_user_by_email(auth_db, email)
    if user:
        return user["id"]
    # Create a minimal account so the user can log in later with Google / password
    new_user = create_user(
        auth_db,
        email.split("@")[0].replace(".", " ").title(),
        email,
        uuid.uuid4().hex,   # random password — user will authenticate via Google
    )
    return new_user["id"]


# ── POST /api/create-payment ──────────────────────────────────

@billing_bp.route("/create-payment", methods=["POST"])
def create_payment_endpoint():
    """
    Create a Midtrans Snap payment token.

    Body: { plan: "pro_monthly" | "pro_annual", email: str }
    Returns: { snap_token, order_id, redirect_url }
    """
    data = request.get_json(silent=True) or {}
    plan  = data.get("plan", "")
    email = (data.get("email") or "").strip().lower()

    if plan not in PLAN_CONFIG:
        return jsonify({"error": f"Unknown plan: {plan}. Must be one of: {list(PLAN_CONFIG.keys())}"}), 400

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
        # Snap will use the notification URL configured in the Midtrans dashboard.
        # You can also override it here:
        # "callbacks": {
        #     "finish": "https://searibu.vercel.app/payment/finish"
        # }
    }

    snap_url = _midtrans_snap_url()
    logger.info(f"Creating payment: order_id={order_id}, plan={plan}, email={email}, url={snap_url}")

    try:
        resp = requests.post(
            snap_url,
            json=payload,
            auth=(_mt_server_key, ""),   # Midtrans uses HTTP Basic Auth
            timeout=15,
        )

        if resp.status_code != 201:
            # Log the full Midtrans error for debugging
            logger.error(
                f"Midtrans error {resp.status_code}: {resp.text[:500]}"
            )
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

        # Persist pending payment record
        create_payment(_db(), user_id, order_id, snap_token, plan, amount)

        logger.info(f"Payment created: order_id={order_id}, snap_token={snap_token[:20]}...")
        return jsonify({
            "snap_token":   snap_token,
            "order_id":     order_id,
            "redirect_url": mt_data.get("redirect_url"),
        })

    except requests.exceptions.Timeout:
        logger.error("Midtrans request timed out")
        return jsonify({"error": "Payment gateway timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Cannot connect to Midtrans: {e}")
        return jsonify({"error": "Cannot reach payment gateway. Please try again."}), 502
    except Exception as e:
        logger.error(f"Unexpected error creating payment: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── POST /api/midtrans-webhook ────────────────────────────────

@billing_bp.route("/midtrans-webhook", methods=["POST", "OPTIONS"])
def midtrans_webhook():
    """
    Receive payment status notifications from Midtrans.

    Midtrans sends a JSON POST to this URL when a transaction status changes.
    This URL must be registered in the Midtrans Dashboard:
      Settings → Configuration → Payment Notification URL

    Signature verification:
      SHA512( order_id + status_code + gross_amount + server_key )
      gross_amount is the EXACT STRING from the notification body (e.g. "39000.00")

    Returns 200 for all valid requests (Midtrans will retry on non-200).
    """
    # Handle CORS preflight (in case Midtrans sends OPTIONS)
    if request.method == "OPTIONS":
        return "", 200

    body = request.get_json(silent=True) or {}
    raw  = json.dumps(body)

    order_id           = body.get("order_id", "")
    transaction_status = body.get("transaction_status", "")
    fraud_status       = body.get("fraud_status", "accept")
    midtrans_id        = body.get("transaction_id", "")
    payment_type       = body.get("payment_type", "")
    gross_amount       = body.get("gross_amount", "0")   # keep as string
    status_code        = body.get("status_code", "")
    signature_key      = body.get("signature_key", "")

    logger.info(
        f"Webhook received: order_id={order_id}, "
        f"status={transaction_status}, fraud={fraud_status}"
    )

    # ── Signature verification ────────────────────────────────────────────
    # gross_amount from Midtrans is a decimal string like "39000.00"
    # We must use the EXACT string — do NOT parse to int/float first.
    if _mt_server_key and signature_key:
        raw_str  = f"{order_id}{status_code}{gross_amount}{_mt_server_key}"
        expected = hashlib.sha512(raw_str.encode("utf-8")).hexdigest()
        if signature_key.lower() != expected.lower():
            logger.warning(
                f"Webhook signature mismatch for order_id={order_id}. "
                f"Expected={expected[:20]}..., Got={signature_key[:20]}..."
            )
            # Return 403 to let Midtrans know the signature is invalid
            return jsonify({"error": "Invalid signature"}), 403
    elif not _mt_server_key:
        logger.warning("No server key configured — skipping webhook signature check")

    # ── Process transaction status ────────────────────────────────────────
    try:
        if transaction_status in ("settlement",) or (
            transaction_status == "capture" and fraud_status == "accept"
        ):
            # Payment succeeded — activate subscription
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
            logger.info(f"Payment pending: order_id={order_id}")

        elif transaction_status in ("deny", "cancel", "expire", "failure"):
            update_payment_status(_db(), order_id, transaction_status, raw)
            logger.info(f"Payment {transaction_status}: order_id={order_id}")

        else:
            logger.info(f"Unhandled transaction_status: {transaction_status}")
            update_payment_status(_db(), order_id, transaction_status, raw)

    except Exception as e:
        logger.error(f"Error processing webhook for {order_id}: {e}", exc_info=True)
        # Still return 200 so Midtrans doesn't retry unnecessarily
        return jsonify({"status": "error", "message": str(e)}), 200

    # Always return 200 — Midtrans requires this
    return jsonify({"status": "ok"})


# ── GET /api/subscription ─────────────────────────────────────

@billing_bp.route("/subscription", methods=["GET"])
def get_subscription_endpoint():
    """
    GET /api/subscription?email=<email>
    GET /api/subscription?user_id=<id>

    Returns the subscription record for the given user.
    Non-existent users get plan=free / status=active.

    Response: { plan, status, expires_at, starts_at?, user_id? }
    """
    auth_db     = _auth_db_path()
    user_id_raw = request.args.get("user_id")
    email       = (request.args.get("email") or "").strip().lower()

    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except ValueError:
            return jsonify({"error": "user_id must be an integer"}), 400

    elif email:
        user = get_user_by_email(auth_db, email)
        if not user:
            # Unknown user → free tier (don't create an account here)
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


# ── POST /api/subscription/check-access ──────────────────────

@billing_bp.route("/subscription/check-access", methods=["POST"])
def check_access():
    """
    POST { user_id, feature }
    or   POST { email, feature }

    Features: 's104_export' | 'forecast_14d' | 'activity_full' | 'luwes_overlay'

    Returns: { allowed: bool, reason: str, plan: str }
    """
    data    = request.get_json(silent=True) or {}
    feature = data.get("feature", "")
    email   = (data.get("email") or "").strip().lower()
    user_id = data.get("user_id")

    # Resolve user_id
    if not user_id and email:
        auth_db = _auth_db_path()
        user = get_user_by_email(auth_db, email)
        user_id = user["id"] if user else None

    if not user_id:
        return jsonify({
            "allowed": False,
            "reason":  "Not authenticated",
            "plan":    "free",
        }), 200  # 200 so frontend can handle gracefully

    # Pro-only features
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