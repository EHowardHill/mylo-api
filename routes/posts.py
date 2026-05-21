# routes/posts.py
#
# UNIFIED ARCHITECTURE: "Posts" are messages. They either belong to
# a specific circle's feed channel, or the global feed channel.

from flask import Blueprint, request, session, jsonify, g
from bson.objectid import ObjectId
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
import os
import datetime
import re

from utils.shared_api import (
    db,
    UPLOAD_FOLDER,
    STATIC_WEB_URL,
    allowed_file,
    is_video_file,
    create_notification,
    fix_photo_path,
    get_feed_general_channel,
    auto_join_feed_circle,
    login_required_for_blueprint,
    public_endpoint,
    to_isoformat,
    can_access_channel,
    mark_context_read,
    get_ignored_user_ids,
)

from utils.encryption import encrypt_text, decrypt_if_encrypted

posts_bp = Blueprint("posts", __name__)
login_required_for_blueprint(posts_bp)


# ====================================================================
# Encryption helpers
# ====================================================================


def _decrypt_msg_text(msg):
    """Decrypt the content field of a message (post)."""
    return decrypt_if_encrypted(msg.get("content", ""), msg.get("encrypted", False))


def _decrypt_comment_text(comment):
    """Decrypt the text field of an embedded comment."""
    return decrypt_if_encrypted(
        comment.get("text", ""), comment.get("encrypted", False)
    )


def _enrich_reshares(messages_list):
    """Fetch original message data for any reshares in the batch."""
    reshare_ids = {m["reshare_id"] for m in messages_list if m.get("reshare_id")}
    if not reshare_ids:
        return {}

    reshared = {}
    r_msgs = list(
        db["messages"].find(
            {
                "_id": {"$in": list(reshare_ids)},
                "is_active": {"$ne": False},
            }
        )
    )
    r_user_ids = {rm["author_id"] for rm in r_msgs}
    r_users = {
        u["_id"]: u for u in db["emails"].find({"_id": {"$in": list(r_user_ids)}})
    }

    for rm in r_msgs:
        author = r_users.get(rm["author_id"], {})
        reshared[rm["_id"]] = {
            "id": str(rm["_id"]),
            "text": _decrypt_msg_text(rm),
            "user_name": author.get("user_full_name", "Unknown"),
            "user_handle": author.get("user_handle", ""),
            "user_photo": fix_photo_path(author.get("photo_url", "no-icon.jpg")),
            "timestamp_iso": to_isoformat(rm["created_at"]),
            "images": [fix_photo_path(u) for u in rm.get("photo_urls", []) if u],
            "videos": [fix_photo_path(u) for u in rm.get("video_urls", []) if u],
        }
    return reshared


def _serialize_poll(poll, current_user_id):
    if not poll:
        return None
    options = poll.get("options", [])
    total_votes = sum(len(opt.get("voter_ids", [])) for opt in options)
    my_vote = None
    serialized_options = []
    for i, opt in enumerate(options):
        voter_ids = opt.get("voter_ids", [])
        count = len(voter_ids)
        pct = round((count / total_votes * 100) if total_votes > 0 else 0)
        if current_user_id and current_user_id in voter_ids:
            my_vote = i
        serialized_options.append(
            {"id": i, "text": opt.get("text", ""), "count": count, "pct": pct}
        )
    return {
        "options": serialized_options,
        "total_votes": total_votes,
        "closed": poll.get("closed", False),
        "my_vote": my_vote,
    }


# ====================================================================
# SERIALIZATION — turns a message doc into the post JSON the frontend expects
# ====================================================================


