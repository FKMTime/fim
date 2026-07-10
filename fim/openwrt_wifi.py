"""OpenWrt wireless UCI helpers."""
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
