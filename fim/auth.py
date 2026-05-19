"""Session authentication."""
import hashlib
import json
import os
import secrets
import time
from fim.config import AUTH_FILE

# ── Auth ────────────────────────────────────────────────────────────────────

sessions = {}          # token -> expiry epoch
SESSION_TTL = 86400 * 7 # 7 days

def load_auth():
    if not os.path.exists(AUTH_FILE):
        h = hashlib.sha256(b"root").hexdigest()
        with open(AUTH_FILE, "w") as f:
            json.dump({"username": "root", "password_hash": h}, f, indent=2)
        print(f"Created {AUTH_FILE} with defaults root/root")
    with open(AUTH_FILE) as f:
        return json.load(f)

def check_credentials(username, password):
    auth = load_auth()
    h = hashlib.sha256(password.encode()).hexdigest()
    return auth.get("username") == username and auth.get("password_hash") == h

def create_session():
    token = secrets.token_hex(32)
    sessions[token] = time.time() + SESSION_TTL
    return token

def validate_session(token):
    if not token:
        return False
    exp = sessions.get(token)
    if not exp:
        return False
    if time.time() > exp:
        del sessions[token]
        return False
    return True

def get_cookie(headers, name):
    for part in headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name)+1:]
    return None