def _serialize_post(msg, current_user, reshared_dict=None):
    # Get ignored IDs to filter comments
    ignored_ids = []
    if current_user:
        # To avoid querying DB for every single post in a loop,
        # you might want to pass ignored_ids into this function from the route.
        # For now, we'll fetch them or assume they are passed in.
        ignored_ids = [str(uid) for uid in get_ignored_user_ids(current_user["_id"])]

    """Convert a feed message document into the post API format."""
    author = msg.get("author", {})
    plaintext = _decrypt_msg_text(msg)

    # Images
    images = [fix_photo_path(u) for u in msg.get("photo_urls", []) if u]
    # Videos
    videos = [fix_photo_path(u) for u in msg.get("video_urls", []) if u]

    # Comments
    clean_comments = []
    for c in msg.get("comments", []):

        # SKIP if the comment author is ignored
        if str(c["user_id"]) in ignored_ids:
            continue

        c_ts = c.get("timestamp")
        if hasattr(c_ts, "isoformat"):
            c_ts = to_isoformat(c_ts)
        else:
            c_ts = str(c_ts)

        c_likes = [str(uid) for uid in c.get("plus_oners", [])]
        c_author = c.get("author", {})
        comment_plaintext = _decrypt_comment_text(c)

        clean_comments.append(
            {
                "_id": str(c["_id"]),
                "user_id": str(c["user_id"]),
                "text": comment_plaintext,
                "timestamp": c_ts,
                "plus_oners": c_likes,
                "user_name": c_author.get("name", "Unknown"),
                "user_photo": c_author.get("photo", "no-icon.jpg"),
                "user_handle": c_author.get("handle", ""),
            }
        )

    post_data = {
        "id": str(msg["_id"]),
        "text": plaintext,
        "timestamp": msg["created_at"].strftime("%b %d, %Y"),
        "timestamp_iso": to_isoformat(msg["created_at"]),
        "images": images,
        "videos": videos,
        "user_id": str(msg["author_id"]),
        "user_name": author.get("name", "Unknown"),
        "user_handle": author.get("handle", ""),
        "user_photo": author.get("photo", "no-icon.jpg"),
        "plus_one_count": len(msg.get("plus_oners", [])),
        "comments": clean_comments,
        "comments_count": len(clean_comments),
        "liked_by_me": (
            current_user["_id"] in msg.get("plus_oners", []) if current_user else False
        ),
        "poll": _serialize_poll(
            msg.get("poll"), current_user["_id"] if current_user else None
        ),
        "source_channel": msg.get("source_channel"),
    }

    # Reshare
    if reshared_dict and msg.get("reshare_id") and msg["reshare_id"] in reshared_dict:
        post_data["reshared_post"] = reshared_dict[msg["reshare_id"]]

    return post_data


# ====================================================================
# POST LIKES (who +1'd)
# ====================================================================


@posts_bp.route("/<post_id>/likes", methods=["GET", "POST"])
def get_post_likes(post_id):
    if "email" not in session:
        return jsonify([])
    try:
        msg = db["messages"].find_one({"_id": ObjectId(post_id)})
    except Exception:
        return jsonify([])
    if not msg:
        return jsonify([])

    plus_oners = msg.get("plus_oners", [])
    if not plus_oners:
        return jsonify([])

    users = db["emails"].find(
        {"_id": {"$in": plus_oners}},
        {"user_full_name": 1, "user_handle": 1, "photo_url": 1},
    )
    return jsonify(
        [
            {
                "name": u["user_full_name"],
                "handle": u["user_handle"],
                "photo_url": fix_photo_path(u.get("photo_url", "no-icon.jpg")),
            }
            for u in users
        ]
    )


# ====================================================================
# REPORT POST
# ====================================================================


@posts_bp.route("/<post_id>/report", methods=["POST"])
def report_post(post_id):
    user = g.current_user
    msg = db["messages"].find_one({"_id": ObjectId(post_id)})
    if not msg:
        return jsonify({"success": False, "error": "Post not found"}), 404

    db["messages"].update_one(
        {"_id": ObjectId(post_id)},
        {
            "$set": {
                "is_active": False,
                "reported_by": user["_id"],
                "reported_at": datetime.datetime.utcnow(),
            }
        },
    )

    return jsonify({"success": True})


# ====================================================================
# DELETE POST
# ====================================================================


@posts_bp.route("/<post_id>/delete", methods=["POST"])
def delete_post(post_id):
    user = g.current_user
    msg = db["messages"].find_one({"_id": ObjectId(post_id)})

    if msg and msg["author_id"] == user["_id"]:
        # Delete uploaded photos
        for url in msg.get("photo_urls", []):
            try:
                filename = url.split("/")[-1] if "/" in url else url
                path = os.path.join(UPLOAD_FOLDER, filename)
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        # Delete uploaded videos
        for url in msg.get("video_urls", []):
            try:
                filename = url.split("/")[-1] if "/" in url else url
                path = os.path.join(UPLOAD_FOLDER, filename)
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        db["messages"].delete_one({"_id": ObjectId(post_id)})
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Permission denied"}), 403


# ====================================================================
# CREATE POST
# ====================================================================


