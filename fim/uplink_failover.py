"""Fail the Wi-Fi uplink over to a backup network when the primary one is gone.

OpenWrt points its client (STA) interface, default_radio0, at one network and
has no fallback of its own. fim keeps an optional backup network in
DATA_DIR/wifi_uplink.json and runs a watchdog on OpenWrt:

- When the uplink has had no IPv4 address for FAILOVER_AFTER_SECS, it rewrites
  default_radio0 to the other network (primary -> backup, or back) and reloads
  Wi-Fi. netifd restarts only the radio whose config changed, so the hotspot
  radios stay up.
- While on the backup it scans for the primary SSID and moves back when the
  primary is in range. Each failed return doubles the wait before the next
  scan, so a primary that is visible but broken (say, a changed password)
  does not cut the uplink every few minutes.

Which network is active lives in the same file. While the primary is active,
uci is the truth for it (edits made in LuCI count); it is copied into the file
the moment the watchdog leaves it, so it can be restored later.
"""
import json
import os
import re
import shutil
import socket
import threading
import time

from fim.commands import run_cmd
from fim.config import DATA_DIR, IS_OPENWRT
from fim.openwrt_wifi import get_sta_network_name, uplink_network_candidates
from fim.progress import action_lock
from fim.wifi_stats import IW_SEARCH_DIRS

STA_SECTION = "default_radio0"
UPLINK_FILE = os.path.join(DATA_DIR, "wifi_uplink.json")

CHECK_INTERVAL_SECS = 15
# Covers association plus DHCP after a reload, and short blips of the network.
FAILOVER_AFTER_SECS = 60
RETURN_CHECK_SECS = 300
MAX_RETURN_CHECK_SECS = 3600
# The primary must keep its address this long before a return counts as good.
RETURN_STABLE_SECS = 120
CMD_TIMEOUT_SECS = 10
SCAN_TIMEOUT_SECS = 20
# An uplink counts as working only when one of these accepts a TCP connection
# through it. TCP rather than ping: venue networks block ICMP more often than
# HTTPS, and a captive portal lets neither through.
INTERNET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
PROBE_TIMEOUT_SECS = 3
# Linux value; the socket module only names it on Linux.
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)

PRIMARY = "primary"
BACKUP = "backup"

_file_lock = threading.Lock()


# ── Stored state ────────────────────────────────────────────────────────────

def load_uplink_state(path=UPLINK_FILE):
    """{"active": "primary"|"backup", "backup": {...}|None, "primary": {...}|None}."""
    state = {"active": PRIMARY, "backup": None, "primary": None}
    try:
        with open(path) as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return state
    if isinstance(stored, dict):
        if stored.get("active") == BACKUP:
            state["active"] = BACKUP
        for name in (BACKUP, PRIMARY):
            net = stored.get(name)
            if isinstance(net, dict) and net.get("ssid"):
                state[name] = {
                    "ssid": str(net["ssid"]),
                    "key": str(net.get("key") or ""),
                    "encryption": str(net.get("encryption") or ""),
                }
    return state


def save_uplink_state(state, path=UPLINK_FILE):
    """Write atomically, readable by root only: the file holds Wi-Fi passwords."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ── uci ─────────────────────────────────────────────────────────────────────

_sae_supported = None


def _find_sbin(name):
    """Locate a tool, also in the sbin directories a service PATH may lack."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    dirs += [d for d in IW_SEARCH_DIRS if d not in dirs]
    return shutil.which(name, path=os.pathsep.join(d for d in dirs if d))


def phy_info_supports_sae(iw_phy_info):
    """Whether `iw phy <phy> info` shows a driver that can do SAE as a client.

    mac80211 drivers run SAE in wpa_supplicant through the AUTHENTICATE
    command; full-MAC drivers (brcmfmac) need SAE offload in their firmware.
    The Raspberry Pi's built-in brcmfmac has neither.
    """
    return ("SAE with AUTHENTICATE command" in iw_phy_info
            or re.search(r"\[ SAE_OFFLOAD \]", iw_phy_info) is not None)


def _sta_phy():
    code, out = run_cmd(["ubus", "call", "network.wireless", "status"], timeout=CMD_TIMEOUT_SECS)
    ifname = parse_sta_ifname(out) if code == 0 else ""
    if not ifname:
        return ""
    try:
        with open(f"/sys/class/net/{ifname}/phy80211/name") as f:
            return f.read().strip()
    except OSError:
        return ""


