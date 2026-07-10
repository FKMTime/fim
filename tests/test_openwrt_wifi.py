import unittest

from fim.openwrt_wifi import (
    parse_hotspot_radio_sections,
    parse_ifstatus_ip,
    uplink_network_candidates,
)


class HotspotRadioDiscoveryTests(unittest.TestCase):
    def test_single_ap_radio(self):
        uci = "\n".join([
            "wireless.radio0=wifi-device",
            "wireless.default_radio0=wifi-iface",
            "wireless.radio1=wifi-device",
            "wireless.default_radio1=wifi-iface",
        ])
        self.assertEqual(parse_hotspot_radio_sections(uci), ["default_radio1"])

    def test_dual_ap_radios(self):
        uci = "\n".join([
            "wireless.default_radio0=wifi-iface",
            "wireless.default_radio1=wifi-iface",
            "wireless.default_radio2=wifi-iface",
        ])
        self.assertEqual(parse_hotspot_radio_sections(uci), ["default_radio1", "default_radio2"])

    def test_ignores_sta_radio(self):
        uci = "wireless.default_radio0=wifi-iface\nwireless.default_radio0.ssid=upstream"
        self.assertEqual(parse_hotspot_radio_sections(uci), [])


class UplinkNetworkTests(unittest.TestCase):
    def test_parse_ifstatus_ip(self):
        payload = '{"ipv4-address":[{"address":"10.0.0.5","mask":24}]}'
        self.assertEqual(parse_ifstatus_ip(payload), "10.0.0.5")

    def test_parse_ifstatus_ip_empty(self):
        self.assertEqual(parse_ifstatus_ip('{"ipv4-address":[]}'), "")

    def test_uplink_candidates_prefers_sta_network(self):
        self.assertEqual(
            uplink_network_candidates("wwan"),
            ["wwan", "wanWIFI"],
        )

    def test_uplink_candidates_fallbacks(self):
        self.assertEqual(uplink_network_candidates(""), ["wwan", "wanWIFI"])


if __name__ == "__main__":
    unittest.main()
