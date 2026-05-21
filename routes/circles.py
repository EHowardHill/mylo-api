from celery import result
from flask import Blueprint, request, session, jsonify
from bson.objectid import ObjectId
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
import os
import datetime
import re
import pymongo
from utils.composite import generate_composite_icon

from utils.shared_api import (
    db,
    UPLOAD_FOLDER,
    STATIC_WEB_URL,
    allowed_file,
    get_user_by_email,
    create_circle,
    join_circle_by_invite,
    get_member,
    member_has_tag,
    member_has_any_tag,
    can_access_channel,
    generate_invite_code,
    fix_photo_path,
    serialize_user,
    get_user_details,
    log_mod_action,
    get_mute_remaining,
    create_notification,
    to_isoformat,
    ensure_circle_roles,
    member_has_permission,
    ROLE_PERMISSIONS,
    login_required,
    process_upload,
    delete_upload,
    PROCESSING_SQUARE,
    PROCESSING_THUMBNAIL,
    PROCESSING_WIDTH_LIMIT,
    IMAGE_EXTENSIONS,
)

circles_bp = Blueprint("circles", __name__)


# Decorators
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


def require_circle_member(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if "email" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        user = get_user_by_email(session["email"])
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        circle_id = kwargs.get("circle_id") or request.view_args.get("circle_id")
        mem = get_member(circle_id, user["_id"])
        if not mem:
            return jsonify({"error": "Not a member of this circle"}), 403
        return f(current_user=user, membership=mem, *args, **kwargs)

    return decorated


def _is_moderator(circle_id, user_id):
    """Returns True if the user has owner, admin, or moderator tag."""
    return member_has_any_tag(circle_id, user_id, ["owner", "admin", "moderator"])


# ====================================================================
# EXISTING SERVER CRUD
# ====================================================================


@circles_bp.route("/", methods=["GET"])
@require_auth
def list_circles(current_user):
    memberships = list(db["circle_members"].find({"user_id": current_user["_id"]}))
    circle_ids = [m["circle_id"] for m in memberships]
    muted_map = {m["circle_id"]: m.get("muted", False) for m in memberships}
    pinned_map = {m["circle_id"]: m.get("pinned") for m in memberships}  # None if unset

    # Pre-calculate the latest activity for each circle
    channels = list(db["channels"].find({"circle_id": {"$in": circle_ids}}))
    circle_last_msg = {}
    for ch in channels:
        sid = ch["circle_id"]
        l_msg = ch.get("last_message_at")
        if l_msg:
            if sid not in circle_last_msg or l_msg > circle_last_msg[sid]:
                circle_last_msg[sid] = l_msg

    # Note: We completely remove the "circle_type": {"$ne": "feed"} filter here!
    circles = db["circles"].find({"_id": {"$in": circle_ids}})

    result = []
    for s in circles:
        name = s.get("name", "")
        icon_url = s.get("icon_url", "")

        # Dynamically fetch others if name or icon_url is missing
        if not name or not icon_url:
            mem_docs = list(db["circle_members"].find({"circle_id": s["_id"]}))
            other_ids = [
                m["user_id"] for m in mem_docs if m["user_id"] != current_user["_id"]
            ]
            others = list(db["emails"].find({"_id": {"$in": other_ids}}))

            if not name and others:
                names = [u.get("user_full_name", "Unknown") for u in others[:4]]
                name = ", ".join(names)
                if len(others) > 4:
                    name += f" +{len(others)-4}"
                elif len(others) == 1:
                    name = others[0].get("user_full_name", "Unknown")

            if not icon_url and others:
                other_ids_str = [str(u["_id"]) for u in others]
                icon_url = generate_composite_icon(
                    other_ids_str, f"{s['_id']}_{current_user['_id']}"
                )

        # Fallback default: The Feed circle is implicitly pinned if not explicitly set to False
        pin_val = pinned_map.get(s["_id"])
        is_pinned = pin_val if pin_val is not None else (s.get("circle_type") == "feed")

        result.append(
            {
                "id": str(s["_id"]),
                "name": name,
                "icon_url": fix_photo_path(icon_url),
                "invite_code": s.get("invite_code", ""),
                "is_owner": s["owner_id"] == current_user["_id"],
                "muted": muted_map.get(s["_id"], False),
                "is_pinned": is_pinned,
                "circle_type": s.get("circle_type", "community"),
                "last_activity": (
                    to_isoformat(circle_last_msg.get(s["_id"]))
                    if circle_last_msg.get(s["_id"])
                    else None
                ),
            }
        )
    return jsonify(result)


@circles_bp.route("/<circle_id>/icon", methods=["POST"])
@require_circle_member
def upload_circle_icon(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403

    info = process_upload(
        request.files.get("file"),
        "circle",
        circle_id,
        processing=PROCESSING_SQUARE,
        target_size=512,
    )
    if not info:
        return jsonify({"error": "Invalid file"}), 400

    db["circles"].update_one(
        {"_id": ObjectId(circle_id)},
        {"$set": {"icon_url": info["stored_filename"]}},
    )
    return jsonify({"success": True, "url": info["url"]})


@circles_bp.route("/", methods=["POST"])
@require_auth
def create_circle_api(current_user):
    data = request.get_json() if request.is_json else request.form
    name = (data.get("name") or "").strip()
    is_public = bool(data.get("is_public", False))
    user_ids = data.get("user_ids", [])

    # 1. Validate targets first to build the exact intended user set
    valid_targets = []
    for uid in user_ids:
        if uid == str(current_user["_id"]):
            continue
        target = db["emails"].find_one({"_id": ObjectId(uid)})
        if target:
            valid_targets.append(target["_id"])

    intended_user_ids = set(valid_targets)
    intended_user_ids.add(current_user["_id"])

    # 2. Check for an existing circle with the exact same members
    user_memberships = list(db["circle_members"].find({"user_id": current_user["_id"]}))
    my_circle_ids = [m["circle_id"] for m in user_memberships]

    # Fetch all members of these circles in one query to save DB overhead
    all_members = list(db["circle_members"].find({"circle_id": {"$in": my_circle_ids}}))

    # Group members by their circle
    circle_members_map = {}
    for m in all_members:
        cid = m["circle_id"]
        if cid not in circle_members_map:
            circle_members_map[cid] = set()
        circle_members_map[cid].add(m["user_id"])

    # Look for an exact match
    existing_circle_id = None
    for cid, mem_ids in circle_members_map.items():
        if mem_ids == intended_user_ids:
            existing_circle_id = cid
            break

    # If an exact match is found, return it immediately
    if existing_circle_id:
        circle = db["circles"].find_one({"_id": existing_circle_id})
        return jsonify(
            {
                "success": True,
                "circle": {
                    "id": str(circle["_id"]),
                    "name": circle.get("name", ""),
                    "invite_code": circle.get("invite_code", ""),
                },
            }
        )

    # 3. If no exact match exists, proceed with standard creation
    if not name:
        return jsonify({"success": False, "error": "Name required"}), 400

    circle_id, code = create_circle(name, current_user["_id"], is_public=is_public)

    # Auto-add invited users
    for uid in valid_targets:
        db["circle_members"].insert_one(
            {
                "circle_id": circle_id,
                "user_id": uid,
                "tags": ["member"],
                "joined_at": datetime.datetime.utcnow(),
            }
        )

    # Auto-enable channel notifications for the first channel for all members added
    first_ch = db["channels"].find_one({"circle_id": circle_id}, sort=[("position", 1)])
    if first_ch:
        alerts = [{"user_id": current_user["_id"], "channel_id": first_ch["_id"]}]
        for uid in valid_targets:
            alerts.append({"user_id": uid, "channel_id": first_ch["_id"]})
        db["channel_alerts"].insert_many(alerts)

    return jsonify(
        {
            "success": True,
            "circle": {"id": str(circle_id), "name": name, "invite_code": code},
        }
    )


@circles_bp.route("/discover", methods=["GET"])
@require_auth
def discover_circles(current_user):
    """List public circles the current user is NOT a member of."""
    memberships = list(db["circle_members"].find({"user_id": current_user["_id"]}))
    joined_circle_ids = [m["circle_id"] for m in memberships]

    discoverable = list(
        db["circles"]
        .find(
            {
                "_id": {"$nin": joined_circle_ids},
                "is_public": True,
                "circle_type": {"$ne": "feed"},
            }
        )
        .sort("created_at", -1)
        .limit(50)
    )

    result = []
    for s in discoverable:
        # Count members for the UI badge
        member_count = db["circle_members"].count_documents({"circle_id": s["_id"]})
        result.append(
            {
                "id": str(s["_id"]),
                "name": s["name"],
                "icon_url": fix_photo_path(s.get("icon_url", "")),
                "invite_code": s.get("invite_code", ""),
                "member_count": member_count,
            }
        )
    return jsonify(result)


@circles_bp.route("/<circle_id>", methods=["GET"])
@require_circle_member
def get_circle(current_user, membership, circle_id):
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if not circle:
        return jsonify({"error": "Not found"}), 404

    name = circle.get("name", "")
    icon_url = circle.get("icon_url", "")

    if not name or not icon_url:
        mem_docs = list(db["circle_members"].find({"circle_id": circle["_id"]}))
        other_ids = [
            m["user_id"] for m in mem_docs if m["user_id"] != current_user["_id"]
        ]
        others = list(db["emails"].find({"_id": {"$in": other_ids}}))

        if not name and others:
            names = [u.get("user_full_name", "Unknown") for u in others[:4]]
            name = ", ".join(names)
            if len(others) > 4:
                name += f" +{len(others)-4}"

        if not icon_url and others:
            other_ids_str = [str(u["_id"]) for u in others]
            icon_url = generate_composite_icon(
                other_ids_str, f"{circle['_id']}_{current_user['_id']}"
            )

    member_count = db["circle_members"].count_documents({"circle_id": circle["_id"]})
    return jsonify(
        {
            "id": str(circle["_id"]),
            "name": name,
            "icon_url": fix_photo_path(icon_url),
            "banner_url": (
                fix_photo_path(circle.get("banner_url", ""))
                if circle.get("banner_url")
                else ""
            ),  # <-- ADD THIS
            "invite_code": circle.get("invite_code", ""),
            "is_public": circle.get("is_public", False),
            "is_owner": circle["owner_id"] == current_user["_id"],
            "member_count": member_count,
            "my_tags": membership.get("tags", []),
        }
    )


@circles_bp.route("/<circle_id>", methods=["PATCH"])
@require_circle_member
def update_circle(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json() if request.is_json else request.form
    updates = {}
    if "name" in data:
        updates["name"] = data["name"].strip()
    if "is_public" in data:
        updates["is_public"] = bool(data["is_public"])
    if updates:
        db["circles"].update_one({"_id": ObjectId(circle_id)}, {"$set": updates})
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>", methods=["DELETE"])
@require_circle_member
def delete_circle(current_user, membership, circle_id):
    if not member_has_tag(circle_id, current_user["_id"], "owner"):
        return jsonify({"error": "Only the owner can delete a circle"}), 403
    sid = ObjectId(circle_id)
    db["messages"].delete_many(
        {
            "channel_id": {
                "$in": [c["_id"] for c in db["channels"].find({"circle_id": sid})]
            }
        }
    )
    db["channels"].delete_many({"circle_id": sid})
    db["channel_folders"].delete_many({"circle_id": sid})
    db["circle_members"].delete_many({"circle_id": sid})
    # Clean up moderation & custom emojis data
    db["bans"].delete_many({"circle_id": sid})
    db["mutes"].delete_many({"circle_id": sid})
    db["warnings"].delete_many({"circle_id": sid})
    db["mod_log"].delete_many({"circle_id": sid})
    db["custom_emojis"].delete_many({"circle_id": sid})
    db["circles"].delete_one({"_id": sid})
    return jsonify({"success": True})


@circles_bp.route("/join", methods=["POST"])
@require_auth
def join_circle(current_user):
    data = request.get_json() if request.is_json else request.form
    code = (data.get("invite_code") or "").strip()
    if not code:
        return jsonify({"success": False, "error": "Invite code is required"}), 400
    circle_id = join_circle_by_invite(code, current_user["_id"])
    if not circle_id:
        return (
            jsonify(
                {"success": False, "error": "Invalid invite code or you are banned"}
            ),
            404,
        )
    circle = db["circles"].find_one({"_id": circle_id})
    return jsonify(
        {"success": True, "circle": {"id": str(circle_id), "name": circle["name"]}}
    )


@circles_bp.route("/<circle_id>/leave", methods=["POST"])
@require_circle_member
def leave_circle(current_user, membership, circle_id):
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if circle and circle["owner_id"] == current_user["_id"]:
        return (
            jsonify(
                {
                    "error": "Owner cannot leave. Transfer ownership or delete the circle."
                }
            ),
            400,
        )
    db["circle_members"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": current_user["_id"]}
    )
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/invite/reset", methods=["POST"])
@require_circle_member
def reset_invite(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    new_code = generate_invite_code()
    db["circles"].update_one(
        {"_id": ObjectId(circle_id)}, {"$set": {"invite_code": new_code}}
    )
    return jsonify({"success": True, "invite_code": new_code})


# ====================================================================
# SERVER MEMBERS & PERMISSIONS
# ====================================================================


@circles_bp.route("/<circle_id>/members", methods=["GET"])
@require_circle_member
def list_members(current_user, membership, circle_id):
    members = list(db["circle_members"].find({"circle_id": ObjectId(circle_id)}))
    user_ids = [m["user_id"] for m in members]
    users = {u["_id"]: u for u in db["emails"].find({"_id": {"$in": user_ids}})}

    # Fetch active mutes for this circle so the client knows who is muted
    active_mutes = {}
    for mute in db["mutes"].find({"circle_id": ObjectId(circle_id)}):
        # Skip expired
        if mute.get("expires_at") and mute["expires_at"] <= datetime.datetime.utcnow():
            continue
        active_mutes[mute["user_id"]] = mute

    result = []
    for m in members:
        u = users.get(m["user_id"])
        if u:
            # ---> NEW: Hide discord.import users from the members list
            if u.get("email", "").endswith("@discord.import"):
                continue

            entry = {
                **serialize_user(u),
                "tags": m.get("tags", []),
                "joined_at": (
                    to_isoformat(m["joined_at"]) if m.get("joined_at") else None
                ),
            }
            # Attach mute info if present
            mute = active_mutes.get(m["user_id"])
            if mute:
                entry["muted"] = True
                entry["mute_remaining"] = get_mute_remaining(mute)
                entry["mute_reason"] = mute.get("reason", "")
            else:
                entry["muted"] = False
            result.append(entry)
    return jsonify(result)


@circles_bp.route("/<circle_id>/members/<user_id>/tags", methods=["PUT"])
@require_circle_member
def set_member_tags(current_user, membership, circle_id, user_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    tags = data.get("tags", [])
    target_mem = get_member(circle_id, user_id)
    if not target_mem:
        return jsonify({"error": "Member not found"}), 404
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if str(circle["owner_id"]) == user_id and "owner" not in tags:
        return jsonify({"error": "Cannot remove owner tag from circle owner"}), 400
    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)},
        {"$set": {"tags": tags}},
    )
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/members/<user_id>/kick", methods=["POST"])
@require_circle_member
def kick_member(current_user, membership, circle_id, user_id):
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403
    if str(current_user["_id"]) == user_id:
        return jsonify({"error": "Cannot kick yourself"}), 400
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if str(circle["owner_id"]) == user_id:
        return jsonify({"error": "Cannot kick the owner"}), 400
    # Prevent mods from kicking admins/owners
    target_mem = get_member(circle_id, user_id)
    if target_mem and member_has_any_tag(circle_id, user_id, ["owner", "admin"]):
        if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
            return jsonify({"error": "Cannot kick an admin"}), 403

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()

    db["circle_members"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )

    log_mod_action(circle_id, "kick", current_user["_id"], user_id, reason)
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/tags", methods=["GET"])
@require_circle_member
def list_circle_tags(current_user, membership, circle_id):
    members = db["circle_members"].find({"circle_id": ObjectId(circle_id)})
    tags = set()
    for m in members:
        tags.update(m.get("tags", []))
    return jsonify(sorted(tags))


# ====================================================================
# CUSTOM EMOJIS — CRUD
# ====================================================================


@circles_bp.route("/<circle_id>/emojis", methods=["GET"])
@require_circle_member
def list_emojis(current_user, membership, circle_id):
    """Retrieve custom emojis for the circle."""
    emojis = list(db["custom_emojis"].find({"circle_id": ObjectId(circle_id)}))
    result = []
    for e in emojis:
        result.append(
            {
                "id": str(e["_id"]),
                "name": e["name"],
                "image_url": fix_photo_path(e["image_url"]),
                "created_by": str(e.get("created_by", "")),
                "created_at": to_isoformat(e.get("created_at")),
            }
        )
    return jsonify(result)


@circles_bp.route("/<circle_id>/emojis", methods=["POST"])
@require_circle_member
def create_emoji(current_user, membership, circle_id):
    if not member_has_permission(circle_id, current_user["_id"], "manage_emojis"):
        return jsonify({"error": "Permission denied"}), 403

    name = request.form.get("name", "").strip().lower()
    name = re.sub(r"[^a-z0-9_]", "", name)
    if not name:
        return jsonify({"error": "Invalid emoji name"}), 400

    if (
        db["custom_emojis"].count_documents(
            {"circle_id": ObjectId(circle_id), "name": name}
        )
        > 0
    ):
        return jsonify({"error": f"Emoji :{name}: already exists"}), 400

    info = process_upload(
        request.files.get("file"),
        "emoji",
        circle_id,
        processing=PROCESSING_THUMBNAIL,
        max_thumbnail=128,
        allowed_exts=IMAGE_EXTENSIONS,
    )
    if not info:
        return jsonify({"error": "Invalid image file"}), 400

    doc = {
        "circle_id": ObjectId(circle_id),
        "name": name,
        "image_url": info["stored_filename"],
        "created_by": current_user["_id"],
        "created_at": datetime.datetime.utcnow(),
    }
    res = db["custom_emojis"].insert_one(doc)

    return jsonify(
        {
            "success": True,
            "emoji": {
                "id": str(res.inserted_id),
                "name": name,
                "image_url": fix_photo_path(info["stored_filename"]),
            },
        }
    )


@circles_bp.route("/<circle_id>/emojis/<emoji_id>", methods=["PATCH"])
@require_circle_member
def update_emoji(current_user, membership, circle_id, emoji_id):
    """Edit a custom emoji's name."""
    if not member_has_permission(circle_id, current_user["_id"], "manage_emojis"):
        return jsonify({"error": "Permission denied"}), 403

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip().lower()
    name = re.sub(r"[^a-z0-9_]", "", name)
    if not name:
        return jsonify({"error": "Invalid emoji name"}), 400

    if (
        db["custom_emojis"].count_documents(
            {"circle_id": ObjectId(circle_id), "name": name}
        )
        > 0
    ):
        return jsonify({"error": f"Emoji :{name}: already exists"}), 400

    db["custom_emojis"].update_one(
        {"_id": ObjectId(emoji_id), "circle_id": ObjectId(circle_id)},
        {"$set": {"name": name}},
    )
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/emojis/<emoji_id>", methods=["DELETE"])
@require_circle_member
def delete_emoji(current_user, membership, circle_id, emoji_id):
    """Delete a custom emoji."""
    if not member_has_permission(circle_id, current_user["_id"], "manage_emojis"):
        return jsonify({"error": "Permission denied"}), 403

    # Optionally delete the file here to save space
    emoji = db["custom_emojis"].find_one(
        {"_id": ObjectId(emoji_id), "circle_id": ObjectId(circle_id)}
    )
    if emoji and emoji.get("image_url"):
        delete_upload(emoji["image_url"])

    db["custom_emojis"].delete_one(
        {"_id": ObjectId(emoji_id), "circle_id": ObjectId(circle_id)}
    )
    return jsonify({"success": True})


# ====================================================================
# MODERATION — BAN / UNBAN
# ====================================================================


@circles_bp.route("/<circle_id>/members/<user_id>/ban", methods=["POST"])
@require_circle_member
def ban_member(current_user, membership, circle_id, user_id):
    """Ban a user from the circle (kicks + prevents rejoin)."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403
    if str(current_user["_id"]) == user_id:
        return jsonify({"error": "Cannot ban yourself"}), 400

    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if str(circle["owner_id"]) == user_id:
        return jsonify({"error": "Cannot ban the circle owner"}), 400

    # Prevent mods from banning admins
    if member_has_any_tag(circle_id, user_id, ["owner", "admin"]):
        if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
            return jsonify({"error": "Cannot ban an admin"}), 403

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    delete_messages = data.get("delete_messages", False)

    # Insert ban record (upsert in case they were already banned)
    db["bans"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)},
        {
            "$set": {
                "banned_by": current_user["_id"],
                "reason": reason,
                "created_at": datetime.datetime.utcnow(),
            }
        },
        upsert=True,
    )

    # Remove from members
    db["circle_members"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )

    # Remove active mute if any
    db["mutes"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )

    # Optionally purge their messages
    deleted_count = 0
    if delete_messages:
        channel_ids = [
            c["_id"] for c in db["channels"].find({"circle_id": ObjectId(circle_id)})
        ]
        result = db["messages"].delete_many(
            {"channel_id": {"$in": channel_ids}, "author_id": ObjectId(user_id)}
        )
        deleted_count = result.deleted_count

    log_mod_action(
        circle_id,
        "ban",
        current_user["_id"],
        user_id,
        reason,
        {"delete_messages": delete_messages, "messages_deleted": deleted_count},
    )

    return jsonify({"success": True, "messages_deleted": deleted_count})


@circles_bp.route("/<circle_id>/members/<user_id>/unban", methods=["POST"])
@require_circle_member
def unban_member(current_user, membership, circle_id, user_id):
    """Remove a ban, allowing the user to rejoin via invite."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    result = db["bans"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )
    if result.deleted_count == 0:
        return jsonify({"error": "User is not banned"}), 404

    log_mod_action(circle_id, "unban", current_user["_id"], user_id)
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/bans", methods=["GET"])
@require_circle_member
def list_bans(current_user, membership, circle_id):
    """List all active bans for this circle."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    bans = list(
        db["bans"].find({"circle_id": ObjectId(circle_id)}).sort("created_at", -1)
    )

    result = []
    for b in bans:
        user = get_user_details(b["user_id"])
        moderator = get_user_details(b["banned_by"])
        result.append(
            {
                "id": str(b["_id"]),
                "user": user,
                "banned_by": moderator,
                "reason": b.get("reason", ""),
                "created_at": to_isoformat(b["created_at"]),
            }
        )
    return jsonify(result)


# ====================================================================
# MODERATION — MUTE / TIMEOUT
# ====================================================================


@circles_bp.route("/<circle_id>/members/<user_id>/mute", methods=["POST"])
@require_circle_member
def mute_member(current_user, membership, circle_id, user_id):
    """
    Mute (timeout) a user. They can still read, but cannot send messages.
    Body: { "reason": "...", "duration_minutes": 60 }
    duration_minutes = 0 or null → indefinite mute
    """
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403
    if str(current_user["_id"]) == user_id:
        return jsonify({"error": "Cannot mute yourself"}), 400

    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if str(circle["owner_id"]) == user_id:
        return jsonify({"error": "Cannot mute the circle owner"}), 400

    if member_has_any_tag(circle_id, user_id, ["owner", "admin"]):
        if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
            return jsonify({"error": "Cannot mute an admin"}), 403

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    duration_minutes = data.get("duration_minutes", 0)

    expires_at = None
    if duration_minutes and int(duration_minutes) > 0:
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(
            minutes=int(duration_minutes)
        )

    db["mutes"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)},
        {
            "$set": {
                "muted_by": current_user["_id"],
                "reason": reason,
                "expires_at": expires_at,
                "created_at": datetime.datetime.utcnow(),
            }
        },
        upsert=True,
    )

    log_mod_action(
        circle_id,
        "mute",
        current_user["_id"],
        user_id,
        reason,
        {"duration_minutes": duration_minutes or "indefinite"},
    )

    # Notify the muted user
    duration_text = (
        f" for {duration_minutes} minutes" if duration_minutes else " indefinitely"
    )
    create_notification(
        user_id=user_id,
        type="moderation",
        title=f"You have been muted{duration_text}",
        body=reason or "No reason provided",
        source_id=current_user["_id"],
        context_id=None,
    )

    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/members/<user_id>/unmute", methods=["POST"])
@require_circle_member
def unmute_member(current_user, membership, circle_id, user_id):
    """Remove a mute from a user."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    result = db["mutes"].delete_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )
    if result.deleted_count == 0:
        return jsonify({"error": "User is not muted"}), 404

    log_mod_action(circle_id, "unmute", current_user["_id"], user_id)
    return jsonify({"success": True})


# ====================================================================
# MODERATION — WARNINGS
# ====================================================================


@circles_bp.route("/<circle_id>/members/<user_id>/warn", methods=["POST"])
@require_circle_member
def warn_member(current_user, membership, circle_id, user_id):
    """Issue a formal warning to a user."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403
    if str(current_user["_id"]) == user_id:
        return jsonify({"error": "Cannot warn yourself"}), 400

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "A reason is required for warnings"}), 400

    warning_doc = {
        "circle_id": ObjectId(circle_id),
        "user_id": ObjectId(user_id),
        "warned_by": current_user["_id"],
        "reason": reason,
        "created_at": datetime.datetime.utcnow(),
    }
    result = db["warnings"].insert_one(warning_doc)

    log_mod_action(circle_id, "warn", current_user["_id"], user_id, reason)

    # Notify the warned user
    create_notification(
        user_id=user_id,
        type="moderation",
        title="You received a warning",
        body=reason,
        source_id=current_user["_id"],
        context_id=None,
    )

    # Return warning count
    count = db["warnings"].count_documents(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)}
    )

    return jsonify(
        {
            "success": True,
            "warning_id": str(result.inserted_id),
            "total_warnings": count,
        }
    )


@circles_bp.route("/<circle_id>/members/<user_id>/warnings", methods=["GET"])
@require_circle_member
def get_member_warnings(current_user, membership, circle_id, user_id):
    """Get all warnings for a specific user in this circle."""
    if not _is_moderator(circle_id, current_user["_id"]):
        # Users can view their own warnings
        if str(current_user["_id"]) != user_id:
            return jsonify({"error": "Permission denied"}), 403

    warnings = list(
        db["warnings"]
        .find({"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)})
        .sort("created_at", -1)
    )

    result = []
    for w in warnings:
        moderator = get_user_details(w["warned_by"])
        result.append(
            {
                "id": str(w["_id"]),
                "reason": w["reason"],
                "warned_by": moderator,
                "created_at": to_isoformat(w["created_at"]),
            }
        )
    return jsonify(result)


@circles_bp.route("/<circle_id>/warnings/<warning_id>", methods=["DELETE"])
@require_circle_member
def delete_warning(current_user, membership, circle_id, warning_id):
    """Delete a specific warning."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    warning = db["warnings"].find_one({"_id": ObjectId(warning_id)})
    if not warning or str(warning["circle_id"]) != circle_id:
        return jsonify({"error": "Warning not found"}), 404

    db["warnings"].delete_one({"_id": ObjectId(warning_id)})

    log_mod_action(
        circle_id,
        "warn_delete",
        current_user["_id"],
        str(warning["user_id"]),
        f"Deleted warning: {warning.get('reason', '')}",
    )
    return jsonify({"success": True})


# ====================================================================
# MODERATION — AUDIT / MOD LOG
# ====================================================================


@circles_bp.route("/<circle_id>/mod-log", methods=["GET"])
@require_circle_member
def get_mod_log(current_user, membership, circle_id):
    """Retrieve the moderation audit log for a circle."""
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    limit = min(int(request.args.get("limit", 50)), 200)
    skip = int(request.args.get("skip", 0))
    action_filter = request.args.get("action")  # optional filter by action type

    query = {"circle_id": ObjectId(circle_id)}
    if action_filter:
        query["action"] = action_filter

    entries = list(
        db["mod_log"].find(query).sort("created_at", -1).skip(skip).limit(limit)
    )

    result = []
    for e in entries:
        moderator = get_user_details(e["moderator_id"])
        target = (
            get_user_details(e["target_user_id"]) if e.get("target_user_id") else None
        )
        result.append(
            {
                "id": str(e["_id"]),
                "action": e["action"],
                "moderator": moderator,
                "target": target,
                "reason": e.get("reason", ""),
                "details": e.get("details", {}),
                "created_at": to_isoformat(e["created_at"]),
            }
        )
    return jsonify(result)


# ====================================================================
# MODERATION — PURGE MESSAGES
# ====================================================================


@circles_bp.route("/<circle_id>/channels/<channel_id>/purge", methods=["POST"])
@require_circle_member
def purge_messages(current_user, membership, circle_id, channel_id):
    """
    Bulk-delete messages from a channel.
    Body: { "count": 50, "user_id": "optional_filter" }
    """
    if not _is_moderator(circle_id, current_user["_id"]):
        return jsonify({"error": "Permission denied"}), 403

    channel = db["channels"].find_one({"_id": ObjectId(channel_id)})
    if not channel or str(channel["circle_id"]) != circle_id:
        return jsonify({"error": "Channel not found"}), 404

    data = request.get_json(silent=True) or {}
    count = min(int(data.get("count", 50)), 500)
    filter_user_id = data.get("user_id")

    query = {"channel_id": ObjectId(channel_id)}
    if filter_user_id:
        query["author_id"] = ObjectId(filter_user_id)

    # Find the messages to delete (newest first)
    messages = list(db["messages"].find(query).sort("created_at", -1).limit(count))
    msg_ids = [m["_id"] for m in messages]

    if msg_ids:
        # Clean up any attached images
        for m in messages:
            delete_upload(m.get("image_url"))
            delete_upload(m.get("file_url"))

        db["messages"].delete_many({"_id": {"$in": msg_ids}})

    log_mod_action(
        circle_id,
        "purge",
        current_user["_id"],
        filter_user_id,
        f"Purged {len(msg_ids)} messages from #{channel['name']}",
        {
            "channel_id": channel_id,
            "channel_name": channel["name"],
            "count": len(msg_ids),
        },
    )

    return jsonify({"success": True, "deleted": len(msg_ids)})


# ====================================================================
# CHANNEL FOLDERS
# ====================================================================


@circles_bp.route("/<circle_id>/folders", methods=["GET"])
@require_circle_member
def list_folders(current_user, membership, circle_id):
    folders = list(
        db["channel_folders"]
        .find({"circle_id": ObjectId(circle_id)})
        .sort("position", 1)
    )
    return jsonify(
        [
            {"id": str(f["_id"]), "name": f["name"], "position": f.get("position", 0)}
            for f in folders
        ]
    )


@circles_bp.route("/<circle_id>/folders", methods=["POST"])
@require_circle_member
def create_folder(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Folder name required"}), 400
    max_pos = db["channel_folders"].find_one(
        {"circle_id": ObjectId(circle_id)}, sort=[("position", -1)]
    )
    pos = (max_pos["position"] + 1) if max_pos else 0
    result = db["channel_folders"].insert_one(
        {"circle_id": ObjectId(circle_id), "name": name, "position": pos}
    )
    return jsonify(
        {
            "success": True,
            "folder": {"id": str(result.inserted_id), "name": name, "position": pos},
        }
    )


@circles_bp.route("/<circle_id>/folders/<folder_id>", methods=["PATCH"])
@require_circle_member
def update_folder(current_user, membership, circle_id, folder_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    updates = {}
    if "name" in data:
        updates["name"] = data["name"].strip()
    if "position" in data:
        updates["position"] = int(data["position"])
    if updates:
        db["channel_folders"].update_one(
            {"_id": ObjectId(folder_id)}, {"$set": updates}
        )
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/folders/<folder_id>", methods=["DELETE"])
@require_circle_member
def delete_folder(current_user, membership, circle_id, folder_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    db["channels"].update_many(
        {"circle_id": ObjectId(circle_id), "folder_id": ObjectId(folder_id)},
        {"$set": {"folder_id": None}},
    )
    db["channel_folders"].delete_one({"_id": ObjectId(folder_id)})
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/folders/reorder", methods=["POST"])
@require_circle_member
def reorder_folders(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    order = data.get("order", [])
    for i, fid in enumerate(order):
        db["channel_folders"].update_one(
            {"_id": ObjectId(fid)}, {"$set": {"position": i}}
        )
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/channels", methods=["POST"])
@require_circle_member
def create_channel(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    name = (data.get("name") or "").strip().lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9\-]", "", name)
    if not name:
        return jsonify({"success": False, "error": "Channel name required"}), 400

    channel_type = data.get("channel_type", "chat")
    if channel_type not in ("chat", "feed"):
        channel_type = "chat"

    folder_id = data.get("folder_id")
    permission_tags = data.get("permission_tags", [])
    slowmode_seconds = int(data.get("slowmode_seconds", 0))
    max_pos = db["channels"].find_one(
        {"circle_id": ObjectId(circle_id)}, sort=[("position", -1)]
    )
    pos = (max_pos["position"] + 1) if max_pos else 0
    result = db["channels"].insert_one(
        {
            "circle_id": ObjectId(circle_id),
            "name": name,
            "description": data.get("description", ""),
            "channel_type": channel_type,
            "folder_id": ObjectId(folder_id) if folder_id else None,
            "position": pos,
            "permission_tags": permission_tags,
            "slowmode_seconds": slowmode_seconds,
            "created_at": datetime.datetime.utcnow(),
        }
    )
    return jsonify(
        {
            "success": True,
            "channel": {
                "id": str(result.inserted_id),
                "name": name,
                "position": pos,
                "folder_id": folder_id,
                "permission_tags": permission_tags,
                "slowmode_seconds": slowmode_seconds,
            },
        }
    )


@circles_bp.route("/<circle_id>/channels/<channel_id>", methods=["PATCH"])
@require_circle_member
def update_channel(current_user, membership, circle_id, channel_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    updates = {}
    if "name" in data:
        updates["name"] = data["name"].strip().lower().replace(" ", "-")
    if "description" in data:
        updates["description"] = data["description"]
    if "folder_id" in data:
        updates["folder_id"] = (
            ObjectId(data["folder_id"]) if data["folder_id"] else None
        )
    if "position" in data:
        updates["position"] = int(data["position"])
    if "permission_tags" in data:
        updates["permission_tags"] = data["permission_tags"]
    if "channel_type" in data:
        ctype = data["channel_type"]
        if ctype in ("chat", "feed"):
            updates["channel_type"] = ctype
    if "slowmode_seconds" in data:
        new_slowmode = max(0, min(int(data["slowmode_seconds"]), 21600))  # max 6 hours
        updates["slowmode_seconds"] = new_slowmode
        # Log slowmode changes
        channel = db["channels"].find_one({"_id": ObjectId(channel_id)})
        if channel:
            log_mod_action(
                circle_id,
                "slowmode",
                current_user["_id"],
                reason=f"Set slowmode to {new_slowmode}s on #{channel['name']}",
                details={"channel_id": channel_id, "seconds": new_slowmode},
            )
    if updates:
        db["channels"].update_one({"_id": ObjectId(channel_id)}, {"$set": updates})
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/channels/<channel_id>", methods=["DELETE"])
@require_circle_member
def delete_channel(current_user, membership, circle_id, channel_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    db["messages"].delete_many({"channel_id": ObjectId(channel_id)})
    db["channels"].delete_one({"_id": ObjectId(channel_id)})
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/channels/reorder", methods=["POST"])
@require_circle_member
def reorder_channels(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json()
    items = data.get("channels", [])
    for item in items:
        updates = {"position": int(item["position"])}
        if "folder_id" in item:
            updates["folder_id"] = (
                ObjectId(item["folder_id"]) if item["folder_id"] else None
            )
        db["channels"].update_one({"_id": ObjectId(item["id"])}, {"$set": updates})
    return jsonify({"success": True})


# ====================================================================
# ROLES — CRUD
# ====================================================================


@circles_bp.route("/<circle_id>/roles", methods=["GET"])
@require_circle_member
def list_roles(current_user, membership, circle_id):
    """Return all roles for this circle, creating defaults if needed."""
    roles = ensure_circle_roles(circle_id)
    result = []
    for r in roles:
        result.append(
            {
                "id": str(r["_id"]),
                "name": r["name"],
                "color": r.get("color", "#9e9e9e"),
                "position": r.get("position", 0),
                "is_default": r.get("is_default", False),
                "permissions": r.get("permissions", {}),
                "created_at": to_isoformat(r.get("created_at")),
            }
        )
    # Sort by position descending (highest = most powerful)
    result.sort(key=lambda x: x["position"], reverse=True)
    return jsonify(result)


@circles_bp.route("/<circle_id>/roles", methods=["POST"])
@require_circle_member
def create_role(current_user, membership, circle_id):
    """Create a new custom role."""
    # Check permission: owner, admin, or has manage_roles
    if not member_has_permission(circle_id, current_user["_id"], "manage_roles"):
        return jsonify({"error": "Permission denied"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Role name is required"}), 400
    if len(name) > 32:
        return jsonify({"error": "Role name too long (max 32 chars)"}), 400

    color = (data.get("color") or "#9e9e9e").strip()
    permissions = data.get("permissions", {})

    # Validate permission keys
    clean_perms = {}
    for key in ROLE_PERMISSIONS:
        clean_perms[key] = bool(permissions.get(key, False))

    # Position: just above the default role
    default_role = db["circle_roles"].find_one(
        {"circle_id": ObjectId(circle_id), "is_default": True}
    )
    position = (default_role.get("position", 0) + 1) if default_role else 1

    now = datetime.datetime.utcnow()
    doc = {
        "circle_id": ObjectId(circle_id),
        "name": name,
        "color": color,
        "position": position,
        "is_default": False,
        "permissions": clean_perms,
        "created_at": now,
    }
    result = db["circle_roles"].insert_one(doc)

    return jsonify(
        {
            "success": True,
            "role": {
                "id": str(result.inserted_id),
                "name": name,
                "color": color,
                "position": position,
                "is_default": False,
                "permissions": clean_perms,
            },
        }
    )


@circles_bp.route("/<circle_id>/roles/<role_id>", methods=["PATCH"])
@require_circle_member
def update_role(current_user, membership, circle_id, role_id):
    """Update a role's name, color, position, or permissions."""
    if not member_has_permission(circle_id, current_user["_id"], "manage_roles"):
        return jsonify({"error": "Permission denied"}), 403

    role = db["circle_roles"].find_one({"_id": ObjectId(role_id)})
    if not role or str(role["circle_id"]) != circle_id:
        return jsonify({"error": "Role not found"}), 404

    data = request.get_json(silent=True) or {}
    updates = {}

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "Role name is required"}), 400
        if len(name) > 32:
            return jsonify({"error": "Role name too long"}), 400
        updates["name"] = name

    if "color" in data:
        updates["color"] = (data["color"] or "#9e9e9e").strip()

    if "position" in data:
        updates["position"] = int(data["position"])

    if "permissions" in data:
        perms = data["permissions"]
        clean_perms = {}
        for key in ROLE_PERMISSIONS:
            clean_perms[key] = bool(perms.get(key, False))
        updates["permissions"] = clean_perms

    if updates:
        db["circle_roles"].update_one({"_id": ObjectId(role_id)}, {"$set": updates})

    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/roles/<role_id>", methods=["DELETE"])
@require_circle_member
def delete_role(current_user, membership, circle_id, role_id):
    """Delete a custom role. Cannot delete the default role."""
    if not member_has_permission(circle_id, current_user["_id"], "manage_roles"):
        return jsonify({"error": "Permission denied"}), 403

    role = db["circle_roles"].find_one({"_id": ObjectId(role_id)})
    if not role or str(role["circle_id"]) != circle_id:
        return jsonify({"error": "Role not found"}), 404

    if role.get("is_default"):
        return jsonify({"error": "Cannot delete the default role"}), 400

    rid = ObjectId(role_id)

    # Remove this role from all members who have it
    db["circle_members"].update_many(
        {"circle_id": ObjectId(circle_id), "role_ids": rid},
        {"$pull": {"role_ids": rid}},
    )

    db["circle_roles"].delete_one({"_id": rid})
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/roles/reorder", methods=["POST"])
@require_circle_member
def reorder_roles(current_user, membership, circle_id):
    """Reorder roles by setting their positions."""
    if not member_has_permission(circle_id, current_user["_id"], "manage_roles"):
        return jsonify({"error": "Permission denied"}), 403

    data = request.get_json(silent=True) or {}
    order = data.get("order", [])  # list of { id, position }

    for item in order:
        db["circle_roles"].update_one(
            {"_id": ObjectId(item["id"]), "circle_id": ObjectId(circle_id)},
            {"$set": {"position": int(item["position"])}},
        )

    return jsonify({"success": True})


# ====================================================================
# MEMBER ROLE ASSIGNMENT
# ====================================================================


@circles_bp.route("/<circle_id>/members/<user_id>/roles", methods=["PUT"])
@require_circle_member
def set_member_roles(current_user, membership, circle_id, user_id):
    """
    Set the roles for a member.
    Body: { "role_ids": ["abc", "def"] }
    """
    if not member_has_permission(circle_id, current_user["_id"], "manage_roles"):
        return jsonify({"error": "Permission denied"}), 403

    target_mem = get_member(circle_id, user_id)
    if not target_mem:
        return jsonify({"error": "Member not found"}), 404

    data = request.get_json(silent=True) or {}
    role_ids_raw = data.get("role_ids", [])

    # Validate all role IDs belong to this circle
    valid_role_ids = []
    new_tags = []
    for rid in role_ids_raw:
        role = db["circle_roles"].find_one(
            {
                "_id": ObjectId(rid),
                "circle_id": ObjectId(circle_id),
            }
        )
        if role:
            valid_role_ids.append(ObjectId(rid))
            new_tags.append(role["name"].lower())

    # Preserve the "owner" tag if the member is the circle owner
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if circle and str(circle["owner_id"]) == user_id:
        if "owner" not in new_tags:
            new_tags.insert(0, "owner")

    # Derive tags from role names for backward compatibility
    # Map well-known role names → legacy tags
    tag_map = {"admin": "admin", "moderator": "moderator", "member": "member"}
    legacy_tags = []
    for t in new_tags:
        legacy_tags.append(tag_map.get(t, t))

    if not legacy_tags:
        legacy_tags = ["member"]

    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(user_id)},
        {"$set": {"role_ids": valid_role_ids, "tags": legacy_tags}},
    )

    return jsonify({"success": True, "role_ids": [str(r) for r in valid_role_ids]})


# ====================================================================
# OWNERSHIP TRANSFER
# ====================================================================


@circles_bp.route("/<circle_id>/transfer-ownership", methods=["POST"])
@require_circle_member
def transfer_ownership(current_user, membership, circle_id):
    """
    Transfer circle ownership to another member.
    Only the current owner can do this.
    Body: { "new_owner_id": "user_id_string" }
    """
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})
    if not circle:
        return jsonify({"error": "Circle not found"}), 404

    if circle["owner_id"] != current_user["_id"]:
        return jsonify({"error": "Only the circle owner can transfer ownership"}), 403

    data = request.get_json(silent=True) or {}
    new_owner_id = data.get("new_owner_id")
    if not new_owner_id:
        return jsonify({"error": "new_owner_id is required"}), 400

    if new_owner_id == str(current_user["_id"]):
        return jsonify({"error": "You are already the owner"}), 400

    # Verify the target is a member
    target_mem = get_member(circle_id, new_owner_id)
    if not target_mem:
        return jsonify({"error": "That user is not a member of this circle"}), 404

    # Transfer ownership on the circle document
    db["circles"].update_one(
        {"_id": ObjectId(circle_id)},
        {"$set": {"owner_id": ObjectId(new_owner_id)}},
    )

    # Update tags: give new owner the "owner" tag, remove from old
    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": ObjectId(new_owner_id)},
        {"$addToSet": {"tags": "owner"}},
    )
    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": current_user["_id"]},
        {"$pull": {"tags": "owner"}},
    )
    # Ensure old owner keeps admin at minimum
    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": current_user["_id"]},
        {"$addToSet": {"tags": "admin"}},
    )

    # Mod log
    log_mod_action(
        circle_id,
        "transfer_ownership",
        current_user["_id"],
        new_owner_id,
        reason="Ownership transferred",
    )

    return jsonify({"success": True})


# ====================================================================
# COMBINED CHANNELS + FOLDERS (for settings panel)
# ====================================================================


@circles_bp.route("/<circle_id>/read-all", methods=["POST"])
@require_circle_member
def mark_circle_read(current_user, membership, circle_id):
    """Mark every channel in this circle as read for the current user."""
    channels = db["channels"].find({"circle_id": ObjectId(circle_id)}, {"_id": 1})
    now = datetime.datetime.utcnow()
    ops = []
    for ch in channels:
        ops.append(
            pymongo.UpdateOne(
                {"user_id": current_user["_id"], "context_id": ch["_id"]},
                {"$set": {"last_read_at": now}},
                upsert=True,
            )
        )
    if ops:
        db["read_states"].bulk_write(ops, ordered=False)
    return jsonify({"success": True})


@circles_bp.route("/<circle_id>/mute", methods=["POST"])
@require_circle_member
def toggle_circle_mute(current_user, membership, circle_id):
    """Toggle mute on/off for the current user in this circle."""
    currently_muted = membership.get("muted", False)
    new_value = not currently_muted

    db["circle_members"].update_one(
        {"circle_id": ObjectId(circle_id), "user_id": current_user["_id"]},
        {"$set": {"muted": new_value}},
    )

    return jsonify({"success": True, "muted": new_value})


@circles_bp.route("/<circle_id>/layout", methods=["GET"])
@require_circle_member
def get_circle_layout(current_user, membership, circle_id):
    """
    Returns channels and folders in one call for the settings drag-and-drop UI.
    """
    channels = list(
        db["channels"].find({"circle_id": ObjectId(circle_id)}).sort("position", 1)
    )
    folders = list(
        db["channel_folders"]
        .find({"circle_id": ObjectId(circle_id)})
        .sort("position", 1)
    )

    return jsonify(
        {
            "channels": [
                {
                    "id": str(c["_id"]),
                    "name": c["name"],
                    "description": c.get("description", ""),
                    "channel_type": c.get("channel_type", "chat"),
                    "folder_id": str(c["folder_id"]) if c.get("folder_id") else None,
                    "position": c.get("position", 0),
                    "permission_tags": c.get("permission_tags", []),
                    "slowmode_seconds": c.get("slowmode_seconds", 0),
                }
                for c in channels
            ],
            "folders": [
                {
                    "id": str(f["_id"]),
                    "name": f["name"],
                    "position": f.get("position", 0),
                }
                for f in folders
            ],
        }
    )


@circles_bp.route("/<circle_id>/unread", methods=["GET"])
@login_required
def get_unread_channels(current_user, circle_id):
    """
    Returns a list of channel IDs that have unread messages for the
    current user, plus optional counts.

    Response:
        {
            "unread": {
                "<channel_id>": { "has_unread": true, "last_message_at": "..." },
                ...
            }
        }
    """
    from bson.objectid import ObjectId
    from app import db, get_member, can_access_channel, to_isoformat

    mem = get_member(circle_id, current_user["_id"])
    if not mem:
        return jsonify({"error": "Not a member"}), 403

    # Get all channels in this circle
    channels = list(db["channels"].find({"circle_id": ObjectId(circle_id)}))

    # Get all read states for this user in one query
    channel_ids = [ch["_id"] for ch in channels]
    read_states = {}
    for rs in db["read_states"].find(
        {
            "user_id": current_user["_id"],
            "context_id": {"$in": channel_ids},
        }
    ):
        read_states[rs["context_id"]] = rs.get("last_read_at")

    unread = {}
    for ch in channels:
        # Skip channels the user can't access
        if not can_access_channel(ch, current_user["_id"], ObjectId(circle_id)):
            continue

        last_msg_at = ch.get("last_message_at")
        if not last_msg_at:
            continue  # No messages ever sent — not unread

        last_read = read_states.get(ch["_id"])

        if last_read is None or last_msg_at > last_read:
            unread[str(ch["_id"])] = {
                "has_unread": True,
                "last_message_at": to_isoformat(last_msg_at),
            }

    return jsonify({"unread": unread})


@circles_bp.route("/<circle_id>/channels", methods=["GET"])
@require_circle_member
def list_channels(current_user, membership, circle_id):
    circle = db["circles"].find_one({"_id": ObjectId(circle_id)})

    # Intelligently filter feed channels if we are looking at the global feed
    if circle and circle.get("circle_type") == "feed":
        channels = list(
            db["channels"]
            .find(
                {
                    "circle_id": ObjectId(circle_id),
                    "$or": [
                        {"channel_type": "feed"},
                        {"owner_id": current_user["_id"]},
                        {"followers": current_user["_id"]},
                    ],
                }
            )
            .sort("position", 1)
        )
    else:
        # Standard circle behavior
        channels = list(
            db["channels"].find({"circle_id": ObjectId(circle_id)}).sort("position", 1)
        )

    result = []
    for c in channels:
        if can_access_channel(c, current_user["_id"], circle_id):
            result.append(
                {
                    "id": str(c["_id"]),
                    "name": c["name"],
                    "description": c.get("description", ""),
                    "channel_type": c.get("channel_type", "chat"),
                    "folder_id": str(c["folder_id"]) if c.get("folder_id") else None,
                    "position": c.get("position", 0),
                    "permission_tags": c.get("permission_tags", []),
                    "slowmode_seconds": c.get("slowmode_seconds", 0),
                    "color": c.get("color", "#4285f4"),
                }
            )
    return jsonify(result)


@circles_bp.route("/my-feed-channels", methods=["GET"])
@require_auth
def get_my_feed_channels(current_user):
    from app import get_feed_general_channel

    mems = list(db["circle_members"].find({"user_id": current_user["_id"]}))
    sids = [m["circle_id"] for m in mems]

    feed_channels = list(
        db["channels"].find({"circle_id": {"$in": sids}, "channel_type": "feed"})
    )

    global_feed = get_feed_general_channel()
    if not any(c["_id"] == global_feed["_id"] for c in feed_channels):
        feed_channels.append(global_feed)

    circles_map = {s["_id"]: s for s in db["circles"].find({"_id": {"$in": sids}})}

    result = []
    for c in feed_channels:
        if can_access_channel(c, current_user["_id"], c["circle_id"]):
            if c["_id"] == global_feed["_id"]:
                srv_name = "Global Feed"
                ch_name = "general"
            else:
                srv = circles_map.get(c["circle_id"])
                srv_name = srv["name"] if srv else "Unknown Circle"
                ch_name = c["name"]

            result.append(
                {"id": str(c["_id"]), "name": ch_name, "circle_name": srv_name}
            )

    # Sort alphabetically by circle name
    result.sort(key=lambda x: x["circle_name"])
    return jsonify(result)


@circles_bp.route("/invite/<invite_code>/preview", methods=["GET"])
def preview_invite(invite_code):
    circle = db["circles"].find_one({"invite_code": invite_code})
    if not circle:
        return jsonify({"success": False, "error": "Invalid invite code"}), 404

    name = circle.get("name", "")
    icon_url = circle.get("icon_url", "")

    # Fallback to group icon generation for nameless DMs (though they rarely share codes)
    if not name or not icon_url:
        mem_docs = list(db["circle_members"].find({"circle_id": circle["_id"]}))
        other_ids = [m["user_id"] for m in mem_docs]
        others = list(db["emails"].find({"_id": {"$in": other_ids}}))

        if not name and others:
            names = [u.get("user_full_name", "Unknown") for u in others[:4]]
            name = ", ".join(names)
            if len(others) > 4:
                name += f" +{len(others)-4}"

        if not icon_url and others:
            other_ids_str = [str(u["_id"]) for u in others]
            icon_url = generate_composite_icon(
                other_ids_str, f"{circle['_id']}_preview"
            )

    member_count = db["circle_members"].count_documents({"circle_id": circle["_id"]})

    return jsonify(
        {
            "success": True,
            "circle": {
                "id": str(circle["_id"]),
                "name": name,
                "icon_url": (
                    fix_photo_path(icon_url)
                    if icon_url
                    else fix_photo_path("no-icon.jpg")
                ),
                "banner_url": (
                    fix_photo_path(circle.get("banner_url", ""))
                    if circle.get("banner_url")
                    else ""
                ),  # <-- ADD THIS
                "member_count": member_count,
                "invite_code": invite_code,
            },
        }
    )


@circles_bp.route("/<circle_id>/banner", methods=["POST"])
@require_circle_member
def upload_circle_banner(current_user, membership, circle_id):
    if not member_has_any_tag(circle_id, current_user["_id"], ["owner", "admin"]):
        return jsonify({"error": "Permission denied"}), 403

    info = process_upload(
        request.files.get("file"),
        "circle_banner",
        circle_id,
        processing=PROCESSING_WIDTH_LIMIT,
        max_width=1920,
    )
    if not info:
        return jsonify({"error": "Invalid file"}), 400

    db["circles"].update_one(
        {"_id": ObjectId(circle_id)},
        {"$set": {"banner_url": info["stored_filename"]}},
    )
    return jsonify({"success": True, "url": info["url"]})
