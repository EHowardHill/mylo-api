# composite.py — Generate composite profile icons and default names for circles

import os
import datetime
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from utils.shared_api import db, UPLOAD_FOLDER, STATIC_WEB_URL, safe_oid


# ---------------------------------------------------------------------------
# Avatar loading
# ---------------------------------------------------------------------------


def _load_avatar(photo_url):
    """Load a user's avatar as a PIL Image, with fallback to a grey square."""
    path = None
    if photo_url and photo_url != "no-icon.jpg":
        path = os.path.join(UPLOAD_FOLDER, os.path.basename(photo_url))
    else:
        path = os.path.join(UPLOAD_FOLDER, "no-icon.jpg")

    try:
        img = Image.open(path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        return img
    except Exception:
        return Image.new("RGB", (512, 512), (189, 189, 189))


def _crop_to_fill(img, target_w, target_h):
    """
    Resize-and-centre-crop so the image *fills* the target rectangle
    without any squashing or letterboxing.

    The image is scaled so its shorter dimension matches the target,
    then the overflowing dimension is centre-cropped away.
    """
    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # Source is wider than target → match heights, crop sides
        new_h = target_h
        new_w = int(src_w * (target_h / src_h))
    else:
        # Source is taller (or equal) → match widths, crop top/bottom
        new_w = target_w
        new_h = int(src_h * (target_w / src_w))

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


# ---------------------------------------------------------------------------
# Composite icon generation
# ---------------------------------------------------------------------------


def generate_composite_icon(user_ids, circle_id, gap=6):
    """
    Generate a composite icon from up to 4 user avatars and save it.

    Layout:
        1 user  → full 512×512
        2 users → left half | right half  (each panel ~253×512, crop-to-fill)
        3 users → left half | top-right / bottom-right
        4 users → 2×2 grid

    Every panel is filled via centre-crop — aspect ratio is always preserved.

    Returns:
        The stored filename (basename only), or "" on failure.
    """
    if not user_ids:
        return ""

    oids = [safe_oid(uid) for uid in user_ids[:4]]
    oids = [o for o in oids if o is not None]
    if not oids:
        return ""

    users = list(db["emails"].find({"_id": {"$in": oids}}))
    if not users:
        return ""

    # Preserve the caller's order
    user_map = {u["_id"]: u for u in users}
    ordered_users = [user_map[o] for o in oids if o in user_map]
    if not ordered_users:
        return ""

    size = 512
    half = size // 2
    g = gap // 2  # half-gap on each touching edge
    bg_color = (224, 224, 224)

    canvas = Image.new("RGB", (size, size), bg_color)

    avatars = [_load_avatar(u.get("photo_url", "no-icon.jpg")) for u in ordered_users]
    n = len(avatars)

    if n == 1:
        canvas.paste(_crop_to_fill(avatars[0], size, size), (0, 0))

    elif n == 2:
        pw = half - g  # panel width
        ph = size  # panel height
        canvas.paste(_crop_to_fill(avatars[0], pw, ph), (0, 0))
        canvas.paste(_crop_to_fill(avatars[1], pw, ph), (half + g, 0))

    elif n == 3:
        lw, lh = half - g, size  # left panel (tall)
        rw, rh = half - g, half - g  # right panels (square-ish)
        canvas.paste(_crop_to_fill(avatars[0], lw, lh), (0, 0))
        canvas.paste(_crop_to_fill(avatars[1], rw, rh), (half + g, 0))
        canvas.paste(_crop_to_fill(avatars[2], rw, rh), (half + g, half + g))

    elif n >= 4:
        cw, ch = half - g, half - g  # each cell
        canvas.paste(_crop_to_fill(avatars[0], cw, ch), (0, 0))
        canvas.paste(_crop_to_fill(avatars[1], cw, ch), (half + g, 0))
        canvas.paste(_crop_to_fill(avatars[2], cw, ch), (0, half + g))
        canvas.paste(_crop_to_fill(avatars[3], cw, ch), (half + g, half + g))

    # Save
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    ts = int(datetime.datetime.now().timestamp())
    filename = secure_filename(f"composite_{circle_id}_{ts}.jpg")
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    canvas.save(save_path, "JPEG", quality=90)

    return filename


# ---------------------------------------------------------------------------
# Default name generation
# ---------------------------------------------------------------------------


def generate_default_name(user_ids, exclude_user_id=None):
    """
    Build a comma-separated name string from up to 4 members.
    Optionally excludes one user (e.g. the viewer for 1-on-1 DMs).

    Returns "" if no users are found.
    """
    oids = []
    for uid in user_ids:
        if exclude_user_id and str(uid) == str(exclude_user_id):
            continue
        o = safe_oid(uid)
        if o:
            oids.append(o)

    if not oids:
        return ""

    users = list(
        db["emails"].find(
            {"_id": {"$in": oids[:4]}},
            {"user_full_name": 1},
        )
    )

    user_map = {u["_id"]: u.get("user_full_name", "Unknown") for u in users}
    names = [user_map[o] for o in oids[:4] if o in user_map]

    remaining = len(oids) - 4
    if remaining > 0:
        return ", ".join(names) + f" +{remaining}"
    return ", ".join(names)
