"""
Secure credential storage using macOS Keychain via the `keyring` library.
All secrets are stored in Keychain — never written to disk as plaintext.
"""

import json
from pathlib import Path

import keyring

SERVICE = "hifi_pipeline"

KEYS = {
    "yt_client_id":     "YouTube OAuth Client ID",
    "yt_client_secret": "YouTube OAuth Client Secret",
}


def save(key: str, value: str):
    keyring.set_password(SERVICE, key, value)


def load(key: str) -> str:
    return keyring.get_password(SERVICE, key) or ""


def delete(key: str):
    try:
        keyring.delete_password(SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


def all_set() -> bool:
    return bool(load("yt_client_id") and load("yt_client_secret"))


def status() -> dict:
    return {k: bool(load(k)) for k in KEYS}


def write_client_secret(dest: Path = Path("credentials/client_secret.json")):
    """
    Reconstruct client_secret.json from Keychain values so the Google OAuth
    library can use it. File is written at runtime only, never committed.
    """
    client_id     = load("yt_client_id")
    client_secret = load("yt_client_secret")

    if not client_id or not client_secret:
        raise ValueError("YouTube credentials not found in Keychain. Add them in Settings.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    dest.write_text(json.dumps(payload, indent=2))
