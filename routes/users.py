# routes/users.py

from flask import Blueprint, request, session, jsonify
import os
import re
import uuid

from utils.shared_api import (
    db,
    UPLOAD_FOLDER,
    get_user_by_email,
    get_user_by_handle,
    serialize_user,
    block_user_dm,
    to_isoformat,
    unblock_user_dm,
    mute_user_dm,
    unmute_user_dm,
    is_blocked,
    is_muted,
    process_upload,
    delete_upload,
    PROCESSING_SQUARE,
    PROCESSING_WIDTH_LIMIT,
    IMAGE_EXTENSIONS,
)

users_bp = Blueprint("users", __name__)


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


# ====================================================================
# USER PROFILE
# ====================================================================


@users_bp.route("/me/avatar", methods=["POST"])
@require_auth
def update_avatar(current_user):
    info = process_upload(
        request.files.get("file"),
        "avatar",
        current_user["_id"],
        processing=PROCESSING_SQUARE,
        target_size=512,
    )
    if not info:
        return jsonify({"success": False, "error": "Invalid file"}), 400

    delete_upload(current_user.get("photo_url"))
    db["emails"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"photo_url": info["stored_filename"]}},
    )
    return jsonify({"success": True, "url": info["url"]})


@users_bp.route("/me/avatar/remove", methods=["POST"])
@require_auth
def remove_avatar(current_user):
    old = current_user.get("photo_url", "")
    if old and "no-icon" not in old:
        try:
            old_fname = os.path.basename(old)
            os.remove(os.path.join(UPLOAD_FOLDER, old_fname))
        except:
            pass
    db["emails"].update_one(
        {"_id": current_user["_id"]}, {"$set": {"photo_url": "no-icon.jpg"}}
    )
    return jsonify({"success": True})


@users_bp.route("/me/about", methods=["POST"])
@require_auth
def update_about(current_user):
    data = request.get_json() if request.is_json else request.form
    db["emails"].update_one(
        {"_id": current_user["_id"]}, {"$set": {"about_me": data.get("about_text", "")}}
    )
    return jsonify({"success": True})


@users_bp.route("/me/name", methods=["POST"])
@require_auth
def update_name(current_user):
    data = request.get_json() if request.is_json else request.form
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"success": False, "error": "Name is required"}), 400
    db["emails"].update_one(
        {"_id": current_user["_id"]}, {"$set": {"user_full_name": new_name}}
    )
    return jsonify({"success": True})


@users_bp.route("/me/banner", methods=["POST"])
def update_banner_pic():
    if "email" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user = db["emails"].find_one({"email": session["email"]})
    info = process_upload(
        request.files.get("file"),
        "banner",
        user["_id"],
        processing=PROCESSING_WIDTH_LIMIT,
        max_width=1920,
    )
    if not info:
        return jsonify({"success": False}), 400

    delete_upload(user.get("banner_url"))
    db["emails"].update_one(
        {"_id": user["_id"]}, {"$set": {"banner_url": info["stored_filename"]}}
    )
    return jsonify({"success": True, "url": info["url"]})


@users_bp.route("/me/banner/remove", methods=["POST"])
@require_auth
def remove_banner_pic(current_user):
    delete_upload(current_user.get("banner_url"))
    db["emails"].update_one({"_id": current_user["_id"]}, {"$set": {"banner_url": ""}})
    return jsonify({"success": True})


@users_bp.route("/handle/<handle>", methods=["GET"])
@require_auth
def get_user_profile(current_user, handle):
    user = get_user_by_handle(handle)
    if not user:
        return jsonify({"error": "User not found"}), 404

    followers = user.get("followers", [])
    following = user.get("following", [])
    is_following = current_user["_id"] in followers

    # Check Block/Mute status relative to me
    # Have I blocked them?
    is_blocked_by_me = is_blocked(current_user["_id"], user["_id"])
    # Have I muted them?
    is_muted_by_me = is_muted(current_user["_id"], user["_id"])

    return jsonify(
        {
            **serialize_user(user),
            "is_me": user["_id"] == current_user["_id"],
            "followers_count": len(followers),
            "following_count": len(following),
            "is_following": is_following,
            "is_blocked": is_blocked_by_me,
            "is_muted": is_muted_by_me,
        }
    )


# ====================================================================
# BACKGROUND IMAGE
# ====================================================================