@posts_bp.route("/create", methods=["POST"])
def create_post_api():
    try:
        user = g.current_user
        text = request.form.get("text")
        files = request.files.getlist("file")
        channel_id_raw = request.form.get("channel_id")
        reshare_id_raw = request.form.get("reshare_id")
        poll_options_raw = request.form.getlist("poll_option")

        # Ensure user is in the global feed circle
        auto_join_feed_circle(user["_id"])

        db["emails"].update_one(
            {"_id": user["_id"]}, {"$set": {"last_active": datetime.datetime.utcnow()}}
        )

        image_urls = []
        video_urls = []

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        for file in files[:4]:
            if file and allowed_file(file.filename):
                filename = secure_filename(
                    f"{int(datetime.datetime.utcnow().timestamp())}_{file.filename}"
                )
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file_url = f"{STATIC_WEB_URL}/uploads/{filename}"

                if is_video_file(file.filename):
                    file.save(filepath)
                    video_urls.append(file_url)
                else:
                    if filename.lower().endswith(".gif"):
                        file.save(filepath)
                    else:
                        img = Image.open(file)
                        img = ImageOps.exif_transpose(img)
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        if img.height > 1920 or img.width > 1920:
                            img.thumbnail((1920, 1920))
                        img.save(filepath)
                    image_urls.append(file_url)

        if text or image_urls or video_urls or reshare_id_raw:
            plaintext = text or ""
            encrypted_text = encrypt_text(plaintext) if plaintext else ""

            # Intelligently route the post based on our context
            target_channel_id = None
            if channel_id_raw:
                target_channel = db["channels"].find_one(
                    {"_id": ObjectId(channel_id_raw)}
                )
                if target_channel and can_access_channel(
                    target_channel, user["_id"], target_channel["circle_id"]
                ):
                    target_channel_id = target_channel["_id"]

            if not target_channel_id:
                # If no valid circle channel was provided, post directly to the global feed
                target_channel_id = get_feed_general_channel()["_id"]

            now = datetime.datetime.utcnow()
            msg_doc = {
                "channel_id": target_channel_id,
                "author_id": user["_id"],
                "content": encrypted_text,
                "encrypted": True,
                "photo_urls": image_urls,
                "video_urls": video_urls,
                "plus_oners": [],
                "comments": [],
                "reactions": [],
                "is_active": True,
                "created_at": now,
                "edited": False,
                "pinned": False,
            }

            # Reshare
            if reshare_id_raw:
                try:
                    msg_doc["reshare_id"] = ObjectId(reshare_id_raw)
                except Exception:
                    pass

            # Poll
            poll_options_clean = [
                o.strip() for o in (poll_options_raw or []) if o.strip()
            ]
            if len(poll_options_clean) >= 2:
                msg_doc["poll"] = {
                    "options": [
                        {"id": i, "text": t, "voter_ids": []}
                        for i, t in enumerate(poll_options_clean)
                    ],
                    "closed": False,
                }

            post_id = db["messages"].insert_one(msg_doc).inserted_id

            # Update channel metadata
            db["channels"].update_one(
                {"_id": target_channel_id},
                {"$set": {"last_message_at": now}},
            )

            # Mention notifications
            if plaintext:
                mentions = re.findall(r"\+([\w]+)", plaintext)
                for handle in set(mentions):
                    mentioned_user = db["emails"].find_one({"user_handle": handle})
                    if mentioned_user and mentioned_user["_id"] != user["_id"]:
                        create_notification(
                            user_id=mentioned_user["_id"],
                            type="mention_post",
                            title=f"{user['user_full_name']} mentioned you in a post",
                            body=plaintext[:80],
                            source_id=user["_id"],
                            context_id=post_id,
                        )

            return jsonify({"success": True, "post_id": str(post_id)})

        return jsonify({"success": False, "error": "Empty post"}), 400

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ====================================================================
# +1 TOGGLE
# ====================================================================


@posts_bp.route("/plus_one/<post_id>", methods=["POST"])
def toggle_plus_one(post_id):
    user = g.current_user
    msg = db["messages"].find_one({"_id": ObjectId(post_id)})

    active = False
    count = 0

    if msg:
        plus_oners = msg.get("plus_oners", [])
        if user["_id"] in plus_oners:
            db["messages"].update_one(
                {"_id": ObjectId(post_id)}, {"$pull": {"plus_oners": user["_id"]}}
            )
            count = len(plus_oners) - 1
            active = False
        else:
            db["messages"].update_one(
                {"_id": ObjectId(post_id)}, {"$addToSet": {"plus_oners": user["_id"]}}
            )
            count = len(plus_oners) + 1
            active = True

            if msg["author_id"] != user["_id"]:
                post_plaintext = _decrypt_msg_text(msg)
                create_notification(
                    user_id=msg["author_id"],
                    type="plus_one_post",
                    title=f"{user['user_full_name']} +1'd your post",
                    body=(post_plaintext[:80] if post_plaintext else ""),
                    source_id=user["_id"],
                    context_id=msg["_id"],
                )

    return jsonify({"success": True, "active": active, "count": count})


