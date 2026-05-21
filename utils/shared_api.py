# shared.py
#
# UNIFIED ARCHITECTURE:
#   - The "Feed" is a single global circle (circle_type="feed").
#   - "Collections" are channels (channel_type="collection") in the feed circle.
#   - "Posts" are messages in feed-circle channels.
#   - All users are auto-joined to the feed circle on registration.

import os
import json
import pymongo
from werkzeug.security import generate_password_hash, check_password_hash
from bson.objectid import ObjectId
import datetime
import secrets
import re
import uuid
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import g, session, jsonify, request
from functools import wraps

import firebase_admin
from firebase_admin import credentials, messaging

from celery import Celery

client = pymongo.MongoClient("mongodb://localhost:27017/")
db = client["mylo"]

try:
    cred_path = os.path.expanduser("/var/www/Mylo/firebase-adminsdk.json")
    if os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("[Firebase] Admin SDK initialized.")
    else:
        print("[Firebase] Warning: firebase-adminsdk.json not found.")
except Exception as e:
    print(f"[Firebase] Initialization error: {e}")

# ---------------------------------------------------------------------------
# Hardwired Static Paths
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.expanduser("/var/www/Mylo/static")
STATIC_WEB_URL = "https://cinemint.online/mylo/static"
UPLOAD_FOLDER = "/var/www/mylo_uploads/"

ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "ico",
    "mp4",
    "webm",
    "mov",
    "avi",
    "mkv",
    "mp3",
    "wav",
    "ogg",
    "flac",
    "aac",
    "m4a",
    "wma",
    "txt",
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "odp",
    "rtf",
    "csv",
    "tsv",
    "zip",
    "rar",
    "7z",
    "tar",
    "gz",
    "bz2",
    "xz",
    "ttf",
    "otf",
    "woff",
    "woff2",
}

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"}
VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "avi", "mkv"}
AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"}

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

# ---------------------------------------------------------------------------
# Channel types
# ---------------------------------------------------------------------------
CHANNEL_TYPE_CHAT = "chat"  # Standard text chat (circles)
CHANNEL_TYPE_FEED = "feed"  # Main feed timeline (global feed circle)
CHANNEL_TYPE_COLLECTION = "collection"  # Themed group of posts (global feed circle)

# ---------------------------------------------------------------------------
# Circle types
# ---------------------------------------------------------------------------
SERVER_TYPE_COMMUNITY = "community"
SERVER_TYPE_FEED = "feed"
SERVER_TYPE_DM = "dm"
SERVER_TYPE_GROUP_DM = "group_dm"

FEED_SERVER_NAME = "Mylo Global Feed"

# ---------------------------------------------------------------------------
# Celery Configuration
# ---------------------------------------------------------------------------
celery_app = Celery(
    "mylo_tasks",
    broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


def safe_oid(id_str):
    try:
        return ObjectId(id_str) if id_str else None
    except Exception:
        return None


def get_file_extension(filename):
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def is_image_file(filename):
    return get_file_extension(filename) in IMAGE_EXTENSIONS


def is_video_file(filename):
    return get_file_extension(filename) in VIDEO_EXTENSIONS


def is_audio_file(filename):
    return get_file_extension(filename) in AUDIO_EXTENSIONS


# ---------------------------------------------------------------------------
# VAPID / Web Push Configuration
# ---------------------------------------------------------------------------


def get_vapid_keys():
    public_key = os.environ.get("MYLO_VAPID_PUBLIC_KEY")
    private_key = os.environ.get("MYLO_VAPID_PRIVATE_KEY")
    claims_email = os.environ.get("MYLO_VAPID_CLAIMS_EMAIL", "mailto:admin@example.com")
    if not public_key or not private_key:
        return None
    return {"public": public_key, "private": private_key, "claims_email": claims_email}


@celery_app.task(name="shared.send_push_to_user_task")
def send_push_to_user_task(
    user_id_str,
    title,
    body,
    tag=None,
    url=None,
    context_id_str=None,
    notification_type=None,
    icon_url=None,
    image_url=None,
):
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        pass  # Don't exit early, FCM might still work

    keys = get_vapid_keys()
    user_oid = safe_oid(user_id_str)

    subscriptions = list(db["push_subscriptions"].find({"user_id": user_oid}))
    fcm_tokens = list(db["fcm_tokens"].find({"user_id": user_oid}))

    if not subscriptions and not fcm_tokens:
        return 0

    success_count = 0

    # --- WebPush Logic ---
    if keys and subscriptions:
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "tag": tag
                or f"mylo-{notification_type or 'notif'}-{datetime.datetime.utcnow().timestamp():.0f}",
                "url": url or "./",
                "icon": icon_url or f"{STATIC_WEB_URL}/icons/icon-192.png",
                "badge": f"{STATIC_WEB_URL}/icons/badge-72.png",
                "image": image_url,
                "context_id": context_id_str,
                "type": notification_type or "general",
                "timestamp": int(datetime.datetime.utcnow().timestamp() * 1000),
                "renotify": True,
            }
        )
        vapid_claims = {"sub": keys["claims_email"]}
        stale_endpoints = []

        for sub in subscriptions:
            subscription_info = {
                "endpoint": sub["endpoint"],
                "keys": sub.get("keys", {}),
            }
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=keys["private"],
                    vapid_claims=vapid_claims,
                    timeout=10,
                )
                success_count += 1
            except WebPushException as e:
                # ... keep your existing WebPush exception handling ...
                status_code = getattr(e, "response", None)
                if status_code and hasattr(status_code, "status_code"):
                    if status_code.status_code in (404, 410):
                        stale_endpoints.append(sub["endpoint"])

        if stale_endpoints:
            db["push_subscriptions"].delete_many({"endpoint": {"$in": stale_endpoints}})

    # --- FCM Logic ---
    stale_fcm_tokens = []
    for fcm_doc in fcm_tokens:
        try:
            fcm_msg = messaging.Message(
                notification=messaging.Notification(
                    title=title, body=body, image=image_url or None
                ),
                data={
                    "url": url or "./",
                    "context_id": context_id_str or "",  # <-- FIXED
                    "type": notification_type or "general",
                    "icon_url": icon_url or "",
                    "image_url": image_url or "",
                },
                token=fcm_doc["token"],
                android=messaging.AndroidConfig(priority="high"),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(content_available=True)
                    )
                ),
            )
            messaging.send(fcm_msg)
            success_count += 1
        except Exception as e:
            if getattr(e, "code", None) == "UNREGISTERED":
                stale_fcm_tokens.append(fcm_doc["token"])
            else:
                print(f"[FCM] Error sending to token: {e}")

    if stale_fcm_tokens:
        db["fcm_tokens"].delete_many({"token": {"$in": stale_fcm_tokens}})

    return success_count


