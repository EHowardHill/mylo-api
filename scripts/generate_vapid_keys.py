#!/usr/bin/env python3
"""
Generate VAPID key pair for Web Push notifications.

Usage:
    python generate_vapid_keys.py

Set the output as environment variables:
    MYLO_VAPID_PRIVATE_KEY
    MYLO_VAPID_PUBLIC_KEY
    MYLO_VAPID_CLAIMS_EMAIL  (e.g. mailto:admin@example.com)
"""

import os
import sys


def ensure_deps():
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        os.system(f"{sys.executable} -m pip install cryptography --break-system-packages")


def generate_keys():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import base64

    # Generate an EC key on the P-256 curve (required by Web Push)
    private_key = ec.generate_private_key(ec.SECP256R1())

    # Private key: raw 32-byte integer, base64url-encoded (no padding)
    raw_private = private_key.private_numbers().private_value.to_bytes(32, byteorder="big")
    private_b64 = base64.urlsafe_b64encode(raw_private).decode("ascii").rstrip("=")

    # Public key: uncompressed point format (65 bytes), base64url-encoded (no padding)
    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(raw_public).decode("ascii").rstrip("=")

    return private_b64, public_b64


def main():
    ensure_deps()
    priv, pub = generate_keys()

    # Verify the public key is 65 bytes (uncompressed P-256 point: 0x04 + 32 + 32)
    import base64
    raw = base64.urlsafe_b64decode(pub + "==")
    assert len(raw) == 65 and raw[0] == 0x04, "Public key format error"

    print()
    print("=" * 64)
    print("  VAPID Keys Generated Successfully")
    print("=" * 64)
    print()
    print("  Add these to your environment (shell profile, systemd, .env):")
    print()
    print(f'  export MYLO_VAPID_PUBLIC_KEY="{pub}"')
    print(f'  export MYLO_VAPID_PRIVATE_KEY="{priv}"')
    print(f'  export MYLO_VAPID_CLAIMS_EMAIL="mailto:ethanlikestowritestuff@gmail.com"')
    print()
    print(f"  Public key length: {len(pub)} chars ({len(raw)} bytes decoded)")
    print()

    try:
        write = input("  Write to .env file in project root? (y/N): ").strip().lower()
    except EOFError:
        write = "n"

    if write == "y":
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".env")

        existing_lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                existing_lines = [
                    line for line in f.readlines()
                    if not line.strip().startswith("MYLO_VAPID_")
                ]

        with open(env_path, "w") as f:
            f.writelines(existing_lines)
            if existing_lines and not existing_lines[-1].endswith("\n"):
                f.write("\n")
            f.write(f"MYLO_VAPID_PUBLIC_KEY={pub}\n")
            f.write(f"MYLO_VAPID_PRIVATE_KEY={priv}\n")
            f.write(f"MYLO_VAPID_CLAIMS_EMAIL=mailto:ethanlikestowritestuff@gmail.com\n")

        print(f"  Written to {env_path}")

    print()


if __name__ == "__main__":
    main()