# ====================================================================
# COMMENT +1 TOGGLE
# ====================================================================


@posts_bp.route("/plus_one_comment/<post_id>/<comment_id>", methods=["POST"])
def toggle_comment_plus_one(post_id, comment_id):
    user = g.current_user
    msg = db["messages"].find_one(
        {"_id": ObjectId(post_id), "comments._id": ObjectId(comment_id)}
    )
    if not msg:
        return jsonify({"success": False, "message": "Comment not found"})

    target_comment = next(
        (c for c in msg["comments"] if c["_id"] == ObjectId(comment_id)), None
    )
    if not target_comment:
        return jsonify({"success": False})

    plus_oners = target_comment.get("plus_oners", [])
    active = False
    count = 0

    if user["_id"] in plus_oners:
        db["messages"].update_one(
            {"_id": ObjectId(post_id), "comments._id": ObjectId(comment_id)},
            {"$pull": {"comments.$.plus_oners": user["_id"]}},
        )
        count = len(plus_oners) - 1
    else:
        db["messages"].update_one(
            {"_id": ObjectId(post_id), "comments._id": ObjectId(comment_id)},
            {"$addToSet": {"comments.$.plus_oners": user["_id"]}},
        )
        count = len(plus_oners) + 1
        active = True

        if target_comment["user_id"] != user["_id"]:
            comment_plaintext = _decrypt_comment_text(target_comment)
            create_notification(
                user_id=target_comment["user_id"],
                type="plus_one_comment",
                title=f"{user['user_full_name']} +1'd your comment",
                body=(comment_plaintext[:80] if comment_plaintext else ""),
                source_id=user["_id"],
                context_id=msg["_id"],
            )

    return jsonify({"success": True, "active": active, "count": count})


# ====================================================================
# CREATE COMMENT
# ====================================================================


@posts_bp.route("/comment/post", methods=["POST"])
def create_comment():
    user = g.current_user
    post_id = request.form.get("post_id")
    text = request.form.get("comment_text")

    if not post_id or not text:
        return jsonify({"success": False, "error": "Missing post_id or text"}), 400

    db["emails"].update_one(
        {"_id": user["_id"]}, {"$set": {"last_active": datetime.datetime.utcnow()}}
    )

    msg = db["messages"].find_one({"_id": ObjectId(post_id)})
    if not msg:
        return jsonify({"success": False, "error": "Post not found"}), 404

    comment_id = ObjectId()
    plaintext = text
    encrypted_text = encrypt_text(plaintext)

    new_comment = {
        "_id": comment_id,
        "user_id": user["_id"],
        "text": encrypted_text,
        "encrypted": True,
        "timestamp": datetime.datetime.utcnow(),
        "plus_oners": [],
    }

    db["messages"].update_one(
        {"_id": ObjectId(post_id)}, {"$push": {"comments": new_comment}}
    )

    # Notify post owner
    if msg["author_id"] != user["_id"]:
        create_notification(
            user_id=msg["author_id"],
            type="comment_post",
            title=f"{user['user_full_name']} commented on your post",
            body=plaintext[:80],
            source_id=user["_id"],
            context_id=msg["_id"],
        )

    # Notify mentions
    mentions = re.findall(r"\+([\w]+)", plaintext)
    for handle in set(mentions):
        mentioned_user = db["emails"].find_one({"user_handle": handle})
        if mentioned_user and mentioned_user["_id"] != user["_id"]:
            create_notification(
                user_id=mentioned_user["_id"],
                type="mention_post",
                title=f"{user['user_full_name']} mentioned you in a comment",
                body=plaintext[:80],
                source_id=user["_id"],
                context_id=msg["_id"],
            )

    return jsonify(
        {
            "success": True,
            "comment_id": str(comment_id),
            "user_name": user.get("user_full_name"),
            "user_handle": user.get("user_handle"),
            "user_photo": fix_photo_path(user.get("photo_url", "no-icon.jpg")),
            "text": plaintext,
            "timestamp": to_isoformat(new_comment["timestamp"]),
        }
    )


