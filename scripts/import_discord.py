import os
import json
import glob
import requests
import re
import secrets
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash
from pymongo import ReturnDocument

# Import Mylo's existing architecture
from shared import db, UPLOAD_FOLDER, generate_invite_code
from utils.encryption import encrypt_text

EXPORT_DIR = "/var/www/Mylo/discord_exports"  # Change this if your JSONs are elsewhere

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"}


def parse_discord_time(ts_str):
    """Convert Discord ISO time to naive UTC datetime for Mylo."""
    if not ts_str:
        return datetime.utcnow()
    dt = datetime.fromisoformat(ts_str)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def download_file(url, prefix="import", original_filename=""):
    """Download Discord assets/attachments and save them to Mylo's UPLOAD_FOLDER."""
    if not url:
        return None, None
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            ext = (
                original_filename.rsplit(".", 1)[-1].lower()
                if "." in original_filename
                else url.split("?")[0].split(".")[-1]
            )
            if not ext or len(ext) > 5:
                ext = "bin"

            filename = f"{prefix}_{secrets.token_hex(6)}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)

            with open(filepath, "wb") as f:
                f.write(resp.content)

            return filename, len(resp.content)
    except Exception as e:
        print(f"Failed to download {url}: {e}")
    return None, None


def get_or_create_user(discord_user):
    """Map a Discord user to a Mylo user using discord_id to prevent duplicates."""
    d_id = discord_user["id"]
    email = f"{d_id}@discord.import"

    fullname = discord_user.get("nickname") or discord_user.get("name")

    # Check if we need to generate a handle
    existing = db["emails"].find_one({"discord_id": d_id})
    if existing:
        handle = existing["user_handle"]
        # Only download a new avatar if we want to overwrite, skipping to save time/space
        # but you can change this to redownload if needed.
        avatar_filename = existing.get("photo_url", "no-icon.jpg")
    else:
        base_handle = (
            re.sub(r"[^a-zA-Z0-9_]", "", discord_user["name"]).lower() or f"user_{d_id}"
        )
        handle = base_handle
        while db["emails"].find_one({"user_handle": handle}):
            handle = f"{base_handle}_{secrets.randbelow(1000)}"

        avatar_filename, _ = download_file(discord_user.get("avatarUrl"), "avatar")
        if not avatar_filename:
            avatar_filename = "no-icon.jpg"

    # Upsert the user
    user_doc = db["emails"].find_one_and_update(
        {"discord_id": d_id},
        {
            "$set": {
                "user_full_name": fullname,
                "photo_url": avatar_filename,
            },
            "$setOnInsert": {
                "email": email,
                "password": generate_password_hash(secrets.token_urlsafe(16)),
                "user_handle": handle,
                "banner_url": "",
                "about_me": "Archived from Discord",
                "status": "offline",
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return user_doc["_id"]


def ensure_server_member(server_id, user_id):
    """Ensure a user is added to the Mylo server_members collection idempotently."""
    db["server_members"].update_one(
        {"server_id": server_id, "user_id": user_id},
        {"$setOnInsert": {"tags": ["member"], "joined_at": datetime.utcnow()}},
        upsert=True,
    )


def run_import():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # Locate cinemint
    admin_user = db["emails"].find_one({"user_handle": "cinemint"})
    if not admin_user:
        print(
            "Error: The admin user 'cinemint' was not found. Please create this account first."
        )
        return
    admin_id = admin_user["_id"]

    json_files = glob.glob(os.path.join(EXPORT_DIR, "*.json"))
    if not json_files:
        print(f"No JSON files found in {EXPORT_DIR}")
        return

    print(f"Found {len(json_files)} files. Starting idempotent import...")

    for filepath in json_files:
        print(f"Processing {os.path.basename(filepath)}...")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        d_guild = data.get("guild", {})
        d_chan = data.get("channel", {})

        guild_id = d_guild.get("id")
        chan_id = d_chan.get("id")

        if not guild_id or not chan_id:
            print("  Skipped: Missing guild or channel ID in JSON.")
            continue

        # --- DB LOOKUP FIRST ---
        existing_channel = db["channels"].find_one({"discord_id": chan_id})

        if existing_channel:
            # The channel and server already exist. Skip metadata parsing.
            c_id = existing_channel["_id"]
            s_id = existing_channel["server_id"]
            print(f"  Channel {chan_id} found in database. Skipping metadata creation.")
        else:
            # The channel is new. We need full metadata to create the structure.
            guild_name = d_guild.get("name")
            chan_name = d_chan.get("name")

            if not guild_name or not chan_name:
                print(
                    "  Skipped: Channel is not in DB, and partial JSON lacks names to create it."
                )
                continue

            # --- SERVER UPSERT ---
            server_doc = db["servers"].find_one({"discord_id": guild_id})
            icon_filename = server_doc.get("icon_url") if server_doc else None

            if not icon_filename:
                icon_filename, _ = download_file(d_guild.get("iconUrl"), "server")

            s_doc = db["servers"].find_one_and_update(
                {"discord_id": guild_id},
                {
                    "$set": {
                        "name": guild_name,
                        "icon_url": icon_filename or "",
                        "owner_id": admin_id,
                    },
                    "$setOnInsert": {
                        "invite_code": generate_invite_code(),
                        "created_at": datetime.utcnow(),
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            s_id = s_doc["_id"]

            # Ensure cinemint is the owner
            db["server_members"].update_one(
                {"server_id": s_id, "user_id": admin_id},
                {
                    "$set": {"tags": ["owner", "admin"]},
                    "$setOnInsert": {"joined_at": datetime.utcnow()},
                },
                upsert=True,
            )

            # --- FOLDER UPSERT ---
            cat_name = d_chan.get("category") or "Imported Channels"
            f_doc = db["channel_folders"].find_one_and_update(
                {"server_id": s_id, "name": cat_name},
                {
                    "$setOnInsert": {
                        "position": db["channel_folders"].count_documents(
                            {"server_id": s_id}
                        )
                    }
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            f_id = f_doc["_id"]

            # --- CHANNEL UPSERT ---
            c_name = re.sub(r"[^a-z0-9\-]", "", chan_name.lower().replace(" ", "-"))
            c_doc = db["channels"].find_one_and_update(
                {"discord_id": chan_id},
                {
                    "$set": {
                        "server_id": s_id,
                        "name": c_name,
                        "description": d_chan.get("topic") or "",
                        "folder_id": f_id,
                    },
                    "$setOnInsert": {
                        "position": db["channels"].count_documents({"server_id": s_id}),
                        "permission_tags": [],
                        "slowmode_seconds": 0,
                        "created_at": datetime.utcnow(),
                        "last_message_at": datetime.utcnow(),
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            c_id = c_doc["_id"]

        # --- MESSAGE UPSERTS ---
        upsert_count = 0
        for msg in data.get("messages", []):
            msg_id = msg["id"]
            u_id = get_or_create_user(msg["author"])
            ensure_server_member(s_id, u_id)

            msg_time = parse_discord_time(msg["timestamp"])
            content = msg.get("content", "")
            encrypted_content = encrypt_text(content)

            # Map Reactions
            mylo_reactions = []
            for r in msg.get("reactions", []):
                react_user_ids = []
                for ru in r.get("users", []):
                    ru_id = get_or_create_user(ru)
                    ensure_server_member(s_id, ru_id)
                    react_user_ids.append(ru_id)

                mylo_reactions.append(
                    {"emoji": r["emoji"].get("name", ""), "user_ids": react_user_ids}
                )

            # --- Handle Attachments ---
            file_url, file_name, file_size, image_url = None, None, None, None
            attachments = msg.get("attachments", [])

            if attachments:
                att = attachments[0]
                existing_msg = db["messages"].find_one({"discord_id": msg_id})

                if existing_msg and existing_msg.get("file_url"):
                    file_url = existing_msg["file_url"]
                    file_name = existing_msg["file_name"]
                    file_size = existing_msg["file_size"]
                    image_url = existing_msg.get("image_url")
                else:
                    dl_filename, dl_size = download_file(
                        att.get("url"), "msg", att.get("fileName", "file")
                    )
                    if dl_filename:
                        file_url = dl_filename
                        file_name = att.get("fileName", "file")
                        file_size = dl_size

                        ext = (
                            file_name.rsplit(".", 1)[-1].lower()
                            if "." in file_name
                            else ""
                        )
                        if ext in IMAGE_EXTENSIONS:
                            image_url = dl_filename

            db["messages"].update_one(
                {"discord_id": msg_id},
                {
                    "$set": {
                        "channel_id": c_id,
                        "author_id": u_id,
                        "content": encrypted_content,
                        "encrypted": True,
                        "image_url": image_url,
                        "file_url": file_url,
                        "file_name": file_name,
                        "file_size": file_size,
                        "created_at": msg_time,
                        "edited": msg.get("timestampEdited") is not None,
                        "reactions": mylo_reactions,
                    }
                },
                upsert=True,
            )
            upsert_count += 1

        print(f"  Processed {upsert_count} messages.")

    print(
        "\nImport Complete! All servers, channels, messages, and attachments have been migrated safely."
    )


if __name__ == "__main__":
    run_import()
