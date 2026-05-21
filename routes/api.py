# routes/api.py

from flask import Blueprint, request, session, jsonify
from bson.objectid import ObjectId
import os
import datetime
import re
import json
from utils.composite import generate_composite_icon

from utils.shared_api import (
    db,
    STATIC_WEB_URL,
    get_ignored_user_ids,
    is_image_file,
    create_user,
    verify_user,
    get_user_details,
    get_user_by_email,
    get_user_by_handle,
    get_user_by_id,
    get_member,
    member_has_any_tag,
    can_access_channel,
    fix_photo_path,
    serialize_user,
    create_notification,
    render_markdown_lite,
    serialize_reactions,
    mark_context_read,
    join_circle_by_invite,
    is_user_banned,
    is_user_muted,
    get_mute_remaining,
    is_blocked,
    create_password_reset_token,
    send_reset_email,
    verify_and_reset_password,
    limiter,
    login_required,
    to_isoformat,
    get_ignored_user_ids,
    process_upload,
    delete_upload,
    PROCESSING_RAW,
)

from utils.encryption import encrypt_text, decrypt_if_encrypted

api_bp = Blueprint("api", __name__)

# ====================================================================
# Helper: process an uploaded file, return metadata dict or None
# ====================================================================


def _serialize_file_fields(msg):
    """
    Build file/image fields for a message API response dict.
    Handles both new-style (file_url/file_name/file_size) and
    legacy (image_url only) messages.
    """
    result = {
        "image_url": None,
        "file_url": None,
        "file_name": None,
        "file_size": None,
    }

    # New-style file fields
    if msg.get("file_url"):
        full_url = f"{STATIC_WEB_URL}/uploads/{msg['file_url']}"
        result["file_url"] = full_url
        result["file_name"] = msg.get("file_name", msg["file_url"])
        result["file_size"] = msg.get("file_size")

        if is_image_file(msg["file_url"]):
            result["image_url"] = full_url

    # Legacy image_url (for old messages that only have image_url)
    elif msg.get("image_url"):
        stored = msg["image_url"]
        full_url = (
            f"{STATIC_WEB_URL}/uploads/{stored}"
            if not stored.startswith("http")
            else stored
        )
        result["image_url"] = full_url
        result["file_url"] = full_url
        result["file_name"] = os.path.basename(stored)
        result["file_size"] = None

    return result


