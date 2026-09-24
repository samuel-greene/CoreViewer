#!/usr/bin/env python3
"""
make_user.py — Interactive helper for generating a pre-hashed user entry
               suitable for pasting into the USERS_JSON environment variable.

Usage:
    python3 scripts/make_user.py

Output (copy the JSON object into your USERS_JSON array):
    {"email": "...", "hash": "...", "salt": "...", "role": "...", "allowed_paths": ["*"]}
"""

import hashlib
import json
import os
import sys


def hash_password(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return key.hex(), salt.hex()


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def main() -> None:
    print("\n=== Coreview user generator ===\n")

    email = prompt("Email")
    if not email or "@" not in email:
        print("ERROR: a valid email address is required.", file=sys.stderr)
        sys.exit(1)

    import getpass
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        print("ERROR: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    role = prompt("Role", "viewer")
    paths_raw = prompt("Allowed paths (comma-separated, * = all)", "*")
    allowed_paths = [p.strip() for p in paths_raw.split(",") if p.strip()]

    pw_hash, pw_salt = hash_password(password)

    user = {
        "email": email,
        "hash": pw_hash,
        "salt": pw_salt,
        "role": role,
        "allowed_paths": allowed_paths,
    }

    print("\n--- Copy this JSON object into your USERS_JSON array ---\n")
    print(json.dumps(user, indent=2))
    print("\n--- One-liner (for env var paste) ---\n")
    print(json.dumps(user))
    print()


if __name__ == "__main__":
    main()