@posts_bp.route("/comment/delete/<post_id>/<comment_id>", methods=["POST"])
def delete_comment(post_id, comment_id):
    user = g.current_user
    result = db["messages"].update_one(
        {"_id": ObjectId(post_id)},
        {"$pull": {"comments": {"_id": ObjectId(comment_id), "user_id": user["_id"]}}},
    )
    if result.modified_count > 0:
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Could not delete"})


# ====================================================================
# FOLLOW / UNFOLLOW USER
# ====================================================================


@posts_bp.route("/follow/<target_id>", methods=["POST"])
def follow_user(target_id):
    user = g.current_user
    target_user = db["emails"].find_one({"_id": ObjectId(target_id)})
    if not target_user:
        return jsonify({"success": False, "message": "User not found"})
    if user["_id"] == target_user["_id"]:
        return jsonify({"success": False, "message": "Cannot follow self"})

    following_list = user.get("following", [])
    is_following = False

    if target_user["_id"] in following_list:
        db["emails"].update_one(
            {"_id": user["_id"]}, {"$pull": {"following": target_user["_id"]}}
        )
        db["emails"].update_one(
            {"_id": target_user["_id"]}, {"$pull": {"followers": user["_id"]}}
        )
        is_following = False
    else:
        db["emails"].update_one(
            {"_id": user["_id"]}, {"$addToSet": {"following": target_user["_id"]}}
        )
        db["emails"].update_one(
            {"_id": target_user["_id"]}, {"$addToSet": {"followers": user["_id"]}}
        )

        is_following = True
        create_notification(
            user_id=target_user["_id"],
            type="follow",
            title=f"{user['user_full_name']} started following you",
            body="",
            source_id=user["_id"],
            context_id=None,
        )

    updated_user = db["emails"].find_one({"_id": target_user["_id"]})
    return jsonify(
        {
            "success": True,
            "following": is_following,
            "followers_count": len(updated_user.get("followers", [])),
            "following_count": len(updated_user.get("following", [])),
        }
    )


# ====================================================================
# BANNER UPLOAD
# ====================================================================


@posts_bp.route("/me/banner", methods=["POST"])
def update_banner_pic():
    user = g.current_user
    file = request.files.get("file")
    if file and allowed_file(file.filename):
        filename = secure_filename(
            f"banner_{user['_id']}_{int(datetime.datetime.utcnow().timestamp())}.{file.filename.rsplit('.', 1)[1]}"
        )
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        if filename.lower().endswith(".gif"):
            file.save(save_path)
        else:
            try:
                img = Image.open(file)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img = ImageOps.exif_transpose(img)
                if img.width > 1920:
                    ratio = 1920 / img.width
                    img = img.resize(
                        (1920, int(img.height * ratio)), Image.Resampling.LANCZOS
                    )
                img.save(save_path)
            except Exception:
                file.seek(0)
                file.save(save_path)

        new_url = f"{STATIC_WEB_URL}/uploads/{filename}"
        old_url = user.get("banner_url")
        if old_url and "uploads" in old_url:
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, old_url.split("/")[-1]))
            except Exception:
                pass
        db["emails"].update_one({"_id": user["_id"]}, {"$set": {"banner_url": new_url}})
        return jsonify({"success": True, "url": new_url})
    return jsonify({"success": False}), 400


# ====================================================================
# FEED ENDPOINT — queries messages
# ====================================================================


