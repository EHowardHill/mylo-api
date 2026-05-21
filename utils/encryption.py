# encryption.py
#
# Server-side encryption at rest for Mylo messages, DMs, and posts.
#
# Setup:
#   1. Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   2. Export it:        export MYLO_ENCRYPTION_KEY="<the key>"
#   3. Restart the app.
#
# If MYLO_ENCRYPTION_KEY is not set, encryption is silently disabled
# and messages are stored as plaintext (same as before).
#
# Every new document gets an `encrypted: True` flag.  Old documents
# without that flag (or with `encrypted: False`) are returned as-is,
# so nothing breaks for historical data.

import os
from cryptography.fernet import Fernet, InvalidToken

_KEY = os.environ.get("MYLO_ENCRYPTION_KEY")
_fernet = None


def _get_fernet():
    """Lazy-init a Fernet instance from the env key."""
    global _fernet
    if _fernet is not None:
        return _fernet
    if not _KEY:
        return None
    try:
        _fernet = Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)
    except Exception as e:
        print(f"[Encryption] Invalid MYLO_ENCRYPTION_KEY: {e}")
        _fernet = None
    return _fernet


def encrypt_text(plaintext):
    """
    Encrypt a string.  Returns the ciphertext string.
    If encryption is not configured, returns the original plaintext.
    """
    if not plaintext:
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_text(ciphertext):
    """
    Decrypt a string.  If it fails (wrong key, not actually encrypted,
    corrupted), returns the original value so nothing crashes.
    """
    if not ciphertext:
        return ciphertext
    f = _get_fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        # Not encrypted, wrong key, or corrupted — return as-is
        return ciphertext


def decrypt_if_encrypted(text, encrypted_flag):
    """
    Convenience wrapper: only attempt decryption when the document
    was stored with `encrypted: True`.  Old unencrypted documents
    pass straight through.
    """
    if not encrypted_flag:
        return text
    return decrypt_text(text)


def is_encryption_enabled():
    """Return True if an encryption key is configured."""
    return _get_fernet() is not None
