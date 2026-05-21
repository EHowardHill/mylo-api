#!/usr/bin/env python3
"""Generate a Fernet encryption key for Mylo message encryption."""
from cryptography.fernet import Fernet

key = Fernet.generate_key().decode()
print(f"\nGenerated encryption key:\n")
print(f"  {key}\n")
print(f"Add to your environment:\n")
print(f'  export MYLO_ENCRYPTION_KEY="{key}"\n')
print(f"Then restart the app.\n")