@posts_bp.route("/feed")
def get_feed_api():
    current_user = g.current_user

    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 20))
    search_term = request.args.get("q")
    target_user_id = request.args.get("user_id")
    following_only = request.args.get("following")
    post_id = request.args.get("post_id")
    channel_id = request.args.get("channel_id")
    aggregate = request.args.get("aggregate") == "true"  # <-- Add this line
    sort_by = request.args.get("sort", "newest")
    media_type = request.args.get("media_type", "all")

    # Base query
    query_filter = {"is_active": {"$ne": False}}

    ignored_ids = get_ignored_user_ids(current_user["_id"])
    if ignored_ids:
        # If we are already filtering by a specific user (e.g. profile view),
        # MongoDB will naturally return nothing if that user is in our ignored list.
        query_filter["author_id"] = {"$nin": ignored_ids}

    if post_id:
        try:
            query_filter = {"_id": ObjectId(post_id), "is_active": {"$ne": False}}
        except Exception:
            return jsonify({"posts": [], "count": 0})

    elif aggregate:
        # User requested the aggregate feed across all circles
        general_ch = get_feed_general_channel()
        mems = list(db["circle_members"].find({"user_id": current_user["_id"]}))
        sids = [m["circle_id"] for m in mems]

        feed_channels = list(
            db["channels"].find(
                {
                    "$or": [
                        {"circle_id": {"$in": sids}, "channel_type": "feed"},
                        {"_id": general_ch["_id"]},
                    ]
                }
            )
        )

        ch_ids = [
            c["_id"]
            for c in feed_channels
            if can_access_channel(c, current_user["_id"], c["circle_id"])
        ]
        query_filter["channel_id"] = {"$in": ch_ids}

    elif channel_id:
        # We are viewing a specific Circle's Feed Channel.
        try:
            ch_obj = db["channels"].find_one({"_id": ObjectId(channel_id)})
            if not ch_obj or not can_access_channel(
                ch_obj, current_user["_id"], ch_obj["circle_id"]
            ):
                return jsonify({"posts": [], "count": 0})
            query_filter["channel_id"] = ObjectId(channel_id)
            mark_context_read(current_user["_id"], channel_id)
        except Exception:
            return jsonify({"posts": [], "count": 0})

    else:
        # We are in the Global Feed.
        general_ch = get_feed_general_channel()
        query_filter["channel_id"] = general_ch["_id"]
        mark_context_read(current_user["_id"], general_ch["_id"])

        if following_only == "true":
            following_ids = current_user.get("following", [])
            if not following_ids:
                return jsonify({"posts": [], "count": 0, "next_offset": None})
            # Viewing 'Following': Only show global posts by people I follow
            query_filter["author_id"] = {"$in": following_ids + [current_user["_id"]]}

    # GLOBALLY apply the user filter so it works across aggregated feeds and specific channels
    if target_user_id:
        try:
            query_filter["author_id"] = ObjectId(target_user_id)
        except Exception:
            return jsonify({"posts": [], "count": 0})

    # Media type filter
    if media_type == "text":
        query_filter["$and"] = query_filter.get("$and", []) + [
            {
                "$or": [
                    {"photo_urls": {"$exists": False}},
                    {"photo_urls": {"$size": 0}},
                    {"photo_urls": None},
                ]
            },
            {
                "$or": [
                    {"video_urls": {"$exists": False}},
                    {"video_urls": {"$size": 0}},
                    {"video_urls": None},
                ]
            },
        ]
    elif media_type == "photos":
        query_filter["photo_urls"] = {
            "$exists": True,
            "$not": {"$size": 0},
            "$ne": None,
        }
    elif media_type == "videos":
        query_filter["video_urls"] = {
            "$exists": True,
            "$not": {"$size": 0},
            "$ne": None,
        }

    # Search (only works on unencrypted content)
    if search_term:
        query_filter["content"] = {"$regex": re.escape(search_term), "$options": "i"}

    # Fetch messages
    if sort_by == "popular":
        pipeline = [
            {"$match": query_filter},
            {
                "$addFields": {
                    "plus_one_count_sort": {"$size": {"$ifNull": ["$plus_oners", []]}}
                }
            },
            {"$sort": {"plus_one_count_sort": -1, "created_at": -1}},
            {"$skip": offset},
            {"$limit": limit},
        ]
        messages = list(db["messages"].aggregate(pipeline))
    elif sort_by == "oldest":
        messages = list(
            db["messages"]
            .find(query_filter)
            .sort("created_at", 1)
            .skip(offset)
            .limit(limit)
        )
    else:
        messages = list(
            db["messages"]
            .find(query_filter)
            .sort("created_at", -1)
            .skip(offset)
            .limit(limit)
        )

    if not messages:
        return jsonify({"posts": [], "count": 0, "next_offset": None})

    # Enrich with author data and channel data
    user_ids = set()
    channel_ids = set()
    for m in messages:
        user_ids.add(m["author_id"])
        if "channel_id" in m:
            channel_ids.add(m["channel_id"])
        for c in m.get("comments", []):
            user_ids.add(c["user_id"])

    users = {}
    if user_ids:
        for u in db["emails"].find({"_id": {"$in": list(user_ids)}}):
            users[u["_id"]] = {
                "name": u.get("user_full_name", "Unknown"),
                "photo": fix_photo_path(u.get("photo_url", "no-icon.jpg")),
                "handle": u.get("user_handle", ""),
            }

    # Build the channel/circle lineage map
    channels_map = {}
    if channel_ids:
        ch_list = list(db["channels"].find({"_id": {"$in": list(channel_ids)}}))
        circle_ids = {ch["circle_id"] for ch in ch_list if "circle_id" in ch}
        circles_map = {
            c["_id"]: c for c in db["circles"].find({"_id": {"$in": list(circle_ids)}})
        }

        for ch in ch_list:
            cid = ch.get("circle_id")
            circle = circles_map.get(cid, {})
            channels_map[ch["_id"]] = {
                "channel_id": str(ch["_id"]),
                "channel_name": ch.get("name", ""),
                "circle_id": str(cid) if cid else None,
                "circle_name": circle.get("name", "Mylo Global Feed"),
                "circle_type": circle.get("circle_type", "feed"),
            }

    for m in messages:
        m["author"] = users.get(
            m["author_id"],
            {"name": "Unknown", "photo": fix_photo_path("no-icon.jpg"), "handle": ""},
        )

        # Attach the source channel to the message doc before serialization
        if m.get("channel_id") and m["channel_id"] in channels_map:
            m["source_channel"] = channels_map[m["channel_id"]]
        else:
            m["source_channel"] = None

        for c in m.get("comments", []):
            c["author"] = users.get(
                c["user_id"],
                {
                    "name": "Unknown",
                    "photo": fix_photo_path("no-icon.jpg"),
                    "handle": "",
                },
            )

    reshared_dict = _enrich_reshares(messages)

    serialized = [_serialize_post(m, current_user, reshared_dict) for m in messages]

    return jsonify(
        {
            "posts": serialized,
            "count": len(serialized),
            "next_offset": (
                offset + len(serialized) if len(serialized) == limit else None
            ),
        }
    )