@users_bp.route("/me/background", methods=["POST"])
@require_auth
def update_background(current_user):
    info = process_upload(
        request.files.get("file"),
        "bg",
        current_user["_id"],
        processing=PROCESSING_WIDTH_LIMIT,
        max_width=3840,
        allowed_exts=IMAGE_EXTENSIONS,
    )
    if not info:
        return jsonify({"success": False, "error": "Background must be an image"}), 400

    delete_upload(current_user.get("background_url"))
    db["emails"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"background_url": info["stored_filename"]}},
    )
    return jsonify({"success": True, "url": info["url"]})


@users_bp.route("/me/background/remove", methods=["POST"])
@require_auth
def remove_background(current_user):
    delete_upload(current_user.get("background_url"))
    db["emails"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"background_url": "", "background_mode": "cover"}},
    )
    return jsonify({"success": True})


@users_bp.route("/me/background/mode", methods=["POST"])
@require_auth
def update_background_mode(current_user):
    """Set the background display mode: 'cover' or 'tile'."""
    data = request.get_json() if request.is_json else request.form
    mode = (data.get("mode") or "cover").strip().lower()
    if mode not in ("cover", "tile"):
        mode = "cover"

    db["emails"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"background_mode": mode}},
    )
    return jsonify({"success": True, "mode": mode})


# ====================================================================
# FOLLOWER / FOLLOWING LISTS
# ====================================================================


@users_bp.route("/handle/<handle>/followers", methods=["GET"])
@require_auth
def get_followers_list(current_user, handle):
    """Return the detailed list of a user's followers."""
    user = get_user_by_handle(handle)
    if not user:
        return jsonify({"error": "User not found"}), 404

    follower_ids = user.get("followers", [])
    if not follower_ids:
        return jsonify([])

    users = db["emails"].find({"_id": {"$in": follower_ids}})
    result = []
    my_following = set(str(uid) for uid in current_user.get("following", []))
    for u in users:
        # Hide discord.import users
        if u.get("email", "").endswith("@discord.import"):
            continue

        data = serialize_user(u)
        data["is_following"] = str(u["_id"]) in my_following
        data["is_me"] = u["_id"] == current_user["_id"]
        result.append(data)
    return jsonify(result)


@users_bp.route("/handle/<handle>/following", methods=["GET"])
@require_auth
def get_following_list(current_user, handle):
    """Return the detailed list of users someone is following."""
    user = get_user_by_handle(handle)
    if not user:
        return jsonify({"error": "User not found"}), 404

    following_ids = user.get("following", [])
    if not following_ids:
        return jsonify([])

    users = db["emails"].find({"_id": {"$in": following_ids}})
    result = []
    my_following = set(str(uid) for uid in current_user.get("following", []))
    for u in users:
        # Hide discord.import users
        if u.get("email", "").endswith("@discord.import"):
            continue

        data = serialize_user(u)
        data["is_following"] = str(u["_id"]) in my_following
        data["is_me"] = u["_id"] == current_user["_id"]
        result.append(data)
    return jsonify(result)


# ====================================================================
# ALL USERS (for feed members panel)
# ====================================================================


@users_bp.route("/all", methods=["GET"])
@require_auth
def list_all_users(current_user):
    """Return all user accounts (for the members sidebar on Feed)."""
    users = (
        db["emails"]
        .find(
            # NEW: Exclude discord.import users at the DB level
            {"email": {"$not": re.compile(r"@discord\.import$", re.IGNORECASE)}},
            {
                "user_full_name": 1,
                "user_handle": 1,
                "photo_url": 1,
                "banner_url": 1,
                "about_me": 1,
                "status": 1,
            },
        )
        .sort("user_full_name", 1)
        .limit(200)
    )

    result = []
    for u in users:
        result.append(serialize_user(u))
    return jsonify(result)


@users_bp.route("/me/following/suggestions", methods=["GET"])
@require_auth
def following_dm_suggestions(current_user):
    """Return users I follow who I don't have a DM conversation with yet."""
    following_ids = current_user.get("following", [])
    if not following_ids:
        return jsonify([])

    # Get all my DM conversations
    convos = db["dm_conversations"].find({"participants": current_user["_id"]})
    dm_partner_ids = set()
    for c in convos:
        for p in c["participants"]:
            if p != current_user["_id"]:
                dm_partner_ids.add(p)

    # Filter: following but no DM yet
    suggestion_ids = [uid for uid in following_ids if uid not in dm_partner_ids]
    if not suggestion_ids:
        return jsonify([])

    users = db["emails"].find({"_id": {"$in": suggestion_ids}})

    # Filter out using list comprehension
    result = [
        serialize_user(u)
        for u in users
        if not u.get("email", "").endswith("@discord.import")
    ]
    return jsonify(result)