# ---------------------------------------------------------------------------
# Photo / URL helpers
# ---------------------------------------------------------------------------


def fix_photo_path(path):
    if not path or path == "no-icon.jpg":
        return f"{STATIC_WEB_URL}/uploads/no-icon.jpg"
    if path.startswith("http"):
        return path
    filename = os.path.basename(path)
    return f"{STATIC_WEB_URL}/uploads/{filename}"


def serialize_user(user):
    bg = user.get("background_url", "")
    if bg and not bg.startswith("http"):
        bg = f"{STATIC_WEB_URL}/uploads/{bg}"
    return {
        "id": str(user["_id"]),
        "handle": user["user_handle"],
        "name": user["user_full_name"],
        "photo_url": fix_photo_path(user.get("photo_url", "no-icon.jpg")),
        "banner_url": user.get("banner_url", ""),
        "about": user.get("about_me", ""),
        "status": user.get("status", "offline"),
        "status_text": user.get("status_text", ""),
        "status_emoji": user.get("status_emoji", ""),
        "background_url": bg,
        "background_mode": user.get("background_mode", "cover"),
    }


def create_notification(
    user_id,
    type,
    title,
    body,
    source_id,
    context_id=None,
    icon_url=None,
    image_url=None,
):
    if str(user_id) == str(source_id):
        return

    if type == "follow" and not context_id:
        context_id = source_id

    if not icon_url:
        source_user = db["emails"].find_one({"_id": safe_oid(source_id)})
        if source_user:
            icon_url = fix_photo_path(source_user.get("photo_url", "no-icon.jpg"))

    db["notifications"].insert_one(
        {
            "user_id": safe_oid(user_id),
            "type": type,
            "title": title,
            "body": body,
            "source_user_id": safe_oid(source_id),
            "context_id": safe_oid(context_id) if context_id else None,
            "is_read": False,
            "created_at": datetime.datetime.utcnow(),
        }
    )

    push_url = "./"
    if type in ("reply", "mention", "channel_message"):
        push_url = f"./#channel/{context_id}" if context_id else "./"
    elif type in ("dm", "dm_message"):
        push_url = f"./#dm/{context_id}" if context_id else "./#dm"
    elif type in ("follow",):
        source_user = db["emails"].find_one({"_id": safe_oid(source_id)})
        handle = source_user.get("user_handle", "") if source_user else ""
        push_url = f"./#user/{handle}" if handle else "./#feed"
    elif type in ("plus_one_post", "comment_post", "plus_one_comment", "mention_post"):
        push_url = f"./#post/{context_id}" if context_id else "./#feed"

    # Queue the push notification in Celery
    send_push_to_user_task.delay(
        user_id_str=str(user_id),
        title=title,
        body=body[:200] if body else "",
        tag=f"mylo-{type}-{source_id}",
        url=push_url,
        context_id_str=str(context_id) if context_id else None,
        notification_type=type,
        icon_url=icon_url,
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# Global Feed Circle
# ---------------------------------------------------------------------------

# Module-level cache so we don't query every request
_feed_circle_cache = None


def get_or_create_feed_circle():
    """
    Return the single global feed circle document, creating it if needed.
    Result is cached in-process.
    """
    global _feed_circle_cache
    if _feed_circle_cache is not None:
        return _feed_circle_cache

    circle = db["circles"].find_one({"circle_type": SERVER_TYPE_FEED})
    if circle:
        _feed_circle_cache = circle
        return circle

    now = datetime.datetime.utcnow()
    circle_doc = {
        "name": FEED_SERVER_NAME,
        "icon_url": "",
        "owner_id": None,
        "invite_code": generate_invite_code(),
        "is_public": True,
        "circle_type": SERVER_TYPE_FEED,
        "created_at": now,
    }
    result = db["circles"].insert_one(circle_doc)
    circle_id = result.inserted_id

    # Create the default "general" feed channel
    db["channels"].insert_one(
        {
            "circle_id": circle_id,
            "name": "general",
            "description": "Main feed",
            "channel_type": CHANNEL_TYPE_FEED,
            "folder_id": None,
            "position": 0,
            "permission_tags": [],
            "slowmode_seconds": 0,
            "visibility": "public",
            "color": "#4285f4",
            "cover_url": "",
            "followers": [],
            "post_count": 0,
            "owner_id": None,
            "created_at": now,
        }
    )

    circle_doc["_id"] = circle_id
    _feed_circle_cache = circle_doc
    return circle_doc


def get_feed_general_channel():
    """Return the 'general' feed channel in the global feed circle."""
    feed = get_or_create_feed_circle()
    ch = db["channels"].find_one(
        {
            "circle_id": feed["_id"],
            "channel_type": CHANNEL_TYPE_FEED,
            "name": "general",
        }
    )
    if not ch:
        # Fallback: create it
        now = datetime.datetime.utcnow()
        result = db["channels"].insert_one(
            {
                "circle_id": feed["_id"],
                "name": "general",
                "description": "Main feed",
                "channel_type": CHANNEL_TYPE_FEED,
                "folder_id": None,
                "position": 0,
                "permission_tags": [],
                "slowmode_seconds": 0,
                "visibility": "public",
                "color": "#4285f4",
                "cover_url": "",
                "followers": [],
                "post_count": 0,
                "owner_id": None,
                "created_at": now,
            }
        )
        ch = db["channels"].find_one({"_id": result.inserted_id})
    return ch


def get_feed_channel_ids():
    """Return all channel IDs belonging to the global feed circle."""
    feed = get_or_create_feed_circle()
    return [
        ch["_id"] for ch in db["channels"].find({"circle_id": feed["_id"]}, {"_id": 1})
    ]


def auto_join_feed_circle(user_id):
    """Ensure a user is a member of the global feed circle."""
    feed = get_or_create_feed_circle()
    existing = db["circle_members"].find_one(
        {
            "circle_id": feed["_id"],
            "user_id": safe_oid(user_id),
        }
    )
    if not existing:
        db["circle_members"].insert_one(
            {
                "circle_id": feed["_id"],
                "user_id": safe_oid(user_id),
                "tags": ["member"],
                "pinned": True,  # Ensure it starts pinned
                "joined_at": datetime.datetime.utcnow(),
            }
        )
    return feed


# ---------------------------------------------------------------------------
# Database indexes
# ---------------------------------------------------------------------------


def ensure_indexes():
    """Create indexes for performance."""
    db["emails"].create_index("email", unique=True)
    db["emails"].create_index("user_handle", unique=True)
    db["circles"].create_index("invite_code", unique=True, sparse=True)
    db["circles"].create_index("circle_type", sparse=True)
    db["circle_members"].create_index([("circle_id", 1), ("user_id", 1)], unique=True)
    db["channels"].create_index([("circle_id", 1), ("position", 1)])
    db["channels"].create_index([("circle_id", 1), ("channel_type", 1)])
    db["channels"].create_index([("followers", 1)])
    db["channel_folders"].create_index([("circle_id", 1), ("position", 1)])

    # Unified Messages index
    db["messages"].create_index([("channel_id", 1), ("created_at", -1)])
    db["messages"].create_index([("author_id", 1), ("created_at", -1)])
    db["messages"].create_index("is_active", sparse=True)

    db["notifications"].create_index(
        [("user_id", 1), ("is_read", 1), ("created_at", -1)]
    )
    db["read_states"].create_index([("user_id", 1), ("context_id", 1)], unique=True)
    db["push_subscriptions"].create_index("endpoint", unique=True)
    db["push_subscriptions"].create_index("user_id")

    db["bans"].create_index([("circle_id", 1), ("user_id", 1)], unique=True)
    db["mutes"].create_index([("circle_id", 1), ("user_id", 1)], unique=True)
    db["mutes"].create_index("expires_at")
    db["warnings"].create_index([("circle_id", 1), ("user_id", 1), ("created_at", -1)])
    db["mod_log"].create_index([("circle_id", 1), ("created_at", -1)])

    db["dm_blocks"].create_index([("blocker_id", 1), ("blocked_id", 1)], unique=True)
    db["dm_mutes"].create_index([("muter_id", 1), ("muted_id", 1)], unique=True)

    db["circle_roles"].create_index([("circle_id", 1), ("position", -1)])
    db["circle_roles"].create_index([("circle_id", 1), ("is_default", 1)])
    db["custom_emojis"].create_index([("circle_id", 1), ("name", 1)], unique=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def generate_invite_code():
    return secrets.token_urlsafe(8)


def dm_conversation_id(user_a, user_b):
    ids = sorted([str(user_a), str(user_b)])
    return f"{ids[0]}_{ids[1]}"


def get_user_details(user_id):
    try:
        user = db["emails"].find_one({"_id": safe_oid(user_id)})
        if user:
            return {
                "id": str(user["_id"]),
                "name": user.get("user_full_name", "Unknown"),
                "photo": fix_photo_path(user.get("photo_url", "no-icon.jpg")),
                "handle": user.get("user_handle", ""),
            }
    except Exception:
        pass
    return {
        "id": str(user_id),
        "name": "Unknown",
        "photo": fix_photo_path("no-icon.jpg"),
        "handle": "",
    }


def get_user_by_email(email):
    return db["emails"].find_one({"email": email})


def get_user_by_handle(handle):
    return db["emails"].find_one({"user_handle": handle})


def get_user_by_id(uid):
    return db["emails"].find_one({"_id": safe_oid(uid)})


def mark_context_read(user_id, context_id):
    try:
        db["read_states"].update_one(
            {"user_id": safe_oid(user_id), "context_id": safe_oid(context_id)},
            {"$set": {"last_read_at": datetime.datetime.utcnow()}},
            upsert=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def create_user(email, password, user_handle, user_full_name):
    if db["emails"].find_one({"email": email}):
        return False
    if db["emails"].find_one({"user_handle": user_handle}):
        return False

    hashed_password = generate_password_hash(password)
    result = db["emails"].insert_one(
        {
            "email": email,
            "password": hashed_password,
            "user_handle": user_handle,
            "user_full_name": user_full_name,
            "photo_url": "no-icon.jpg",
            "banner_url": "",
            "about_me": "",
            "status": "online",
            "created_at": datetime.datetime.utcnow(),
        }
    )

    # Auto-join the global feed circle
    auto_join_feed_circle(result.inserted_id)

    return True


def verify_user(email, password):
    user = db["emails"].find_one({"email": email})
    if user and check_password_hash(user["password"], password):
        return user
    return None


# ---------------------------------------------------------------------------
# Circle helpers
# ---------------------------------------------------------------------------


def create_circle(name, owner_id, is_public=False, icon_url=""):
    invite_code = generate_invite_code()
    circle = {
        "name": name,
        "icon_url": icon_url,
        "owner_id": safe_oid(owner_id),
        "invite_code": invite_code,
        "is_public": is_public,
        "circle_type": SERVER_TYPE_COMMUNITY,
        "created_at": datetime.datetime.utcnow(),
    }
    result = db["circles"].insert_one(circle)
    circle_id = result.inserted_id

    db["circle_members"].insert_one(
        {
            "circle_id": circle_id,
            "user_id": safe_oid(owner_id),
            "tags": ["owner", "admin"],
            "joined_at": datetime.datetime.utcnow(),
        }
    )

    db["channels"].insert_one(
        {
            "circle_id": circle_id,
            "name": "general",
            "description": "General discussion",
            "channel_type": CHANNEL_TYPE_CHAT,
            "folder_id": None,
            "position": 0,
            "permission_tags": [],
            "slowmode_seconds": 0,
            "created_at": datetime.datetime.utcnow(),
        }
    )
    return circle_id, invite_code


def join_circle_by_invite(invite_code, user_id):
    circle = db["circles"].find_one({"invite_code": invite_code})
    if not circle:
        return None
    if is_user_banned(circle["_id"], user_id):
        return None
    existing = db["circle_members"].find_one(
        {
            "circle_id": circle["_id"],
            "user_id": safe_oid(user_id),
        }
    )
    if existing:
        return circle["_id"]
    db["circle_members"].insert_one(
        {
            "circle_id": circle["_id"],
            "user_id": safe_oid(user_id),
            "tags": ["member"],
            "joined_at": datetime.datetime.utcnow(),
        }
    )

    # Auto-subscribe user to the first channel in the circle
    first_ch = db["channels"].find_one(
        {"circle_id": circle["_id"]}, sort=[("position", 1)]
    )
    if first_ch:
        db["channel_alerts"].update_one(
            {"user_id": safe_oid(user_id), "channel_id": first_ch["_id"]},
            {"$set": {"user_id": safe_oid(user_id), "channel_id": first_ch["_id"]}},
            upsert=True,
        )

    return circle["_id"]


def get_member(circle_id, user_id):
    return db["circle_members"].find_one(
        {
            "circle_id": safe_oid(circle_id),
            "user_id": safe_oid(user_id),
        }
    )


def member_has_tag(circle_id, user_id, tag):
    mem = get_member(circle_id, user_id)
    if not mem:
        return False
    return tag in mem.get("tags", [])


def member_has_any_tag(circle_id, user_id, tags):
    mem = get_member(circle_id, user_id)
    if not mem:
        return False
    return bool(set(tags) & set(mem.get("tags", [])))


def can_access_channel(channel, user_id, circle_id):
    perm_tags = channel.get("permission_tags", [])
    if not perm_tags:
        return True
    mem = get_member(circle_id, user_id)
    if not mem:
        return False
    user_tags = set(mem.get("tags", []))
    if "owner" in user_tags or "admin" in user_tags:
        return True
    return bool(set(perm_tags) & user_tags)


# ---------------------------------------------------------------------------
# Moderation helpers
# ---------------------------------------------------------------------------

MOD_ACTIONS = (
    "ban",
    "unban",
    "kick",
    "mute",
    "unmute",
    "warn",
    "warn_delete",
    "purge",
    "slowmode",
)


def log_mod_action(
    circle_id, action, moderator_id, target_user_id=None, reason="", details=None
):
    doc = {
        "circle_id": safe_oid(circle_id),
        "action": action,
        "moderator_id": safe_oid(moderator_id),
        "target_user_id": safe_oid(target_user_id) if target_user_id else None,
        "reason": reason or "",
        "details": details or {},
        "created_at": datetime.datetime.utcnow(),
    }
    db["mod_log"].insert_one(doc)


def is_user_banned(circle_id, user_id):
    return (
        db["bans"].find_one(
            {"circle_id": safe_oid(circle_id), "user_id": safe_oid(user_id)}
        )
        is not None
    )


def is_user_muted(circle_id, user_id):
    mute = db["mutes"].find_one(
        {"circle_id": safe_oid(circle_id), "user_id": safe_oid(user_id)}
    )
    if not mute:
        return None
    if mute.get("expires_at") and mute["expires_at"] <= datetime.datetime.utcnow():
        db["mutes"].delete_one({"_id": mute["_id"]})
        return None
    return mute


def get_mute_remaining(mute_doc):
    if not mute_doc or not mute_doc.get("expires_at"):
        return "Indefinite"
    remaining = mute_doc["expires_at"] - datetime.datetime.utcnow()
    if remaining.total_seconds() <= 0:
        return "Expired"
    mins = int(remaining.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h {mins % 60}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_markdown_lite(text, custom_emojis=None):
    if not text:
        return ""

    text = text.replace("<", "&lt;").replace(">", "&gt;")

    code_placeholders = []

    def store_code(match):
        is_block = match.group(0).startswith("```")
        content = match.group(1)
        html = (
            f"<pre><code>{content}</code></pre>"
            if is_block
            else f"<code>{content}</code>"
        )
        placeholder = f"__CODE_BLOCK_{len(code_placeholders)}__"
        code_placeholders.append(html)
        return placeholder

    text = re.sub(r"```(.*?)```", store_code, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", store_code, text)
    text = re.sub(r"\|\|(.*?)\|\|", r'<span class="spoiler">\1</span>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)

    text = re.sub(
        r"(?<!\w)\+([a-zA-Z0-9_]+)",
        r'<span class="text-gp-blue font-medium cursor-pointer hover:underline mention-link" data-handle="\1">+\1</span>',
        text,
    )
    text = re.sub(
        r"(?<!\w)#([a-z0-9\-]+)",
        r'<span class="text-gp-blue font-medium cursor-pointer hover:underline channel-link" data-channel="\1">#\1</span>',
        text,
    )

    if custom_emojis:
        for ce in custom_emojis:
            name = ce["name"]
            url = fix_photo_path(ce["image_url"])
            pattern = r"(?<!\w):" + re.escape(name) + r":(?!\w)"
            img_tag = f'<img src="{url}" alt=":{name}:" title=":{name}:" class="custom-emoji inline-block h-6 align-middle">'
            text = re.sub(pattern, img_tag, text)

    def _yt_embed(m):
        vid = m.group(2)
        full_url = m.group(1)
        start = ""
        t = re.search(r"[?&]t=(\d+)", full_url)
        if t:
            start = f"?start={t.group(1)}"
        return (
            f'<div class="yt-embed">'
            f'<iframe src="https://www.youtube.com/embed/{vid}{start}" '
            f'frameborder="0" allow="accelerometer; autoplay; clipboard-write; '
            f'encrypted-media; gyroscope; picture-in-picture" '
            f'allowfullscreen loading="lazy"></iframe></div>'
        )

    text = re.sub(
        r'(?<!")(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})(?:[^\s<"]*)?)',
        _yt_embed,
        text,
        flags=re.IGNORECASE,
    )

    img_regex = r'(?<!")((https?://[^\s<"]+\.(?:jpg|jpeg|png|gif|webp))(\?[^\s<"]*)?)'
    text = re.sub(
        img_regex,
        lambda m: f'<br><a href="{m.group(1)}" target="_blank" class="block mt-2"><img src="{m.group(1)}" class="msg-embed" alt="Image embed" loading="lazy"></a>',
        text,
        flags=re.IGNORECASE,
    )

    vid_regex = r'(?<!")((https?://[^\s<"]+\.(?:mp4|webm|mov|mkv|avi))(\?[^\s<"]*)?)'
    text = re.sub(
        vid_regex,
        # ADD #t=0.001 to the src attribute below:
        lambda m: f'<br><div class="mt-2 max-w-sm rounded overflow-hidden border border-gray-200 dark:border-gray-700 bg-black"><video src="{m.group(1)}#t=0.001" controls class="w-full max-h-64 object-contain" preload="metadata"></video></div>',
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^"\s]+)\)',
        r'<a href="\2" target="_blank" class="text-gp-blue hover:underline">\1</a>',
        text,
    )
    text = re.sub(
        r'(?<!")(https?://[^\s<"]+)(?![^<]*>)',
        lambda m: f'<a href="{m.group()}" target="_blank" class="text-gp-blue hover:underline">{m.group()}</a>',
        text,
    )

    for i, code_html in enumerate(code_placeholders):
        text = text.replace(f"__CODE_BLOCK_{i}__", code_html)

    text = text.replace("\n", "<br>")
    return text


def serialize_reactions(reactions, current_user_id):
    if not reactions:
        return []
    result = []
    uid = current_user_id
    for r in reactions:
        user_ids = [str(x) for x in r.get("user_ids", [])]
        result.append(
            {"emoji": r["emoji"], "count": len(user_ids), "me": str(uid) in user_ids}
        )
    return result


# ---------------------------------------------------------------------------
# Enriched Messages (replaces get_enriched_posts)
# ---------------------------------------------------------------------------


def get_enriched_feed_messages(
    query_filter=None, search_term=None, skip=0, limit=20, sort_dir=-1
):
    """
    Query the `messages` collection for feed-type messages (posts).
    Enriches with author info, comment authors, etc.
    This replaces the old get_enriched_posts().
    """
    if query_filter is None:
        query_filter = {}

    if search_term:
        query_filter["content"] = {"$regex": re.escape(search_term), "$options": "i"}

    messages = list(
        db["messages"]
        .find(query_filter)
        .sort("created_at", sort_dir)
        .skip(skip)
        .limit(limit)
    )

    if not messages:
        return []

    user_ids = set()
    for m in messages:
        user_ids.add(m["author_id"])
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

    for m in messages:
        m["author"] = users.get(
            m["author_id"],
            {"name": "Unknown", "photo": fix_photo_path("no-icon.jpg"), "handle": ""},
        )
        for c in m.get("comments", []):
            c["author"] = users.get(
                c["user_id"],
                {
                    "name": "Unknown",
                    "photo": fix_photo_path("no-icon.jpg"),
                    "handle": "",
                },
            )

    return messages


# ---------------------------------------------------------------------------
# DM Blocking / Muting Helpers
# ---------------------------------------------------------------------------


def block_user_dm(blocker_id, blocked_id):
    if str(blocker_id) == str(blocked_id):
        return False
    try:
        db["dm_blocks"].insert_one(
            {
                "blocker_id": safe_oid(blocker_id),
                "blocked_id": safe_oid(blocked_id),
                "created_at": datetime.datetime.utcnow(),
            }
        )
        return True
    except pymongo.errors.DuplicateKeyError:
        return True


def unblock_user_dm(blocker_id, blocked_id):
    db["dm_blocks"].delete_one(
        {"blocker_id": safe_oid(blocker_id), "blocked_id": safe_oid(blocked_id)}
    )
    return True


def is_blocked(user_id, target_id):
    return (
        db["dm_blocks"].find_one(
            {"blocker_id": safe_oid(user_id), "blocked_id": safe_oid(target_id)}
        )
        is not None
    )


def mute_user_dm(muter_id, muted_id):
    if str(muter_id) == str(muted_id):
        return False
    try:
        db["dm_mutes"].insert_one(
            {
                "muter_id": safe_oid(muter_id),
                "muted_id": safe_oid(muted_id),
                "created_at": datetime.datetime.utcnow(),
            }
        )
        return True
    except pymongo.errors.DuplicateKeyError:
        return True


def unmute_user_dm(muter_id, muted_id):
    db["dm_mutes"].delete_one(
        {"muter_id": safe_oid(muter_id), "muted_id": safe_oid(muted_id)}
    )
    return True


def is_muted(muter_id, muted_id):
    return (
        db["dm_mutes"].find_one(
            {"muter_id": safe_oid(muter_id), "muted_id": safe_oid(muted_id)}
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Password Reset Logic
# ---------------------------------------------------------------------------


def send_reset_email(user_email, token):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("FROM_EMAIL", "noreply@yourdomain.com")
    if not api_key:
        print("[Email] SENDGRID_API_KEY not set.")
        return False
    reset_link = f"https://cinemint.online/mylo/reset-password/{token}"
    message = Mail(
        from_email=from_email,
        to_emails=user_email,
        subject="Reset your Mylo Password",
        html_content=f"""
            <h3>Password Reset Request</h3>
            <p>Click the link below to reset your Mylo password:</p>
            <p><a href="{reset_link}">{reset_link}</a></p>
            <p>If this was not you, please ignore this email. Link expires in 1 hour.</p>
        """,
    )
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        return response.status_code in (200, 201, 202)
    except Exception as e:
        print(f"[Email] Error sending reset email: {e}")
        return False


def create_password_reset_token(email):
    user = db["emails"].find_one({"email": email})
    if not user:
        return None
    token = str(uuid.uuid4())
    expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    db["password_resets"].insert_one(
        {"user_id": user["_id"], "token": token, "expires_at": expiry, "used": False}
    )
    return token


def verify_and_reset_password(token, new_password):
    reset_doc = db["password_resets"].find_one(
        {
            "token": token,
            "used": False,
            "expires_at": {"$gt": datetime.datetime.utcnow()},
        }
    )
    if not reset_doc:
        return False
    hashed_password = generate_password_hash(new_password)
    db["emails"].update_one(
        {"_id": reset_doc["user_id"]}, {"$set": {"password": hashed_password}}
    )
    db["password_resets"].update_one(
        {"_id": reset_doc["_id"]}, {"$set": {"used": True}}
    )
    return True


def load_current_user():
    if hasattr(g, "_current_user"):
        return g._current_user
    email = session.get("email")
    if not email:
        g._current_user = None
        return None
    user = get_user_by_email(email)
    if not user:
        session.clear()
        g._current_user = None
        return None
    g._current_user = user
    return user


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = load_current_user()
        if user is None:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, current_user=user, **kwargs)

    return decorated_function


def login_required_for_blueprint(bp):
    @bp.before_request
    def _enforce_login():
        view_fn = bp.view_functions.get(
            request.endpoint.split(".")[-1] if request.endpoint else ""
        )
        if view_fn and getattr(view_fn, "_public_endpoint", False):
            return None
        user = load_current_user()
        if user is None:
            return jsonify({"error": "Unauthorized"}), 401
        g.current_user = user

    return bp


def public_endpoint(f):
    f._public_endpoint = True
    return f


def to_isoformat(dt):
    if dt is None:
        return None
    s = dt.isoformat()
    if not s.endswith("Z") and "+" not in s:
        s += "Z"
    return s


# ---------------------------------------------------------------------------
# Role Permissions System
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS = {
    "manage_circle": "Edit circle name, icon, and general settings",
    "manage_channels": "Create, edit, delete, and reorder channels and folders",
    "manage_roles": "Create, edit, delete, and assign roles (below own rank)",
    "manage_emojis": "Upload, edit, and delete custom emojis",
    "manage_invites": "Reset the circle invite link",
    "kick_members": "Kick members from the circle",
    "ban_members": "Ban and unban members",
    "mute_members": "Mute and unmute members",
    "manage_messages": "Delete others' messages and bulk-purge",
    "send_messages": "Send messages in channels",
    "attach_files": "Upload files and images",
    "mention_everyone": "Use +everyone mentions",
}

_DEFAULT_ROLES = [
    {
        "name": "Admin",
        "color": "#4285f4",
        "position": 100,
        "is_default": False,
        "permissions": {k: True for k in ROLE_PERMISSIONS},
    },
    {
        "name": "Moderator",
        "color": "#0f9d58",
        "position": 50,
        "is_default": False,
        "permissions": {
            "kick_members": True,
            "ban_members": True,
            "mute_members": True,
            "manage_messages": True,
            "manage_emojis": True,
            "send_messages": True,
            "attach_files": True,
            "mention_everyone": True,
        },
    },
    {
        "name": "Member",
        "color": "#9e9e9e",
        "position": 0,
        "is_default": True,
        "permissions": {"send_messages": True, "attach_files": True},
    },
]


def ensure_circle_roles(circle_id):
    sid = safe_oid(circle_id)
    existing = list(db["circle_roles"].find({"circle_id": sid}).sort("position", -1))
    if existing:
        return existing
    now = datetime.datetime.utcnow()
    for tmpl in _DEFAULT_ROLES:
        db["circle_roles"].insert_one(
            {
                "circle_id": sid,
                "name": tmpl["name"],
                "color": tmpl["color"],
                "position": tmpl["position"],
                "is_default": tmpl["is_default"],
                "permissions": tmpl["permissions"],
                "created_at": now,
            }
        )
    return list(db["circle_roles"].find({"circle_id": sid}).sort("position", -1))


def get_member_roles(circle_id, user_id):
    mem = get_member(circle_id, user_id)
    if not mem:
        return []
    role_ids = mem.get("role_ids", [])
    if role_ids:
        return list(db["circle_roles"].find({"_id": {"$in": role_ids}}))
    default = db["circle_roles"].find_one(
        {"circle_id": safe_oid(circle_id), "is_default": True}
    )
    return [default] if default else []


def member_has_permission(circle_id, user_id, permission):
    circle = db["circles"].find_one({"_id": safe_oid(circle_id)})
    if circle and circle.get("owner_id") == safe_oid(user_id):
        return True
    mem = get_member(circle_id, user_id)
    if not mem:
        return False
    tags = set(mem.get("tags", []))
    if "owner" in tags:
        return True
    if "admin" in tags:
        return True
    role_ids = mem.get("role_ids", [])
    if role_ids:
        roles = db["circle_roles"].find({"_id": {"$in": role_ids}})
    else:
        roles = db["circle_roles"].find(
            {"circle_id": safe_oid(circle_id), "is_default": True}
        )
    for role in roles:
        if role.get("permissions", {}).get(permission, False):
            return True
    if "moderator" in tags and permission in (
        "kick_members",
        "ban_members",
        "mute_members",
        "manage_messages",
        "manage_emojis",
        "send_messages",
        "attach_files",
    ):
        return True
    return False


def get_ignored_user_ids(user_id):
    """Returns a list of ObjectIds that the user has blocked or muted."""
    uid = safe_oid(user_id)
    blocked = [
        b["blocked_id"]
        for b in db["dm_blocks"].find({"blocker_id": uid}, {"blocked_id": 1})
    ]
    muted = [
        m["muted_id"] for m in db["dm_mutes"].find({"muter_id": uid}, {"muted_id": 1})
    ]
    # Return a unique list of ObjectIds
    return list(set(blocked + muted))


# ---------------------------------------------------------------------------
# Centralized file upload handling
# ---------------------------------------------------------------------------

PROCESSING_RAW = "raw"  # save as-is, no processing
PROCESSING_AUTO = "auto"  # EXIF-rotate images, raw save otherwise
PROCESSING_SQUARE = "square"  # center-crop and resize to NxN
PROCESSING_WIDTH_LIMIT = "width_limit"  # cap width, preserve aspect ratio
PROCESSING_THUMBNAIL = "thumbnail"  # fit inside NxN box, preserve aspect ratio


def _save_raw(file, save_path):
    file.seek(0)
    file.save(save_path)


def _save_rotated(file, save_path):
    try:
        img = Image.open(file)
        img = ImageOps.exif_transpose(img)
        img.save(save_path)
    except Exception:
        _save_raw(file, save_path)


def _save_square(file, save_path, target_size):
    try:
        img = Image.open(file)
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        m = min(w, h)
        img = img.crop(((w - m) / 2, (h - m) / 2, (w + m) / 2, (h + m) / 2))
        img = img.resize((target_size, target_size), Image.Resampling.LANCZOS)
        img.save(save_path)
    except Exception:
        _save_raw(file, save_path)


def _save_width_limited(file, save_path, max_width):
    try:
        img = Image.open(file)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img = ImageOps.exif_transpose(img)
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize(
                (max_width, int(img.height * ratio)),
                Image.Resampling.LANCZOS,
            )
        img.save(save_path)
    except Exception:
        _save_raw(file, save_path)


def _save_thumbnail(file, save_path, max_size):
    try:
        img = Image.open(file)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        img.save(save_path)
    except Exception:
        _save_raw(file, save_path)


def process_upload(
    file,
    prefix,
    entity_id,
    processing=PROCESSING_AUTO,
    target_size=512,
    max_width=1920,
    max_thumbnail=128,
    allowed_exts=None,
):
    """
    Validate, save, and (optionally) image-process an uploaded file.

    Args:
        file: a werkzeug FileStorage from request.files
        prefix: short tag for the filename (e.g. "msg", "avatar", "emoji")
        entity_id: ObjectId or string used in the filename
        processing: one of PROCESSING_* constants
        target_size: side length for square mode
        max_width: cap for width_limit mode
        max_thumbnail: box size for thumbnail mode
        allowed_exts: optional subset of ALLOWED_EXTENSIONS to enforce

    Returns:
        dict with stored_filename, original_name, url, size, ext, is_image
        or None if the upload is missing/invalid.

    GIFs are always saved raw to preserve animation, regardless of mode.
    """
    if not file or not file.filename:
        return None
    if not allowed_file(file.filename):
        return None

    ext = get_file_extension(file.filename)
    if allowed_exts is not None and ext not in allowed_exts:
        return None

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    original_name = file.filename
    ts = int(datetime.datetime.now().timestamp())
    stored_filename = secure_filename(f"{prefix}_{entity_id}_{ts}.{ext}")
    save_path = os.path.join(UPLOAD_FOLDER, stored_filename)

    is_img = is_image_file(stored_filename)
    is_gif = ext == "gif"

    if processing == PROCESSING_RAW or is_gif or not is_img:
        _save_raw(file, save_path)
    elif processing == PROCESSING_AUTO:
        _save_rotated(file, save_path)
    elif processing == PROCESSING_SQUARE:
        _save_square(file, save_path, target_size)
    elif processing == PROCESSING_WIDTH_LIMIT:
        _save_width_limited(file, save_path, max_width)
    elif processing == PROCESSING_THUMBNAIL:
        _save_thumbnail(file, save_path, max_thumbnail)
    else:
        _save_raw(file, save_path)

    file_size = os.path.getsize(save_path)
    return {
        "stored_filename": stored_filename,
        "original_name": original_name,
        "url": f"{STATIC_WEB_URL}/uploads/{stored_filename}",
        "size": file_size,
        "ext": ext,
        "is_image": is_img,
    }


def delete_upload(file_ref):
    """
    Delete an uploaded file by filename, full URL, or stored path.
    Silently ignores missing files, external URLs, and the no-icon sentinel.
    """
    if not file_ref:
        return
    filename = os.path.basename(file_ref)
    if not filename or filename == "no-icon.jpg":
        return
    try:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
