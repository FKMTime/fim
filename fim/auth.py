"""Session authentication."""
import hashlib
import json
import os
import secrets
import time

from fim.config import AUTH_FILE, IS_OPENWRT

# ── Auth ────────────────────────────────────────────────────────────────────

sessions = {}          # token -> expiry epoch
SESSION_TTL = 86400 * 7 # 7 days


def load_auth():
    if IS_OPENWRT:
        print("OpenWrt: using system root password (/etc/shadow)")
        return {"username": "root", "source": "openwrt"}
    if not os.path.exists(AUTH_FILE):
        h = hashlib.sha256(b"root").hexdigest()
        with open(AUTH_FILE, "w") as f:
            json.dump({"username": "root", "password_hash": h}, f, indent=2)
        print(f"Created {AUTH_FILE} with defaults root/root")
    with open(AUTH_FILE) as f:
        return json.load(f)


def get_username():
    if IS_OPENWRT:
        return "root"
    return load_auth().get("username", "root")


def get_account_info():
    return {
        "ok": True,
        "username": get_username(),
        "auth_source": "openwrt" if IS_OPENWRT else "local",
        "uses_system_password": IS_OPENWRT,
    }


def validate_new_password(password):
    if not isinstance(password, str):
        raise ValueError("Invalid password")
    if not password:
        raise ValueError("Password cannot be empty")
    if len(password) > 128:
        raise ValueError("Password too long")
    return password


def check_credentials(username, password):
    if IS_OPENWRT:
        from fim.openwrt_auth import verify_openwrt_credentials
        return verify_openwrt_credentials(username, password)
    auth = load_auth()
    h = hashlib.sha256(password.encode()).hexdigest()
    return auth.get("username") == username and auth.get("password_hash") == h


def change_password(username, current_password, new_password):
    try:
        new_password = validate_new_password(new_password)
    except ValueError as e:
        return False, str(e)

    if IS_OPENWRT:
        from fim.openwrt_auth import OPENWRT_USERNAME, set_linux_password, verify_openwrt_credentials
        if username != OPENWRT_USERNAME:
            return False, "Only the root account is supported on OpenWrt"
        if not verify_openwrt_credentials(username, current_password):
            return False, "Current password is incorrect"
        return set_linux_password(username, new_password)

    if not check_credentials(username, current_password):
        return False, "Current password is incorrect"
    auth = load_auth()
    if auth.get("username") != username:
        return False, "Invalid user"
    auth["password_hash"] = hashlib.sha256(new_password.encode()).hexdigest()
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)
    return True, ""

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