@users_bp.route("/search", methods=["GET"])
@require_auth
def search_users(current_user):
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    regex = re.compile(re.escape(q), re.IGNORECASE)
    users = (
        db["emails"]
        .find(
            {
                # Wrap the search query and exclude discord.import
                "$and": [
                    {"$or": [{"user_handle": regex}, {"user_full_name": regex}]},
                    {
                        "email": {
                            "$not": re.compile(r"@discord\.import$", re.IGNORECASE)
                        }
                    },
                ]
            }
        )
        .limit(20)
    )
    return jsonify([serialize_user(u) for u in users])


@users_bp.route("/me/unread", methods=["GET"])
@require_auth
def get_unread_status(current_user):
    uid = current_user["_id"]

    read_states = {
        str(rs["context_id"]): rs["last_read_at"]
        for rs in db["read_states"].find({"user_id": uid})
    }

    unread_circles = {}

    memberships = db["circle_members"].find({"user_id": uid})
    circle_ids = [m["circle_id"] for m in memberships]

    channels = db["channels"].find(
        {"circle_id": {"$in": circle_ids}, "last_message_at": {"$exists": True}}
    )

    for ch in channels:
        ch_id = str(ch["_id"])
        sid = str(ch["circle_id"])
        last_msg = ch.get("last_message_at")
        last_read = read_states.get(ch_id)

        if last_msg and (not last_read or last_msg > last_read):
            if sid not in unread_circles or last_msg > unread_circles[sid]:
                unread_circles[sid] = last_msg

    # Convert datetimes to isoformat before shipping them down
    formatted_unread = {sid: to_isoformat(dt) for sid, dt in unread_circles.items()}

    return jsonify({"circles": formatted_unread})


@users_bp.route("/me/status", methods=["POST"])
@require_auth
def update_custom_status(current_user):
    data = request.get_json() if request.is_json else request.form
    text = (data.get("status_text") or "").strip()[:100]
    emoji = (data.get("status_emoji") or "").strip()

    db["emails"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"status_text": text, "status_emoji": emoji}},
    )
    return jsonify({"success": True})


@users_bp.route("/block/<target_id>", methods=["POST"])
@require_auth
def block_user_endpoint(current_user, target_id):
    if block_user_dm(current_user["_id"], target_id):
        return jsonify({"success": True, "blocked": True})
    return jsonify({"error": "Failed to block"}), 400


@users_bp.route("/unblock/<target_id>", methods=["POST"])
@require_auth
def unblock_user_endpoint(current_user, target_id):
    if unblock_user_dm(current_user["_id"], target_id):
        return jsonify({"success": True, "blocked": False})
    return jsonify({"error": "Failed to unblock"}), 400


@users_bp.route("/mute/<target_id>", methods=["POST"])
@require_auth
def mute_user_endpoint(current_user, target_id):
    if mute_user_dm(current_user["_id"], target_id):
        return jsonify({"success": True, "muted": True})
    return jsonify({"error": "Failed to mute"}), 400


@users_bp.route("/unmute/<target_id>", methods=["POST"])
@require_auth
def unmute_user_endpoint(current_user, target_id):
    if unmute_user_dm(current_user["_id"], target_id):
        return jsonify({"success": True, "muted": False})
    return jsonify({"error": "Failed to unmute"}), 400


@users_bp.route("/me/delete", methods=["POST"])
@require_auth
def delete_account(current_user):
    user_id = current_user["_id"]

    for key in ("photo_url", "banner_url", "background_url"):
        delete_upload(current_user.get(key))

    # 2. Tombstone the user account (Scramble login & personal data)
    random_suffix = str(uuid.uuid4())[:8]
    db["emails"].update_one(
        {"_id": user_id},
        {
            "$set": {
                "user_full_name": "Deleted User",
                "user_handle": f"deleted_{random_suffix}",
                "email": f"deleted_{random_suffix}@mylo.local",  # Frees up their original email
                "password": "deleted_account_unusable_hash",
                "photo_url": "no-icon.jpg",
                "banner_url": "",
                "background_url": "",
                "about_me": "",
                "status": "offline",
            }
        },
    )

    # 3. Make all of their posts inactive
    db["messages"].update_many({"author_id": user_id}, {"$set": {"is_active": False}})

    # 4. Cleanup push subscriptions & tokens
    db["push_subscriptions"].delete_many({"user_id": user_id})
    db["fcm_tokens"].delete_many({"user_id": user_id})

    # Clear the session
    session.clear()

    return jsonify({"success": True})