def sta_supports_sae():
    """Whether the STA interface can join a network with WPA3 (SAE).

    Both must allow it: OpenWrt's wpa_supplicant exits 0 for `-vsae` when it
    was built with SAE (LuCI checks the same way), and the radio's driver must
    list SAE support. Cached once known: neither changes while fim runs.
    """
    global _sae_supported
    if _sae_supported is not None:
        return _sae_supported
    wpa_supplicant = _find_sbin("wpa_supplicant")
    iw = _find_sbin("iw")
    if not wpa_supplicant or not iw:
        _sae_supported = False
        return False
    if run_cmd([wpa_supplicant, "-vsae"], timeout=CMD_TIMEOUT_SECS)[0] != 0:
        _sae_supported = False
        return False
    phy = _sta_phy()
    if not phy:
        return False  # the interface is not up yet; ask again next time
    code, out = run_cmd([iw, "phy", phy, "info"], timeout=CMD_TIMEOUT_SECS)
    if code != 0:
        return False
    _sae_supported = phy_info_supports_sae(out)
    return _sae_supported


def default_encryption(sae_supported):
    """For a network whose security type fim does not know, e.g. the backup.

    sae-mixed lets the client join WPA2, WPA3 and transition networks alike,
    but only a wpa_supplicant built with SAE can use it; psk2 works everywhere
    else.
    """
    return "sae-mixed" if sae_supported else "psk2"


def sta_set_commands(net, sae_supported=None):
    """uci commands that point the STA interface at `net`.

    A network with a password but no known security type (the backup) gets
    default_encryption(); one read from uci keeps its own.
    """
    prefix = f"wireless.{STA_SECTION}"
    cmds = [["uci", "set", f"{prefix}.ssid={net['ssid']}"]]
    if net.get("key"):
        encryption = net.get("encryption") or ""
        if encryption in ("", "none", "owe"):
            if sae_supported is None:
                sae_supported = sta_supports_sae()
            encryption = default_encryption(sae_supported)
        cmds.append(["uci", "set", f"{prefix}.encryption={encryption}"])
        cmds.append(["uci", "set", f"{prefix}.key={net['key']}"])
    else:
        cmds.append(["uci", "set", f"{prefix}.encryption=none"])
        # An empty value deletes the option.
        cmds.append(["uci", "set", f"{prefix}.key="])
    return cmds


def _uci_get(key):
    code, out = run_cmd(["uci", "-q", "get", key], timeout=CMD_TIMEOUT_SECS)
    return out.strip() if code == 0 else ""


def read_sta():
    prefix = f"wireless.{STA_SECTION}"
    return {
        "ssid": _uci_get(f"{prefix}.ssid"),
        "key": _uci_get(f"{prefix}.key"),
        "encryption": _uci_get(f"{prefix}.encryption"),
    }


def apply_sta(net, log=print):
    """Point the STA interface at `net`, commit and reload Wi-Fi. Returns success."""
    for cmd in sta_set_commands(net) + [["uci", "commit", "wireless"]]:
        code, out = run_cmd(cmd, timeout=CMD_TIMEOUT_SECS)
        if code != 0:
            log(f"`{' '.join(cmd[:2])} …` failed: {out.strip()}")
            return False
    code, out = run_cmd(["wifi", "reload"], timeout=CMD_TIMEOUT_SECS + 20)
    if code != 0:
        log(f"`wifi reload` failed: {out.strip()}")
        return False
    return True


# ── Settings from the web UI ────────────────────────────────────────────────

def displayed_networks(state, sta):
    """(primary, backup) to show in the UI, given the stored state and uci's STA."""
    if state["active"] == BACKUP and state["primary"]:
        primary = state["primary"]
    else:
        primary = sta
    return primary, state["backup"]


def plan_settings_change(state, sta, primary_update, backup_update):
    """Fold a settings change from the UI into the state.

    primary_update: {"ssid", "key"} with "" meaning unchanged (the form's
    existing semantics), or None. backup_update: {"ssid", "key"} as the whole
    new backup, an empty ssid removing it, or None for unchanged.

    Returns (new_state, net_for_uci): net_for_uci is the network the STA
    interface must now use, or None to leave uci alone.
    """
    new = {
        "active": state["active"],
        "backup": state["backup"],
        "primary": state["primary"],
    }
    net_for_uci = None

    if primary_update and (primary_update.get("ssid") or primary_update.get("key")):
        if new["active"] == BACKUP and new["primary"]:
            # Stored only; the watchdog returns to it once it is in range.
            merged = dict(new["primary"])
            for field in ("ssid", "key"):
                if primary_update.get(field):
                    merged[field] = primary_update[field]
            new["primary"] = merged
        else:
            merged = dict(sta)
            for field in ("ssid", "key"):
                if primary_update.get(field):
                    merged[field] = primary_update[field]
            if merged != sta:
                new["active"] = PRIMARY
                net_for_uci = merged

    if backup_update is not None:
        ssid = backup_update.get("ssid") or ""
        key = backup_update.get("key") or ""
        old = new["backup"]
        if not ssid:
            new["backup"] = None
            if new["active"] == BACKUP:
                new["active"] = PRIMARY
                net_for_uci = new["primary"]
        elif not old or old["ssid"] != ssid or old["key"] != key:
            new["backup"] = {"ssid": ssid, "key": key, "encryption": ""}
            if new["active"] == BACKUP:
                net_for_uci = new["backup"]

    return new, net_for_uci


