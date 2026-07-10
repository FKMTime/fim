"""OpenWrt authentication via rpcd session (same as LuCI)."""
import json
import re
import subprocess

try:
    import crypt
except ImportError:
    crypt = None

from fim.commands import run_cmd
from fim.config import IS_ROOT

SHADOW_FILE = "/etc/shadow"
_RPCD_SECTION_RE = re.compile(r"^rpcd\.(@login\[\d+\]|[^=\s]+)=(\w+)")
_RPCD_OPTION_RE = re.compile(r"^rpcd\.([^.]+)\.(\w+)=(.*)$")


def parse_shadow_hash(shadow_output, username):
    for line in shadow_output.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if parts[0] == username:
            return parts[1] if len(parts) > 1 else ""
    return None


def parse_rpcd_logins(uci_show_output):
    """Return {section_id: {option: value}} for rpcd login blocks."""
    logins = {}
    current_section = None
    for line in uci_show_output.splitlines():
        section_match = _RPCD_SECTION_RE.match(line.strip())
        if section_match:
            if section_match.group(2) == "login":
                current_section = section_match.group(1)
                logins[current_section] = {}
            else:
                current_section = None
            continue
        if not current_section:
            continue
        option_match = _RPCD_OPTION_RE.match(line.strip())
        if option_match and option_match.group(1) == current_section:
            value = option_match.group(3).strip()
            if (value.startswith("'") and value.endswith("'")) or (
                value.startswith('"') and value.endswith('"')
            ):
                value = value[1:-1]
            logins[current_section][option_match.group(2)] = value
    return logins


def list_rpcd_usernames(uci_show_output):
    users = []
    for opts in parse_rpcd_logins(uci_show_output).values():
        username = opts.get("username")
        if username and username not in users:
            users.append(username)
    return users


def choose_default_login_username(users):
    if "root" in users:
        return "root"
    return users[0] if users else "root"


def get_default_login_username():
    code, out = run_cmd(["uci", "show", "rpcd"], timeout=5)
    if code != 0:
        return "root"
    return choose_default_login_username(list_rpcd_usernames(out))


def find_rpcd_login(uci_show_output, username):
    for section, opts in parse_rpcd_logins(uci_show_output).items():
        if opts.get("username") == username:
            return section, opts
    return None, None


def verify_linux_password(password, shadow_hash):
    if not shadow_hash or shadow_hash in ("", "x", "*", "!"):
        return False
    if crypt is None:
        return False
    return crypt.crypt(password, shadow_hash) == shadow_hash


def verify_rpcd_password(stored_hash, password):
    if not stored_hash:
        return False
    if stored_hash.startswith("$p$"):
        shadow_user = stored_hash[3:]
        shadow_hash = read_shadow_hash(shadow_user)
        return verify_linux_password(password, shadow_hash)
    if crypt is None:
        return False
    return crypt.crypt(password, stored_hash) == stored_hash


def read_shadow_hash(username):
    try:
        with open(SHADOW_FILE, encoding="utf-8") as f:
            return parse_shadow_hash(f.read(), username)
    except OSError:
        return None


def verify_openwrt_credentials_ubus(username, password):
    payload = json.dumps({"username": username, "password": password})
    code, out = run_cmd(["ubus", "call", "session", "login", payload], timeout=15)
    if code != 0:
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    return bool(data.get("ubus_rpc_session"))


def verify_openwrt_credentials_fallback(username, password):
    code, out = run_cmd(["uci", "show", "rpcd"], timeout=5)
    if code == 0:
        _, opts = find_rpcd_login(out, username)
        if opts and opts.get("password"):
            return verify_rpcd_password(opts["password"], password)
    if IS_ROOT:
        shadow_hash = read_shadow_hash(username)
        if shadow_hash is not None:
            return verify_linux_password(password, shadow_hash)
    return False


def verify_openwrt_credentials(username, password):
    if not username or not isinstance(username, str):
        return False
    if not isinstance(password, str):
        return False
    if verify_openwrt_credentials_ubus(username, password):
        return True
    return verify_openwrt_credentials_fallback(username, password)


def generate_rpcd_password_hash(password):
    code, out = run_cmd(["uhttpd", "-m", password], timeout=10)
    if code == 0:
        candidate = out.strip().splitlines()[-1].strip()
        if candidate.startswith("$"):
            return candidate
    if crypt is not None:
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    return None


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


def change_openwrt_password(username, current_password, new_password):
    if not verify_openwrt_credentials(username, current_password):
        return False, "Current password is incorrect"

    code, out = run_cmd(["uci", "show", "rpcd"], timeout=5)
    section, opts = (None, None)
    if code == 0:
        section, opts = find_rpcd_login(out, username)

    if opts:
        stored_hash = opts.get("password", "")
        if stored_hash.startswith("$p$"):
            return set_linux_password(stored_hash[3:], new_password)
        if not IS_ROOT:
            return False, "Must run as root to change this account password"
        new_hash = generate_rpcd_password_hash(new_password)
        if not new_hash:
            return False, "Could not generate password hash"
        set_code, set_out = run_cmd(
            ["uci", "set", f"rpcd.{section}.password={new_hash}"],
            timeout=10,
        )
        if set_code != 0:
            return False, (set_out or "uci set failed").strip()
        commit_code, commit_out = run_cmd(["uci", "commit", "rpcd"], timeout=10)
        if commit_code != 0:
            return False, (commit_out or "uci commit failed").strip()
        run_cmd(["/etc/init.d/rpcd", "restart"], timeout=15)
        return True, ""

    return set_linux_password(username, new_password)
