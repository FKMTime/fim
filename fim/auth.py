"""Session authentication."""
import hashlib
import json
import os
import secrets
import time

from fim.config import AUTH_FILE, IS_OPENWRT

# ── Auth ────────────────────────────────────────────────────────────────────

sessions = {}          # token -> {"exp": epoch, "username": str}
SESSION_TTL = 86400 * 7 # 7 days


def load_auth():
    if IS_OPENWRT:
        print("OpenWrt: using LuCI/rpcd credentials (ubus session login)")
        return {"source": "openwrt"}
    if not os.path.exists(AUTH_FILE):
        h = hashlib.sha256(b"root").hexdigest()
        with open(AUTH_FILE, "w") as f:
            json.dump({"username": "root", "password_hash": h}, f, indent=2)
        print(f"Created {AUTH_FILE} with defaults root/root")
    with open(AUTH_FILE) as f:
        return json.load(f)


def _session_record(token):
    record = sessions.get(token)
    if isinstance(record, dict):
        return record
    if isinstance(record, (int, float)):
        return {"exp": record, "username": "root"}
    return None


def get_session_username(token):
    record = _session_record(token)
    if not record:
        if IS_OPENWRT:
            return "root"
        return load_auth().get("username", "root")
    return record.get("username") or "root"


def get_account_info(token=None):
    username = get_session_username(token) if token else (
        "root" if IS_OPENWRT else load_auth().get("username", "root")
    )
    return {
        "ok": True,
        "username": username,
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
        from fim.openwrt_auth import change_openwrt_password
        return change_openwrt_password(username, current_password, new_password)

    if not check_credentials(username, current_password):
        return False, "Current password is incorrect"
    auth = load_auth()
    if auth.get("username") != username:
        return False, "Invalid user"
    auth["password_hash"] = hashlib.sha256(new_password.encode()).hexdigest()
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)
    return True, ""


def create_session(username="root"):
    token = secrets.token_hex(32)
    sessions[token] = {
        "exp": time.time() + SESSION_TTL,
        "username": username,
    }
    return token


def validate_session(token):
    if not token:
        return False
    record = _session_record(token)
    if not record:
        return False
    exp = record.get("exp")
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