# ── Watchdog ────────────────────────────────────────────────────────────────

def parse_sta_ifname(wireless_status_json, section=STA_SECTION):
    """Kernel interface name of `section` from `ubus call network.wireless status`."""
    try:
        status = json.loads(wireless_status_json)
    except ValueError:
        return ""
    if not isinstance(status, dict):
        return ""
    for radio in status.values():
        for iface in (radio or {}).get("interfaces", []) or []:
            if iface.get("section") == section and iface.get("ifname"):
                return iface["ifname"]
    return ""


def _unescape_iw_ssid(text):
    """iw prints unprintable SSID bytes, and edge spaces, as \\xNN."""
    out = bytearray()
    i = 0
    raw = text.encode("utf-8", "surrogateescape")
    while i < len(raw):
        if raw[i:i + 2] == b"\\x" and i + 4 <= len(raw):
            try:
                out.append(int(raw[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return out.decode("utf-8", "replace")


def parse_scan_ssids(iw_scan_output):
    """SSIDs seen in `iw dev <if> scan` output."""
    ssids = set()
    for line in iw_scan_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("SSID: "):
            ssids.add(_unescape_iw_ssid(stripped[len("SSID: "):]))
    return ssids


def parse_ifstatus_l3_device(ifstatus_output):
    """The device carrying an interface's traffic, if it has an IPv4 address."""
    try:
        info = json.loads(ifstatus_output)
    except ValueError:
        return ""
    if not isinstance(info, dict) or not info.get("ipv4-address"):
        return ""
    return info.get("l3_device") or info.get("device") or ""


def uplink_device():
    """Device of the Wi-Fi uplink when it has an address, else ""."""
    for iface in uplink_network_candidates(get_sta_network_name()):
        code, out = run_cmd(["ifstatus", iface], timeout=CMD_TIMEOUT_SECS)
        device = parse_ifstatus_l3_device(out) if code == 0 else ""
        if device:
            return device
    return ""


def internet_reachable(device, probes=INTERNET_PROBES, timeout=PROBE_TIMEOUT_SECS):
    """Whether any probe accepts a TCP connection sent out through `device`.

    Bound to the device, so a working Ethernet uplink on the same router
    cannot make a dead Wi-Fi uplink look fine.
    """
    for host, port in probes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, device.encode() + b"\0")
            sock.connect((host, port))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


class FailoverPolicy:
    """Decides when to switch; no I/O, so it can be tested with a fake clock."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.down_since = None
        self.next_return_check = 0.0
        self.return_wait = RETURN_CHECK_SECS
        self.returning_since = None

    def decide(self, now, uplink_up, active):
        """Return "switch", "scan" or None."""
        if uplink_up:
            self.down_since = None
            if active == PRIMARY:
                if self.returning_since is not None and now - self.returning_since >= RETURN_STABLE_SECS:
                    self.returning_since = None
                    self.return_wait = RETURN_CHECK_SECS
                return None
            return "scan" if now >= self.next_return_check else None
        if self.down_since is None:
            self.down_since = now
        return "switch" if now - self.down_since >= FAILOVER_AFTER_SECS else None

    def switched(self, now, to):
        # The new network counts as down until it gets an address.
        self.down_since = now
        if to == PRIMARY:
            self.returning_since = now
        else:
            if self.returning_since is not None:
                # The primary did not hold: wait longer before trying it again.
                self.return_wait = min(self.return_wait * 2, MAX_RETURN_CHECK_SECS)
                self.returning_since = None
            self.next_return_check = now + self.return_wait

    def scanned(self, now):
        self.next_return_check = now + self.return_wait

    def reloaded(self, now):
        """uci was changed from the UI: give the new network time to come up."""
        self.down_since = now


class UplinkWatchdog:
    def __init__(self, iw_path, clock=time.monotonic):
        self.iw_path = iw_path
        self.clock = clock
        self.policy = FailoverPolicy()
        self._stop = threading.Event()
        self._warned = set()

    def _log(self, message):
        # flush: procd hands stdout a pipe, which Python would buffer for hours.
        print(f"Uplink failover: {message}", flush=True)

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            self._log(message)

    def primary_in_range(self, ssid):
        if not self.iw_path:
            return False
        code, out = run_cmd(["ubus", "call", "network.wireless", "status"], timeout=CMD_TIMEOUT_SECS)
        ifname = parse_sta_ifname(out) if code == 0 else ""
        if not ifname:
            self._warn_once("ifname", "cannot find the STA interface name, not scanning for the primary")
            return False
        code, out = run_cmd([self.iw_path, "dev", ifname, "scan"], timeout=SCAN_TIMEOUT_SECS)
        if code != 0:
            # The radio refuses a scan while wpa_supplicant runs its own (it
            # does so non-stop while it cannot connect): read the results the
            # kernel cached from those instead.
            code, out = run_cmd([self.iw_path, "dev", ifname, "scan", "dump"], timeout=SCAN_TIMEOUT_SECS)
        if code != 0:
            self._warn_once(("scan", out.strip()[:80]), f"scan on {ifname} failed: {out.strip()}")
            return False
        return ssid in parse_scan_ssids(out)

    def switch_to(self, target, now):
        """Switch under action_lock; skipped when a user action holds it."""
        if not action_lock.acquire(blocking=False):
            return
        try:
            with _file_lock:
                state = load_uplink_state()
            if not state["backup"] or state["active"] == target:
                return
            if target == BACKUP:
                current = read_sta()
                if current["ssid"]:
                    state["primary"] = current
                net = state["backup"]
            else:
                net = state["primary"]
                if not net:
                    self._warn_once("no-primary", "the primary network is not known, staying on the backup")
                    return
            self._log(f"switching the uplink to the {target} network ({net['ssid']})")
            if not apply_sta(net, log=self._log):
                return
            state["active"] = target
            with _file_lock:
                save_uplink_state(state)
            self.policy.switched(now, target)
        finally:
            action_lock.release()

    def tick(self):
        with _file_lock:
            state = load_uplink_state()
        if not state["backup"]:
            self.policy.reset()
            return
        now = self.clock()
        device = uplink_device()
        up = bool(device) and internet_reachable(device)
        active = state["active"]
        action = self.policy.decide(now, up, active)
        if action == "switch":
            problem = "no internet" if device else "no address"
            self._log(f"{problem} on the {active} network for {FAILOVER_AFTER_SECS}s")
            self.switch_to(PRIMARY if active == BACKUP else BACKUP, now)
        elif action == "scan" and state["primary"]:
            self.policy.scanned(now)
            if self.primary_in_range(state["primary"]["ssid"]):
                self._log("the primary network is in range again")
                self.switch_to(PRIMARY, self.clock())

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self._warn_once(("loop", type(e).__name__), f"check failed: {e}")
            self._stop.wait(CHECK_INTERVAL_SECS)

    def stop(self):
        self._stop.set()


_watchdog = None


def start_uplink_failover():
    """Start the watchdog on OpenWrt; it idles until a backup network is set."""
    global _watchdog
    if not IS_OPENWRT or _watchdog is not None:
        return
    iw_path = _find_sbin("iw")
    if not iw_path:
        print("Uplink failover: `iw` not found, will fail over but not scan to return to the primary",
              flush=True)
    _watchdog = UplinkWatchdog(iw_path)
    threading.Thread(target=_watchdog.run, name="uplink-failover", daemon=True).start()


def stop_uplink_failover():
    if _watchdog is not None:
        _watchdog.stop()


def get_uplink_settings():
    """(state, sta) for the web UI."""
    with _file_lock:
        state = load_uplink_state()
    return state, read_sta()


def update_uplink_settings(primary_update, backup_update):
    """Fold a UI change into the stored state. Caller holds action_lock.

    Returns (changed, net_for_uci): whether the stored state changed, and the
    network the STA interface must now use, or None.
    """
    with _file_lock:
        state = load_uplink_state()
        sta = read_sta()
        new, net_for_uci = plan_settings_change(state, sta, primary_update, backup_update)
        if new != state:
            save_uplink_state(new)
    if _watchdog is not None and new["active"] != state["active"]:
        _watchdog.policy.reset()
    return new != state, net_for_uci


def note_sta_reloaded():
    """Tell the watchdog the STA network was just changed from the UI."""
    if _watchdog is not None:
        _watchdog.policy.reloaded(_watchdog.clock())