# ====================================================================
# PUBLIC POST VIEW
# ====================================================================


@posts_bp.route("/public/<post_id>", methods=["GET"])
@public_endpoint
def get_public_post(post_id):
    try:
        msg = db["messages"].find_one(
            {"_id": ObjectId(post_id), "is_active": {"$ne": False}}
        )
        if not msg:
            return jsonify({"success": False, "error": "Post not found"}), 404

        # --- NEW: Enrich source channel/circle ---
        msg["source_channel"] = None
        if msg.get("channel_id"):
            ch = db["channels"].find_one({"_id": msg["channel_id"]})
            if ch:
                cid = ch.get("circle_id")
                circle = db["circles"].find_one({"_id": cid}) if cid else {}
                msg["source_channel"] = {
                    "channel_id": str(ch["_id"]),
                    "channel_name": ch.get("name", ""),
                    "circle_id": str(cid) if cid else None,
                    "circle_name": (
                        circle.get("name", "Mylo Global Feed")
                        if circle
                        else "Mylo Global Feed"
                    ),
                    "circle_type": (
                        circle.get("circle_type", "feed") if circle else "feed"
                    ),
                }
        # -----------------------------------------

        # Enrich Author
        author = db["emails"].find_one({"_id": msg["author_id"]})
        msg["author"] = {
            "name": author.get("user_full_name", "Unknown") if author else "Unknown",
            "photo": (
                fix_photo_path(author.get("photo_url", "no-icon.jpg"))
                if author
                else fix_photo_path("no-icon.jpg")
            ),
            "handle": author.get("user_handle", "") if author else "",
        }

        # Enrich Comments
        for c in msg.get("comments", []):
            c_author = db["emails"].find_one({"_id": c["user_id"]})
            c["author"] = {
                "name": (
                    c_author.get("user_full_name", "Unknown") if c_author else "Unknown"
                ),
                "photo": (
                    fix_photo_path(c_author.get("photo_url", "no-icon.jpg"))
                    if c_author
                    else fix_photo_path("no-icon.jpg")
                ),
                "handle": c_author.get("user_handle", "") if c_author else "",
            }

        reshared_dict = _enrich_reshares([msg])
        post_data = _serialize_post(msg, None, reshared_dict)
        post_data["liked_by_me"] = False

        return jsonify({"success": True, "post": post_data})

    except Exception as e:
        print(f"Public post error: {e}")
        return jsonify({"success": False, "error": "Invalid ID or Circle Error"}), 400


# ====================================================================
# POLL ENDPOINTS
# ====================================================================


