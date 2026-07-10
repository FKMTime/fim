"""OpenWrt system password helpers (/etc/shadow)."""
import subprocess

try:
    import crypt
except ImportError:
    crypt = None

from fim.config import IS_ROOT

SHADOW_FILE = "/etc/shadow"
OPENWRT_USERNAME = "root"


def parse_shadow_hash(shadow_output, username=OPENWRT_USERNAME):
    for line in shadow_output.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if parts[0] == username:
            return parts[1] if len(parts) > 1 else ""
    return None


def verify_linux_password(password, shadow_hash):
    if not shadow_hash or shadow_hash in ("", "x", "*", "!"):
        return False
    if crypt is None:
        return False
    return crypt.crypt(password, shadow_hash) == shadow_hash


def read_shadow_hash(username=OPENWRT_USERNAME):
    try:
        with open(SHADOW_FILE, encoding="utf-8") as f:
            return parse_shadow_hash(f.read(), username)
    except OSError:
        return None


def verify_openwrt_credentials(username, password):
    if username != OPENWRT_USERNAME:
        return False
    if not IS_ROOT:
        return False
    shadow_hash = read_shadow_hash(username)
    if shadow_hash is None:
        return False
    return verify_linux_password(password, shadow_hash)


def set_linux_password(username, new_password):
    if not IS_ROOT:
        return False, "Must run as root to change the system password"
    try:
        proc = subprocess.run(
            ["passwd", username],
            input=f"{new_password}\n{new_password}\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "passwd command not found"
    except subprocess.TimeoutExpired:
        return False, "passwd timed out"
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "passwd failed").strip()
        return False, msg or "passwd failed"
    return True, ""