def _format_file_size(size_bytes):
    """Format file size in human-readable form."""
    if size_bytes is None:
        return None
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def require_circle_member(f):
    """Requires auth AND membership in the circle identified by <circle_id>."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        from app import load_current_user

        user = load_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        circle_id = kwargs.get("circle_id") or request.view_args.get("circle_id")
        mem = get_member(circle_id, user["_id"])
        if not mem:
            return jsonify({"error": "Not a member of this circle"}), 403
        return f(current_user=user, membership=mem, *args, **kwargs)

    return decorated


# ====================================================================
# Encryption helpers for messages
# ====================================================================


def _decrypt_message_content(msg):
    """Decrypt the content field of a channel or DM message document."""
    return decrypt_if_encrypted(msg.get("content", ""), msg.get("encrypted", False))


def _encrypt_system_message(text):
    """Encrypt a system-generated message (group DM events)."""
    return encrypt_text(text)


# ====================================================================
# AUTHENTICATION
# ====================================================================


@api_bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute")
def login_api():
    data = request.get_json() if request.is_json else request.form
    user = verify_user(data.get("email", "").lower(), data.get("password", ""))
    if user:
        session.permanent = True
        session["email"] = user["email"]
        session["user_handle"] = user["user_handle"]

        if "pending_invite" in session:
            join_circle_by_invite(session["pending_invite"], user["_id"])
            session.pop("pending_invite", None)

        return jsonify({"success": True, "user": serialize_user(user)})
    return jsonify({"success": False, "error": "Invalid credentials"}), 401


@api_bp.route("/logout", methods=["POST"])
def logout_api():
    session.clear()
    return jsonify({"success": True})


@api_bp.route("/register", methods=["POST"])
@limiter.limit("3 per hour")
def register_api():
    data = request.get_json() if request.is_json else request.form
    email = data.get("email", "").lower()

    if create_user(
        email,
        data.get("password", ""),
        data.get("handle", ""),
        data.get("fullname", ""),
    ):
        session.permanent = True
        session["email"] = email
        session["user_handle"] = data.get("handle")

        if "pending_invite" in session:
            new_user = get_user_by_email(email)
            if new_user:
                join_circle_by_invite(session["pending_invite"], new_user["_id"])
            session.pop("pending_invite", None)

        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Email or handle taken"}), 409


@api_bp.route("/me", methods=["GET"])
@login_required
def get_me(current_user):
    return jsonify(serialize_user(current_user))


# ====================================================================
# MESSAGES
# ====================================================================


def _check_mute_and_slowmode(current_user, channel):
    """
    Returns an error response tuple if the user is muted or in slowmode cooldown.
    Returns None if they are allowed to post.
    """
    circle_id = channel["circle_id"]

    mute = is_user_muted(circle_id, current_user["_id"])
    if mute:
        remaining = get_mute_remaining(mute)
        reason = mute.get("reason", "")
        msg = f"You are muted in this circle ({remaining} remaining)"
        if reason:
            msg += f". Reason: {reason}"
        return jsonify({"error": msg}), 403

    slowmode = channel.get("slowmode_seconds", 0)
    if slowmode and slowmode > 0:
        if not member_has_any_tag(
            circle_id, current_user["_id"], ["owner", "admin", "moderator"]
        ):
            last_msg = db["messages"].find_one(
                {
                    "channel_id": channel["_id"],
                    "author_id": current_user["_id"],
                },
                sort=[("created_at", -1)],
            )
            if last_msg:
                elapsed = (
                    datetime.datetime.utcnow() - last_msg["created_at"]
                ).total_seconds()
                if elapsed < slowmode:
                    wait = int(slowmode - elapsed)
                    return (
                        jsonify(
                            {
                                "error": f"Slowmode active. Please wait {wait}s before sending another message.",
                                "slowmode_wait": wait,
                            }
                        ),
                        429,
                    )
    return None


@api_bp.route("/channels/<channel_id>/messages", methods=["GET"])
@login_required
def get_messages(current_user, channel_id):
    channel = db["channels"].find_one({"_id": ObjectId(channel_id)})
    if not channel:
        return jsonify({"error": "Not found"}), 404
    circle_id = channel["circle_id"]
    if not can_access_channel(channel, current_user["_id"], circle_id):
        return jsonify({"error": "No access"}), 403

    mark_context_read(current_user["_id"], channel_id)

    before = request.args.get("before")
    limit = min(int(request.args.get("limit", 50)), 100)
    query = {"channel_id": ObjectId(channel_id)}

    ignored_ids = get_ignored_user_ids(current_user["_id"])
    if ignored_ids:
        query["author_id"] = {"$nin": ignored_ids}

    if request.args.get("pinned") == "true":
        query["pinned"] = True
    elif before:
        try:
            # Try to parse as an ObjectId first (from our new infinite scroll)
            query["_id"] = {"$lt": ObjectId(before)}
        except Exception:
            # Fallback just in case older frontend code sends an ISO string
            try:
                query["created_at"] = {
                    "$lt": datetime.datetime.fromisoformat(
                        before.replace("Z", "+00:00")
                    )
                }
            except Exception:
                pass

    messages = list(db["messages"].find(query).sort("created_at", -1).limit(limit))
    messages.reverse()

    user_ids = set()
    reply_ids = set()
    for m in messages:
        user_ids.add(m["author_id"])
        if m.get("reply_to"):
            reply_ids.add(m["reply_to"])
        if m.get("comments"):
            user_ids.add(m["comments"][-1]["user_id"])

    users = {u["_id"]: u for u in db["emails"].find({"_id": {"$in": list(user_ids)}})}
    replied_msgs = {
        m["_id"]: m for m in db["messages"].find({"_id": {"$in": list(reply_ids)}})
    }
    reply_users = {
        u["_id"]: u
        for u in db["emails"].find(
            {"_id": {"$in": [m["author_id"] for m in replied_msgs.values()]}}
        )
    }

    # Fetch custom emojis to render in markdown
    custom_emojis = list(db["custom_emojis"].find({"circle_id": circle_id}))

    result = []
    for m in messages:
        author = users.get(m["author_id"])
        file_fields = _serialize_file_fields(m)

        # ── Decrypt content ──
        plaintext = _decrypt_message_content(m)

        comments = m.get("comments", [])
        last_reply = None
        if comments:
            lc = comments[-1]
            lc_author = users.get(lc["user_id"])
            lc_plaintext = decrypt_if_encrypted(
                lc.get("text", ""), lc.get("encrypted", False)
            )

            last_reply = {
                "author_name": lc_author["user_full_name"] if lc_author else "Unknown",
                "author_photo": (
                    fix_photo_path(lc_author.get("photo_url", "no-icon.jpg"))
                    if lc_author
                    else fix_photo_path("no-icon.jpg")
                ),
                "text": (
                    lc_plaintext[:60] + "..."
                    if len(lc_plaintext) > 60
                    else lc_plaintext
                ),
                "timestamp": to_isoformat(lc.get("timestamp")),
            }

        msg_obj = {
            "id": str(m["_id"]),
            "content": plaintext,
            "content_html": render_markdown_lite(plaintext, custom_emojis),
            "image_url": file_fields["image_url"],
            "file_url": file_fields["file_url"],
            "file_name": file_fields["file_name"],
            "file_size": file_fields["file_size"],
            "author": (
                serialize_user(author) if author else get_user_details(m["author_id"])
            ),
            "created_at": to_isoformat(m["created_at"]),
            "edited": m.get("edited", False),
            "pinned": m.get("pinned", False),
            "is_system": m.get("is_system", False),
            "reactions": serialize_reactions(
                m.get("reactions", []), current_user["_id"]
            ),
            "plus_oners": [str(uid) for uid in m.get("plus_oners", [])],
            "comments_count": len(m.get("comments", [])),
            "last_reply": last_reply,
        }
        if m.get("reply_to") and m["reply_to"] in replied_msgs:
            orig = replied_msgs[m["reply_to"]]
            orig_author = reply_users.get(orig["author_id"])
            orig_plaintext = _decrypt_message_content(orig)
            msg_obj["reply_to"] = {
                "id": str(orig["_id"]),
                "content": orig_plaintext,
                "author_name": (
                    orig_author["user_full_name"] if orig_author else "Unknown"
                ),
            }
        result.append(msg_obj)
    return jsonify(result)


@api_bp.route("/channels/<channel_id>/messages", methods=["POST"])
@login_required
def send_message(current_user, channel_id):
    channel = db["channels"].find_one({"_id": ObjectId(channel_id)})

    if not channel:
        return jsonify({"error": "Not found"}), 404
    circle_id = channel["circle_id"]
    if not can_access_channel(channel, current_user["_id"], circle_id):
        return jsonify({"error": "No access"}), 403

    if is_user_banned(circle_id, current_user["_id"]):
        return jsonify({"error": "You are banned from this circle"}), 403

    block = _check_mute_and_slowmode(current_user, channel)
    if block:
        return block

    if request.content_type and "multipart" in request.content_type:
        content = request.form.get("content", "")
        file = request.files.get("file") or request.files.get("image")
        reply_to_id = request.form.get("reply_to")
    else:
        data = request.get_json() or {}
        content = data.get("content", "")
        file = None
        reply_to_id = data.get("reply_to")

    if len(content) > 4096:
        return jsonify({"error": "Message exceeds 4096 characters"}), 400

    if not content.strip() and not file:
        return jsonify({"error": "Empty message"}), 400

    file_info = process_upload(file, "msg", current_user["_id"])

    # Keep plaintext for notifications and response, encrypt for storage
    plaintext = content.strip()
    encrypted_content = encrypt_text(plaintext)

    now = datetime.datetime.utcnow()
    msg_doc = {
        "channel_id": ObjectId(channel_id),
        "author_id": current_user["_id"],
        "content": encrypted_content,
        "encrypted": True,
        "image_url": (
            file_info["stored_filename"]
            if file_info and file_info["is_image"]
            else None
        ),
        "file_url": file_info["stored_filename"] if file_info else None,
        "file_name": file_info["original_name"] if file_info else None,
        "file_size": file_info["size"] if file_info else None,
        "created_at": now,
        "edited": False,
        "reply_to": ObjectId(reply_to_id) if reply_to_id else None,
    }
    res = db["messages"].insert_one(msg_doc)
    msg_id = res.inserted_id

    db["channels"].update_one(
        {"_id": ObjectId(channel_id)}, {"$set": {"last_message_at": now}}
    )

    # Build image URL for rich notifications
    notif_image = file_info["url"] if file_info and file_info["is_image"] else None

    if reply_to_id:
        parent = db["messages"].find_one({"_id": ObjectId(reply_to_id)})
        if parent:
            create_notification(
                parent["author_id"],
                "reply",
                f"{current_user['user_full_name']} replied to you",
                plaintext[:50],
                current_user["_id"],
                channel_id,
                image_url=notif_image,
            )

    mentions = set(re.findall(r"(?<!\w)\+([a-zA-Z0-9_]+)", plaintext))
    mentioned_ids = set()
    for handle in mentions:
        target = get_user_by_handle(handle)
        if target:
            mentioned_ids.add(target["_id"])
            create_notification(
                target["_id"],
                "mention",
                f"{current_user['user_full_name']} tagged you",
                plaintext[:50],
                current_user["_id"],
                channel_id,
                image_url=notif_image,
            )

    subscribers = db["channel_alerts"].find({"channel_id": ObjectId(channel_id)})

    parent = None
    if reply_to_id:
        parent = db["messages"].find_one({"_id": ObjectId(reply_to_id)})

    for sub in subscribers:
        uid = sub["user_id"]
        # Skip self
        if uid == current_user["_id"]:
            continue
        # Skip if they already received a mention ping
        if uid in mentioned_ids:
            continue
        # Skip if they already received a direct thread reply ping
        if reply_to_id and parent and uid == parent["author_id"]:
            continue
        # Ensure they haven't been removed from the circle since subscribing
        if not get_member(circle_id, uid):
            continue

        create_notification(
            uid,
            "channel_message",
            f"New message in #{channel['name']}",
            plaintext[:50] if plaintext else "Attachment sent.",
            current_user["_id"],
            channel_id,
            image_url=notif_image,
        )

    custom_emojis = list(db["custom_emojis"].find({"circle_id": circle_id}))

    return jsonify(
        {
            "success": True,
            "message": {
                "id": str(msg_id),
                "content": plaintext,
                "content_html": render_markdown_lite(plaintext, custom_emojis),
                "image_url": (
                    file_info["url"] if file_info and file_info["is_image"] else None
                ),
                "file_url": file_info["url"] if file_info else None,
                "file_name": file_info["original_name"] if file_info else None,
                "file_size": file_info["size"] if file_info else None,
                "author": serialize_user(current_user),
                "created_at": to_isoformat(now),
                "edited": False,
                "reply_to": {"id": reply_to_id} if reply_to_id else None,
            },
        }
    )


@api_bp.route("/channels/<channel_id>/alert", methods=["GET"])
@login_required
def get_channel_alert(current_user, channel_id):
    alert = db["channel_alerts"].find_one(
        {"user_id": current_user["_id"], "channel_id": ObjectId(channel_id)}
    )
    return jsonify({"enabled": bool(alert)})


@api_bp.route("/channels/<channel_id>/alert", methods=["POST"])
@login_required
def toggle_channel_alert(current_user, channel_id):
    channel = db["channels"].find_one({"_id": ObjectId(channel_id)})
    if not channel or not can_access_channel(
        channel, current_user["_id"], channel["circle_id"]
    ):
        return jsonify({"error": "No access"}), 403

    alert = db["channel_alerts"].find_one(
        {"user_id": current_user["_id"], "channel_id": ObjectId(channel_id)}
    )

    if alert:
        db["channel_alerts"].delete_one({"_id": alert["_id"]})
        return jsonify({"enabled": False})
    else:
        db["channel_alerts"].insert_one(
            {"user_id": current_user["_id"], "channel_id": ObjectId(channel_id)}
        )
        return jsonify({"enabled": True})


@api_bp.route("/notifications", methods=["GET"])
@login_required
def get_notifications(current_user):
    notifs = list(
        db["notifications"]
        .find({"user_id": current_user["_id"]})
        .sort("created_at", -1)
        .limit(100)
    )
    result = []
    for n in notifs:
        source = get_user_by_id(n["source_user_id"])
        result.append(
            {
                "id": str(n["_id"]),
                "type": n["type"],
                "title": n["title"],
                "body": n.get("body", ""),
                "is_read": n.get("is_read", False),
                "created_at": to_isoformat(n["created_at"]),
                "source_user": serialize_user(source) if source else None,
                "context_id": str(n["context_id"]) if n.get("context_id") else None,
            }
        )
    return jsonify(result)


@api_bp.route("/notifications/read", methods=["POST"])
@login_required
def read_notifications(current_user):
    db["notifications"].update_many(
        {"user_id": current_user["_id"], "is_read": False}, {"$set": {"is_read": True}}
    )
    return jsonify({"success": True})


@api_bp.route("/messages/<message_id>", methods=["PATCH"])
@login_required
def edit_message(current_user, message_id):
    msg = db["messages"].find_one({"_id": ObjectId(message_id)})
    if not msg:
        return jsonify({"error": "Not found"}), 404
    if msg["author_id"] != current_user["_id"]:
        return jsonify({"error": "Cannot edit another user's message"}), 403

    channel = db["channels"].find_one({"_id": msg["channel_id"]})
    if channel:
        mute = is_user_muted(channel["circle_id"], current_user["_id"])
        if mute:
            return jsonify({"error": "You are muted in this circle"}), 403

    data = request.get_json()
    new_content = (data.get("content") or "").strip()

    if len(new_content) > 4096:
        return jsonify({"error": "Message exceeds 4096 characters"}), 400

    has_attachment = bool(msg.get("file_url") or msg.get("image_url"))
    if not new_content and not has_attachment:
        return jsonify({"error": "Content required"}), 400

    # ── Encrypt edited content ──
    encrypted_content = encrypt_text(new_content)

    db["messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$set": {"content": encrypted_content, "edited": True, "encrypted": True}},
    )

    custom_emojis = (
        list(db["custom_emojis"].find({"circle_id": channel["circle_id"]}))
        if channel
        else None
    )

    return jsonify(
        {
            "success": True,
            "content": new_content,
            "content_html": render_markdown_lite(new_content, custom_emojis),
        }
    )


@api_bp.route("/messages/<message_id>", methods=["DELETE"])
@login_required
def delete_message(current_user, message_id):
    msg = db["messages"].find_one({"_id": ObjectId(message_id)})
    if not msg:
        return jsonify({"error": "Not found"}), 404
    if msg["author_id"] != current_user["_id"]:
        channel = db["channels"].find_one({"_id": msg["channel_id"]})
        if channel and not member_has_any_tag(
            channel["circle_id"], current_user["_id"], ["owner", "admin", "moderator"]
        ):
            return jsonify({"error": "Permission denied"}), 403

    for key in ("file_url", "image_url"):
        delete_upload(msg.get(key))

    db["messages"].delete_one({"_id": ObjectId(message_id)})
    return jsonify({"success": True})


@api_bp.route("/messages/<message_id>/react", methods=["POST"])
@login_required
def toggle_reaction(current_user, message_id):
    data = request.get_json()
    emoji = data.get("emoji")
    if not emoji:
        return jsonify({"error": "Emoji required"}), 400

    col = db["messages"]
    msg = col.find_one({"_id": ObjectId(message_id)})
    if not msg:
        return jsonify({"error": "Message not found"}), 404

    if not msg:
        col = db["direct_messages"]
        msg = col.find_one({"_id": ObjectId(message_id)})

    if not msg:
        return jsonify({"error": "Message not found"}), 404

    existing_reaction = None
    reactions = msg.get("reactions", [])

    for r in reactions:
        if r["emoji"] == emoji:
            existing_reaction = r
            break

    uid = current_user["_id"]

    if existing_reaction:
        if uid in existing_reaction["user_ids"]:
            col.update_one(
                {"_id": msg["_id"], "reactions.emoji": emoji},
                {"$pull": {"reactions.$.user_ids": uid}},
            )
            col.update_one(
                {
                    "_id": msg["_id"],
                    "reactions.emoji": emoji,
                    "reactions.user_ids": {"$size": 0},
                },
                {"$pull": {"reactions": {"emoji": emoji}}},
            )
        else:
            col.update_one(
                {"_id": msg["_id"], "reactions.emoji": emoji},
                {"$addToSet": {"reactions.$.user_ids": uid}},
            )
    else:
        col.update_one(
            {"_id": msg["_id"]},
            {"$push": {"reactions": {"emoji": emoji, "user_ids": [uid]}}},
        )

    updated_msg = col.find_one({"_id": ObjectId(message_id)})
    return jsonify(
        {
            "success": True,
            "reactions": serialize_reactions(updated_msg.get("reactions", []), uid),
        }
    )


@api_bp.route("/messages/<message_id>/pin", methods=["POST"])
@login_required
def toggle_pin_message(current_user, message_id):
    col = db["messages"]
    msg = col.find_one({"_id": ObjectId(message_id)})
    if not msg:
        return jsonify({"error": "Message not found"}), 404

    # Permission checks
    channel = db["channels"].find_one({"_id": msg["channel_id"]})
    if not channel or not can_access_channel(
        channel, current_user["_id"], channel["circle_id"]
    ):
        return jsonify({"error": "No access"}), 403

    is_pinned = msg.get("pinned", False)
    new_pinned = not is_pinned

    col.update_one({"_id": msg["_id"]}, {"$set": {"pinned": new_pinned}})

    sys_text = f"{current_user['user_full_name']} {'pinned' if new_pinned else 'unpinned'} a message to this channel."
    now = datetime.datetime.utcnow()
    sys_msg = {
        "author_id": current_user["_id"],
        "content": _encrypt_system_message(sys_text),
        "encrypted": True,
        "image_url": None,
        "file_url": None,
        "file_name": None,
        "file_size": None,
        "created_at": now,
        "is_system": True,
        "channel_id": msg["channel_id"],
    }

    db["messages"].insert_one(sys_msg)
    db["channels"].update_one(
        {"_id": msg["channel_id"]}, {"$set": {"last_message_at": now}}
    )

    return jsonify({"success": True, "pinned": new_pinned})


@api_bp.route("/upload", methods=["POST"])
@login_required
def upload_file(current_user):
    file = request.files.get("file")
    info = process_upload(file, "file", current_user["_id"], processing=PROCESSING_RAW)
    if not info:
        return jsonify({"error": "Invalid file"}), 400
    return jsonify({"success": True, "url": info["url"]})


# ====================================================================
# PASSWORD RESET
# ====================================================================


@api_bp.route("/auth/forgot-password", methods=["POST"])
@limiter.limit("3 per hour")
def forgot_password_api():
    data = request.get_json() if request.is_json else request.form
    email = data.get("email", "").lower()

    token = create_password_reset_token(email)
    if token:
        send_reset_email(email, token)

    return jsonify(
        {
            "success": True,
            "message": "If an account exists, a reset link has been sent.",
        }
    )


@api_bp.route("/auth/reset-password", methods=["POST"])
@limiter.limit("5 per hour")
def reset_password_api():
    data = request.get_json() if request.is_json else request.form
    token = data.get("token")
    password = data.get("password")

    if not token or not password:
        return jsonify({"success": False, "error": "Missing data"}), 400

    if verify_and_reset_password(token, password):
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "Invalid or expired token"}), 400


# ====================================================================
# GIF SEARCH (KLIPY API v2 proxy — Tenor-compatible)
# ====================================================================


def _klipy_request(endpoint, extra_params=None):
    """Make a request to the KLIPY v2 API and return normalized results."""
    import urllib.request
    import urllib.parse

    klipy_key = os.getenv("KLIPY_API_KEY")
    if not klipy_key:
        return {"error": "GIF search not configured. Set KLIPY_API_KEY env var."}, 503

    params = {
        "key": klipy_key,
        "limit": 20,
        "media_filter": "gif,tinygif",
        "client_key": "mylo",
    }
    if extra_params:
        params.update(extra_params)

    url = f"https://api.klipy.com/v2/{endpoint}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mylo/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        results = []
        for r in data.get("results", []):
            mf = r.get("media_formats", {})
            gif_url = mf.get("gif", {}).get("url", "")
            thumb_url = mf.get("tinygif", {}).get("url", "") or gif_url
            if gif_url:
                results.append({"url": gif_url, "thumb": thumb_url})

        return {"results": results}, 200
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"[GIF] KLIPY API error: {e}")


@api_bp.route("/gif/trending", methods=["GET"])
@login_required
def gif_trending(current_user):
    body, status = _klipy_request("featured")
    return jsonify(body), status


@api_bp.route("/gif/search", methods=["GET"])
@login_required
def gif_search(current_user):
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    body, status = _klipy_request("search", {"q": q})
    return jsonify(body), status


# ====================================================================
# DIRECT MESSAGES
# ====================================================================


@api_bp.route("/dm/conversations", methods=["GET"])
@login_required
def get_dm_conversations(current_user):
    """Fetch all 1-on-1 and Group DMs to populate the sidebar."""
    # 1. Get all circle IDs the user belongs to
    memberships = list(db["circle_members"].find({"user_id": current_user["_id"]}))
    circle_ids = [m["circle_id"] for m in memberships]

    # 2. Find which of those are specifically DM or Group DM circles
    circles = list(
        db["circles"]
        .find({"_id": {"$in": circle_ids}, "circle_type": {"$in": ["dm", "group_dm"]}})
        .sort("created_at", -1)
    )

    result = []
    for c in circles:

        # 3. Get the latest message to act as a preview snippet
        channel = db["channels"].find_one({"circle_id": c["_id"], "name": "chat"})
        last_message = None
        if channel:
            lm = db["messages"].find_one(
                {"channel_id": channel["_id"]}, sort=[("created_at", -1)]
            )
            if lm:
                pt = _decrypt_message_content(lm)
                last_message = {"content": pt[:60] + ("..." if len(pt) > 60 else "")}

        # 4. Gather the other members' info to display their avatars and names
        members = list(db["circle_members"].find({"circle_id": c["_id"]}))
        other_user_ids = [
            m["user_id"] for m in members if m["user_id"] != current_user["_id"]
        ]

        other_users = []
        if other_user_ids:
            for u in db["emails"].find({"_id": {"$in": other_user_ids}}):
                other_users.append(
                    {
                        "id": str(u["_id"]),
                        "name": u.get("user_full_name", "Unknown"),
                        "handle": u.get("user_handle", ""),
                        "photo_url": fix_photo_path(u.get("photo_url", "no-icon.jpg")),
                    }
                )

        display_name = c.get("name", "")
        icon_url = c.get("icon_url", "")

        if not display_name and other_users:
            names = [u["name"] for u in other_users[:4]]
            display_name = ", ".join(names)
            if len(other_users) > 4:
                display_name += f" +{len(other_users)-4}"

        if not icon_url and other_users:
            other_ids_str = [u["id"] for u in other_users]
            # Use current_user['_id'] in the cache key so it caches per-user-perspective
            icon_url = generate_composite_icon(
                other_ids_str, f"{c['_id']}_{current_user['_id']}"
            )

        doc = {
            "id": str(c["_id"]),
            "display_name": c.get("name", ""),
            "icon_url": (
                fix_photo_path(c.get("icon_url", "")) if c.get("icon_url") else ""
            ),
            "last_message": last_message,
            "member_count": len(members),
        }

        doc["other_user"] = other_users[0] if other_users else None
        result.append(doc)

    return jsonify(result)


@api_bp.route("/dm/conversations/<target_user_id>", methods=["POST"])
@login_required
def start_dm(current_user, target_user_id):
    """Create a new 1-on-1 DM Circle, or return the existing one."""
    if str(current_user["_id"]) == target_user_id:
        return jsonify({"error": "Cannot start a DM with yourself."}), 400

    target_user = get_user_by_id(target_user_id)
    if not target_user:
        return jsonify({"error": "User not found."}), 404

    if is_blocked(target_user["_id"], current_user["_id"]):
        return jsonify({"error": "You are blocked by this user."}), 403

    # Check if a DM circle already exists between these two users
    user_memberships = db["circle_members"].find({"user_id": current_user["_id"]})
    user_circle_ids = [m["circle_id"] for m in user_memberships]

    target_memberships = db["circle_members"].find(
        {"user_id": ObjectId(target_user_id), "circle_id": {"$in": user_circle_ids}}
    )
    shared_circle_ids = [m["circle_id"] for m in target_memberships]

    existing_dm = db["circles"].find_one(
        {"_id": {"$in": shared_circle_ids}, "circle_type": "dm"}
    )

    if existing_dm:
        return jsonify({"success": True, "circle": {"id": str(existing_dm["_id"])}})

    # Create the unified DM circle
    now = datetime.datetime.utcnow()

    res = db["circles"].insert_one(
        {
            "name": "",
            "icon_url": "",
            "owner_id": current_user["_id"],
            "is_public": False,
            "circle_type": "dm",
            "created_at": now,
        }
    )
    circle_id = res.inserted_id

    # Add both users
    db["circle_members"].insert_many(
        [
            {
                "circle_id": circle_id,
                "user_id": current_user["_id"],
                "tags": ["member"],
                "joined_at": now,
            },
            {
                "circle_id": circle_id,
                "user_id": ObjectId(target_user_id),
                "tags": ["member"],
                "joined_at": now,
            },
        ]
    )

    # Establish the initial chat channel
    ch_res = db["channels"].insert_one(
        {
            "circle_id": circle_id,
            "name": "chat",
            "channel_type": "chat",
            "position": 0,
            "permission_tags": [],
            "slowmode_seconds": 0,
            "created_at": now,
        }
    )
    ch_id = ch_res.inserted_id

    # Auto-enable notifications for both users in the DM
    db["channel_alerts"].insert_many(
        [
            {"user_id": current_user["_id"], "channel_id": ch_id},
            {"user_id": ObjectId(target_user_id), "channel_id": ch_id},
        ]
    )

    return jsonify({"success": True, "circle": {"id": str(circle_id)}})