@posts_bp.route("/<post_id>/vote", methods=["POST"])
def vote_poll(post_id):
    user = g.current_user
    data = request.get_json() or {}
    option_id = data.get("option_id")
    if option_id is None:
        return jsonify({"success": False, "error": "option_id required"}), 400
    try:
        option_id = int(option_id)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid option_id"}), 400

    msg = db["messages"].find_one(
        {"_id": ObjectId(post_id), "is_active": {"$ne": False}}
    )
    if not msg:
        return jsonify({"success": False, "error": "Post not found"}), 404

    poll = msg.get("poll")
    if not poll:
        return jsonify({"success": False, "error": "This post has no poll"}), 400
    if poll.get("closed"):
        return jsonify({"success": False, "error": "This poll is closed"}), 400

    options = poll.get("options", [])
    if option_id < 0 or option_id >= len(options):
        return jsonify({"success": False, "error": "Invalid option"}), 400

    uid = user["_id"]
    for i, opt in enumerate(options):
        if uid in opt.get("voter_ids", []):
            db["messages"].update_one(
                {"_id": ObjectId(post_id)},
                {"$pull": {f"poll.options.{i}.voter_ids": uid}},
            )

    db["messages"].update_one(
        {"_id": ObjectId(post_id)},
        {"$addToSet": {f"poll.options.{option_id}.voter_ids": uid}},
    )

    updated = db["messages"].find_one({"_id": ObjectId(post_id)})
    return jsonify({"success": True, "poll": _serialize_poll(updated.get("poll"), uid)})


@posts_bp.route("/<post_id>/unvote", methods=["POST"])
def unvote_poll(post_id):
    user = g.current_user
    msg = db["messages"].find_one(
        {"_id": ObjectId(post_id), "is_active": {"$ne": False}}
    )
    if not msg:
        return jsonify({"success": False, "error": "Post not found"}), 404
    poll = msg.get("poll")
    if not poll:
        return jsonify({"success": False, "error": "This post has no poll"}), 400
    if poll.get("closed"):
        return jsonify({"success": False, "error": "This poll is closed"}), 400

    uid = user["_id"]
    for i, opt in enumerate(poll.get("options", [])):
        if uid in opt.get("voter_ids", []):
            db["messages"].update_one(
                {"_id": ObjectId(post_id)},
                {"$pull": {f"poll.options.{i}.voter_ids": uid}},
            )

    updated = db["messages"].find_one({"_id": ObjectId(post_id)})
    return jsonify({"success": True, "poll": _serialize_poll(updated.get("poll"), uid)})


@posts_bp.route("/<post_id>/poll/close", methods=["POST"])
def close_poll(post_id):
    user = g.current_user
    msg = db["messages"].find_one({"_id": ObjectId(post_id)})
    if not msg:
        return jsonify({"success": False, "error": "Post not found"}), 404
    if msg["author_id"] != user["_id"]:
        return (
            jsonify({"success": False, "error": "Only the author can close a poll"}),
            403,
        )
    poll = msg.get("poll")
    if not poll:
        return jsonify({"success": False, "error": "This post has no poll"}), 400

    db["messages"].update_one(
        {"_id": ObjectId(post_id)}, {"$set": {"poll.closed": True}}
    )
    updated = db["messages"].find_one({"_id": ObjectId(post_id)})
    return jsonify(
        {"success": True, "poll": _serialize_poll(updated.get("poll"), user["_id"])}
    )


@posts_bp.route("/<post_id>/poll/voters/<int:option_id>", methods=["GET"])
def poll_voters(post_id, option_id):
    msg = db["messages"].find_one(
        {"_id": ObjectId(post_id), "is_active": {"$ne": False}}
    )
    if not msg:
        return jsonify([])
    poll = msg.get("poll")
    if not poll:
        return jsonify([])
    options = poll.get("options", [])
    if option_id < 0 or option_id >= len(options):
        return jsonify([])
    voter_ids = options[option_id].get("voter_ids", [])
    if not voter_ids:
        return jsonify([])
    voters = db["emails"].find(
        {"_id": {"$in": voter_ids}},
        {"user_full_name": 1, "user_handle": 1, "photo_url": 1},
    )
    return jsonify(
        [
            {
                "name": u["user_full_name"],
                "handle": u["user_handle"],
                "photo_url": fix_photo_path(u.get("photo_url", "no-icon.jpg")),
            }
            for u in voters
        ]
    )
