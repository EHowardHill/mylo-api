# routes/push.py
# Web Push notification endpoints — subscribe, unsubscribe, VAPID key

from flask import Blueprint, request, session, jsonify
import datetime

from utils.shared_api import db, get_user_by_email, get_vapid_keys

push_bp = Blueprint("push", __name__)


def require_auth(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if "email" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        user = get_user_by_email(session["email"])
        if not user:
            session.clear()
            return jsonify({"error": "Unauthorized"}), 401
        return f(current_user=user, *args, **kwargs)

    return decorated


@push_bp.route("/vapid-key", methods=["GET"])
def get_vapid_public_key():
    """Return the VAPID public key so the client can subscribe."""
    keys = get_vapid_keys()
    if not keys:
        return (
            jsonify(
                {
                    "error": "Push notifications not configured. Set MYLO_VAPID_PUBLIC_KEY and MYLO_VAPID_PRIVATE_KEY environment variables."
                }
            ),
            503,
        )
    return jsonify({"public_key": keys["public"]})


@push_bp.route("/vapid_public_key", methods=["GET"])
def vapid_check():
    """
    Diagnostic endpoint — visit /api/push/vapid_public_key in your browser
    to verify VAPID keys are correctly configured.
    """
    import base64

    keys = get_vapid_keys()
    if not keys:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "MYLO_VAPID_PUBLIC_KEY and/or MYLO_VAPID_PRIVATE_KEY not set",
                    "hint": "Run: python scripts/generate_vapid_keys.py, then export the keys as env vars and restart the app.",
                }
            ),
            503,
        )

    pub = keys["public"]
    priv = keys["private"]
    problems = []

    # Check public key
    try:
        # Add back padding for decoding
        raw_pub = base64.urlsafe_b64decode(pub + "==")
        if len(raw_pub) != 65:
            problems.append(f"Public key decodes to {len(raw_pub)} bytes, expected 65")
        elif raw_pub[0] != 0x04:
            problems.append(
                f"Public key first byte is 0x{raw_pub[0]:02x}, expected 0x04 (uncompressed point)"
            )
    except Exception as e:
        problems.append(f"Public key base64url decode failed: {e}")

    # Check private key
    try:
        raw_priv = base64.urlsafe_b64decode(priv + "==")
        if len(raw_priv) != 32:
            problems.append(
                f"Private key decodes to {len(raw_priv)} bytes, expected 32"
            )
    except Exception as e:
        problems.append(f"Private key base64url decode failed: {e}")

    # Check claims email
    email = keys.get("claims_email", "")
    if not email.startswith("mailto:"):
        problems.append(
            f"MYLO_VAPID_CLAIMS_EMAIL should start with 'mailto:', got: {email!r}"
        )

    if problems:
        return (
            jsonify(
                {
                    "ok": False,
                    "public_key_length": len(pub),
                    "problems": problems,
                    "hint": "Regenerate keys with: python scripts/generate_vapid_keys.py",
                }
            ),
            400,
        )

    return jsonify(
        {
            "ok": True,
            "public_key_length": len(pub),
            "public_key_decoded_bytes": 65,
            "private_key_decoded_bytes": 32,
            "claims_email": email,
            "message": "VAPID keys look correct.",
        }
    )


@push_bp.route("/subscribe", methods=["POST"])
@require_auth
def subscribe(current_user):
    """Store or update a push subscription for the current user."""
    data = request.get_json()
    subscription = data.get("subscription")

    if not subscription or not subscription.get("endpoint"):
        return jsonify({"error": "Invalid subscription"}), 400

    endpoint = subscription["endpoint"]
    user_agent = data.get("user_agent", "")
    platform = data.get("platform", "unknown")

    # Upsert: replace if same endpoint exists, otherwise insert
    db["push_subscriptions"].update_one(
        {"endpoint": endpoint},
        {
            "$set": {
                "user_id": current_user["_id"],
                "endpoint": endpoint,
                "keys": subscription.get("keys", {}),
                "expiration_time": subscription.get("expirationTime"),
                "user_agent": user_agent,
                "platform": platform,
                "updated_at": datetime.datetime.utcnow(),
            },
            "$setOnInsert": {
                "created_at": datetime.datetime.utcnow(),
            },
        },
        upsert=True,
    )

    return jsonify({"success": True})


@push_bp.route("/unsubscribe", methods=["POST"])
@require_auth
def unsubscribe(current_user):
    """Remove a push subscription."""
    data = request.get_json()
    endpoint = data.get("endpoint")

    if not endpoint:
        return jsonify({"error": "Endpoint required"}), 400

    db["push_subscriptions"].delete_one(
        {"endpoint": endpoint, "user_id": current_user["_id"]}
    )

    return jsonify({"success": True})


@push_bp.route("/test", methods=["POST"])
@require_auth
def test_push(current_user):
    """Send a test push notification to the current user."""
    from app import send_push_to_user_task

    send_push_to_user_task.delay(
        user_id_str=str(current_user["_id"]),
        title="Mylo",
        body="Push notifications are working! 🎉",
        tag="mylo-test",
        url="./",
    )

    return jsonify({"success": True, "message": "Task queued"})


@push_bp.route("/status", methods=["GET"])
@require_auth
def push_status(current_user):
    """Return the user's push subscription count and status."""
    count = db["push_subscriptions"].count_documents({"user_id": current_user["_id"]})
    keys = get_vapid_keys()
    return jsonify(
        {
            "subscriptions": count,
            "push_configured": keys is not None,
        }
    )


@push_bp.route("/fcm/token", methods=["POST"])
@require_auth
def save_fcm_token(current_user):
    """Store or update an FCM token from a native mobile client."""
    data = request.get_json()
    token = data.get("token")

    if not token:
        return jsonify({"error": "Token required"}), 400

    platform = data.get("platform", "unknown")

    # Upsert the token
    db["fcm_tokens"].update_one(
        {"token": token},
        {
            "$set": {
                "user_id": current_user["_id"],
                "token": token,
                "platform": platform,
                "updated_at": datetime.datetime.utcnow(),
            },
            "$setOnInsert": {
                "created_at": datetime.datetime.utcnow(),
            },
        },
        upsert=True,
    )

    return jsonify({"success": True})
