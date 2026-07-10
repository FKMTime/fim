"""OpenWrt wireless UCI helpers."""
import json
import re

from fim.commands import run_cmd

_HOTSPOT_RADIO_RE = re.compile(r"^wireless\.(default_radio(\d+))=")


def parse_hotspot_radio_sections(uci_show_output):
    """Return sorted default_radioN section names for AP interfaces (N >= 1)."""
    sections = set()
    for line in uci_show_output.splitlines():
        match = _HOTSPOT_RADIO_RE.match(line.strip())
        if match and int(match.group(2)) >= 1:
            sections.add(match.group(1))
    return sorted(sections, key=lambda name: int(re.search(r"\d+", name).group()))


def get_hotspot_radio_sections():
    """Discover hotspot AP sections present in wireless UCI config."""
    code, out = run_cmd(["uci", "show", "wireless"], timeout=5)
    if code != 0:
        return ["default_radio1"]
    sections = parse_hotspot_radio_sections(out)
    return sections or ["default_radio1"]


def parse_ifstatus_ip(ifstatus_output):
    try:
        info = json.loads(ifstatus_output)
        addrs = info.get("ipv4-address", [])
        return addrs[0]["address"] if addrs else ""
    except Exception:
        return ""


def get_ifstatus_ip(iface):
    code, out = run_cmd(["ifstatus", iface], timeout=5)
    if code != 0:
        return ""
    return parse_ifstatus_ip(out)


def get_sta_network_name():
    code, out = run_cmd(["uci", "get", "wireless.default_radio0.network"], timeout=5)
    return out.strip() if code == 0 else ""


def uplink_network_candidates(sta_network=""):
    candidates = []
    if sta_network:
        candidates.append(sta_network)
    for fallback in ("wwan", "wanWIFI"):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def get_uplink_ip(sta_network=""):
    for iface in uplink_network_candidates(sta_network):
        ip = get_ifstatus_ip(iface)
        if ip:
            return ip
    return ""
