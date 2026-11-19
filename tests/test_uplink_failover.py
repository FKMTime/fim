import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import fim.uplink_failover as uf
from fim.uplink_failover import (
    BACKUP,
    FAILOVER_AFTER_SECS,
    MAX_RETURN_CHECK_SECS,
    PRIMARY,
    RETURN_CHECK_SECS,
    RETURN_STABLE_SECS,
    FailoverPolicy,
    UplinkWatchdog,
    displayed_networks,
    load_uplink_state,
    parse_scan_ssids,
    parse_sta_ifname,
    plan_settings_change,
    save_uplink_state,
    sta_set_commands,
)

HOME = {"ssid": "HomeNet", "key": "home-pass-1", "encryption": "psk2"}
SPARE = {"ssid": "SpareNet", "key": "spare-pass-1", "encryption": ""}


def state(active=PRIMARY, backup=SPARE, primary=None):
    return {"active": active, "backup": backup, "primary": primary}


class PolicyTests(unittest.TestCase):
    def test_short_outage_does_not_switch(self):
        p = FailoverPolicy()
        self.assertIsNone(p.decide(0, False, PRIMARY))
        self.assertIsNone(p.decide(FAILOVER_AFTER_SECS - 1, False, PRIMARY))
        self.assertIsNone(p.decide(FAILOVER_AFTER_SECS, True, PRIMARY))
        self.assertIsNone(p.decide(FAILOVER_AFTER_SECS + 10, False, PRIMARY))

    def test_switches_after_sustained_outage(self):
        p = FailoverPolicy()
        p.decide(0, False, PRIMARY)
        self.assertEqual(p.decide(FAILOVER_AFTER_SECS, False, PRIMARY), "switch")

    def test_new_network_gets_full_grace_after_switch(self):
        p = FailoverPolicy()
        p.decide(0, False, PRIMARY)
        p.switched(100, BACKUP)
        self.assertIsNone(p.decide(100 + FAILOVER_AFTER_SECS - 1, False, BACKUP))
        self.assertEqual(p.decide(100 + FAILOVER_AFTER_SECS, False, BACKUP), "switch")

    def test_reload_from_ui_resets_the_timer(self):
        p = FailoverPolicy()
        p.decide(0, False, PRIMARY)
        p.reloaded(50)
        self.assertIsNone(p.decide(FAILOVER_AFTER_SECS, False, PRIMARY))
        self.assertEqual(p.decide(50 + FAILOVER_AFTER_SECS, False, PRIMARY), "switch")

    def test_scans_for_primary_while_on_backup(self):
        p = FailoverPolicy()
        p.switched(0, BACKUP)
        self.assertIsNone(p.decide(RETURN_CHECK_SECS - 1, True, BACKUP))
        self.assertEqual(p.decide(RETURN_CHECK_SECS, True, BACKUP), "scan")
        p.scanned(RETURN_CHECK_SECS)
        self.assertIsNone(p.decide(RETURN_CHECK_SECS + 1, True, BACKUP))

    def test_failed_returns_back_off(self):
        p = FailoverPolicy()
        p.switched(0, BACKUP)
        p.switched(300, PRIMARY)      # primary seen in a scan
        p.switched(400, BACKUP)       # ...but it did not come up
        self.assertEqual(p.next_return_check, 400 + 2 * RETURN_CHECK_SECS)
        for _ in range(10):
            p.switched(0, PRIMARY)
            p.switched(0, BACKUP)
        self.assertEqual(p.return_wait, MAX_RETURN_CHECK_SECS)

    def test_stable_return_resets_back_off(self):
        p = FailoverPolicy()
        p.switched(0, BACKUP)
        p.switched(300, PRIMARY)
        p.switched(400, BACKUP)
        p.switched(1000, PRIMARY)
        p.decide(1000 + RETURN_STABLE_SECS, True, PRIMARY)
        self.assertEqual(p.return_wait, RETURN_CHECK_SECS)


class StateFileTests(unittest.TestCase):
    def test_missing_file_means_no_backup(self):
        self.assertEqual(load_uplink_state("/nonexistent/wifi_uplink.json"),
                         {"active": PRIMARY, "backup": None, "primary": None})

    def test_round_trip_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wifi_uplink.json")
            s = state(active=BACKUP, primary=HOME)
            save_uplink_state(s, path)
            self.assertEqual(load_uplink_state(path), s)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_garbage_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wifi_uplink.json")
            with open(path, "w") as f:
                json.dump({"active": "other", "backup": {"ssid": ""}}, f)
            self.assertEqual(load_uplink_state(path),
                             {"active": PRIMARY, "backup": None, "primary": None})


class UciCommandTests(unittest.TestCase):
    def test_unknown_type_uses_sae_mixed_when_supported(self):
        self.assertIn(["uci", "set", "wireless.default_radio0.encryption=sae-mixed"],
                      sta_set_commands(SPARE, sae_supported=True))

    def test_unknown_type_falls_back_to_psk2(self):
        self.assertEqual(sta_set_commands(SPARE, sae_supported=False), [
            ["uci", "set", "wireless.default_radio0.ssid=SpareNet"],
            ["uci", "set", "wireless.default_radio0.encryption=psk2"],
            ["uci", "set", "wireless.default_radio0.key=spare-pass-1"],
        ])

    def test_keeps_primary_encryption(self):
        net = dict(HOME, encryption="sae-mixed")
        self.assertIn(["uci", "set", "wireless.default_radio0.encryption=sae-mixed"],
                      sta_set_commands(net, sae_supported=False))

    def test_known_type_is_kept_when_sae_is_supported(self):
        self.assertIn(["uci", "set", "wireless.default_radio0.encryption=psk2"],
                      sta_set_commands(HOME, sae_supported=True))

    def test_open_network(self):
        self.assertEqual(sta_set_commands({"ssid": "Cafe", "key": ""}, sae_supported=True), [
            ["uci", "set", "wireless.default_radio0.ssid=Cafe"],
            ["uci", "set", "wireless.default_radio0.encryption=none"],
            ["uci", "set", "wireless.default_radio0.key="],
        ])


# Trimmed `iw phy <phy> info` output.
PHY_MAC80211 = "Wiphy phy1\n\tDevice supports SAE with AUTHENTICATE command\n"
PHY_BRCMFMAC = "Wiphy phy0\n\tSupported extended features:\n\t\t* [ DFS_OFFLOAD ]: DFS offload\n"
PHY_SAE_OFFLOAD = "Wiphy phy0\n\tSupported extended features:\n\t\t* [ SAE_OFFLOAD ]: SAE offload support\n"
PHY_AP_OFFLOAD_ONLY = "Wiphy phy0\n\tSupported extended features:\n\t\t* [ SAE_OFFLOAD_AP ]: AP mode SAE authentication offload support\n"

WIRELESS_STATUS = json.dumps({"radio0": {"interfaces": [
    {"section": "default_radio0", "ifname": "phy0-sta0"}]}})


class SaeDetectionTests(unittest.TestCase):
    def setUp(self):
        uf._sae_supported = None
        self.addCleanup(setattr, uf, "_sae_supported", None)

    def test_phy_capability(self):
        self.assertTrue(uf.phy_info_supports_sae(PHY_MAC80211))
        self.assertTrue(uf.phy_info_supports_sae(PHY_SAE_OFFLOAD))
        self.assertFalse(uf.phy_info_supports_sae(PHY_BRCMFMAC))
        self.assertFalse(uf.phy_info_supports_sae(PHY_AP_OFFLOAD_ONLY))

    def _detect(self, wpa_code, phy_info, phy="phy0"):
        replies = {
            ("/usr/sbin/wpa_supplicant", "-vsae"): (wpa_code, ""),
            ("ubus", "call", "network.wireless", "status"): (0, WIRELESS_STATUS),
            ("/usr/sbin/iw", "phy", "phy0", "info"): (0, phy_info),
        }
        with mock.patch.object(uf.shutil, "which", side_effect=lambda name, path: f"/usr/sbin/{name}"), \
             mock.patch.object(uf, "_sta_phy", return_value=phy), \
             mock.patch.object(uf, "run_cmd", side_effect=lambda args, timeout: replies[tuple(args)]) as run:
            return uf.sta_supports_sae(), run

    def test_needs_both_wpa_supplicant_and_driver(self):
        self.assertTrue(self._detect(0, PHY_MAC80211)[0])

    def test_brcmfmac_without_sae_falls_back(self):
        # The Raspberry Pi's built-in Wi-Fi: wpa_supplicant can, the chip cannot.
        self.assertFalse(self._detect(0, PHY_BRCMFMAC)[0])

    def test_wpa_supplicant_without_sae(self):
        supported, run = self._detect(1, PHY_MAC80211)
        self.assertFalse(supported)
        self.assertEqual(run.call_count, 1)

    def test_result_is_cached(self):
        self._detect(0, PHY_MAC80211)
        supported, run = self._detect(0, PHY_BRCMFMAC)
        self.assertTrue(supported)
        run.assert_not_called()

    def test_unknown_phy_is_not_cached(self):
        self.assertFalse(self._detect(0, PHY_MAC80211, phy="")[0])
        self.assertIsNone(uf._sae_supported)


class SettingsTests(unittest.TestCase):
    def test_ui_shows_stored_primary_while_on_backup(self):
        primary, backup = displayed_networks(state(BACKUP, primary=HOME), SPARE)
        self.assertEqual(primary, HOME)
        self.assertEqual(backup, SPARE)

    def test_ui_shows_uci_while_on_primary(self):
        primary, _ = displayed_networks(state(PRIMARY, primary={"ssid": "Old", "key": ""}), HOME)
        self.assertEqual(primary, HOME)

    def test_primary_edit_on_primary_goes_to_uci(self):
        new, net = plan_settings_change(state(), HOME, {"ssid": "NewHome", "key": ""}, None)
        self.assertEqual(net, dict(HOME, ssid="NewHome"))
        self.assertEqual(new, state())

    def test_unchanged_primary_on_primary_is_a_no_op(self):
        new, net = plan_settings_change(state(), HOME, {"ssid": "HomeNet", "key": "home-pass-1"}, None)
        self.assertIsNone(net)
        self.assertEqual(new, state())

    def test_primary_edit_on_backup_is_stored_only(self):
        new, net = plan_settings_change(state(BACKUP, primary=HOME), SPARE,
                                        {"ssid": "HomeNet", "key": "new-pass-12"}, None)
        self.assertIsNone(net)
        self.assertEqual(new["primary"]["key"], "new-pass-12")
        self.assertEqual(new["active"], BACKUP)

    def test_unchanged_backup_is_a_no_op(self):
        s = state()
        new, net = plan_settings_change(s, HOME, {"ssid": "", "key": ""},
                                        {"ssid": "SpareNet", "key": "spare-pass-1"})
        self.assertEqual(new, s)
        self.assertIsNone(net)

    def test_set_backup_while_on_primary(self):
        new, net = plan_settings_change(state(backup=None), HOME, None,
                                        {"ssid": "SpareNet", "key": "spare-pass-1"})
        self.assertEqual(new["backup"]["ssid"], "SpareNet")
        self.assertIsNone(net)

    def test_edit_backup_while_on_it_goes_to_uci(self):
        new, net = plan_settings_change(state(BACKUP, primary=HOME), SPARE, None,
                                        {"ssid": "SpareNet", "key": "other-pass-1"})
        self.assertEqual(net["key"], "other-pass-1")
        self.assertEqual(new["active"], BACKUP)

    def test_removing_backup_while_on_it_restores_primary(self):
        new, net = plan_settings_change(state(BACKUP, primary=HOME), SPARE, None,
                                        {"ssid": "", "key": ""})
        self.assertEqual(net, HOME)
        self.assertEqual(new["active"], PRIMARY)
        self.assertIsNone(new["backup"])


class InternetCheckTests(unittest.TestCase):
    def test_l3_device_needs_an_address(self):
        up = json.dumps({"up": True, "l3_device": "phy0-sta0",
                         "ipv4-address": [{"address": "192.0.2.10", "mask": 24}]})
        no_addr = json.dumps({"up": False, "device": "phy0-sta0", "ipv4-address": []})
        self.assertEqual(uf.parse_ifstatus_l3_device(up), "phy0-sta0")
        self.assertEqual(uf.parse_ifstatus_l3_device(no_addr), "")
        self.assertEqual(uf.parse_ifstatus_l3_device("Interface wwan not found"), "")

    def _probe(self, connect_results):
        sockets = []

        def make_socket(*args):
            sock = mock.Mock()
            result = connect_results[len(sockets)]
            if isinstance(result, Exception):
                sock.connect.side_effect = result
            sockets.append(sock)
            return sock

        with mock.patch.object(uf.socket, "socket", side_effect=make_socket):
            return uf.internet_reachable("phy0-sta0"), sockets

    def test_first_probe_answers(self):
        reachable, sockets = self._probe([None])
        self.assertTrue(reachable)
        sockets[0].setsockopt.assert_called_once_with(
            uf.socket.SOL_SOCKET, uf.SO_BINDTODEVICE, b"phy0-sta0\0")
        sockets[0].connect.assert_called_once_with(("1.1.1.1", 443))
        sockets[0].close.assert_called_once()

    def test_later_probe_answers(self):
        reachable, sockets = self._probe([TimeoutError(), OSError("unreachable"), None])
        self.assertTrue(reachable)
        self.assertEqual(len(sockets), 3)
        for sock in sockets:
            sock.close.assert_called_once()

    def test_no_probe_answers(self):
        reachable, _ = self._probe([TimeoutError(), TimeoutError(), TimeoutError()])
        self.assertFalse(reachable)


class ParserTests(unittest.TestCase):
    def test_sta_ifname(self):
        status = json.dumps({
            "radio0": {"interfaces": [{"section": "default_radio0", "ifname": "phy0-sta0"}]},
            "radio1": {"interfaces": [{"section": "default_radio1", "ifname": "phy1-ap0"}]},
        })
        self.assertEqual(parse_sta_ifname(status), "phy0-sta0")
        self.assertEqual(parse_sta_ifname("not json"), "")
        self.assertEqual(parse_sta_ifname("{}"), "")

    def test_scan_ssids(self):
        out = "\n".join([
            "BSS 02:00:00:00:00:01(on phy0-sta0)",
            "\tSSID: HomeNet",
            "BSS 02:00:00:00:00:02(on phy0-sta0)",
            "\tSSID: Venue Guest",
            "BSS 02:00:00:00:00:03(on phy0-sta0)",
            "\tSSID: \\x20Edge\\x20",
        ])
        self.assertEqual(parse_scan_ssids(out), {"HomeNet", "Venue Guest", " Edge "})


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "wifi_uplink.json")
        self.now = 0
        self.uplink_ip = ""
        self.internet = True
        self.sta = dict(HOME)
        self.applied = []
        patches = [
            mock.patch.object(uf, "UPLINK_FILE", self.path),
            mock.patch.object(uf, "load_uplink_state",
                              side_effect=lambda: load_uplink_state(self.path)),
            mock.patch.object(uf, "save_uplink_state",
                              side_effect=lambda s: save_uplink_state(s, self.path)),
            mock.patch.object(uf, "uplink_device",
                              side_effect=lambda: "phy0-sta0" if self.uplink_ip else ""),
            mock.patch.object(uf, "internet_reachable", side_effect=lambda device: self.internet),
            mock.patch.object(uf, "get_sta_network_name", return_value="wwan"),
            mock.patch.object(uf, "read_sta", side_effect=lambda: dict(self.sta)),
            mock.patch.object(uf, "apply_sta", side_effect=self._apply),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)
        self.wd = UplinkWatchdog(None, clock=lambda: self.now)

    def _apply(self, net, log=print):
        self.applied.append(net["ssid"])
        self.sta = dict(net)
        return True

    def test_idle_without_backup(self):
        for self.now in range(0, 600, 15):
            self.wd.tick()
        self.assertEqual(self.applied, [])

    def test_fails_over_and_remembers_primary(self):
        save_uplink_state(state(), self.path)
        with mock.patch("builtins.print"):
            for self.now in range(0, FAILOVER_AFTER_SECS + 15, 15):
                self.wd.tick()
        self.assertEqual(self.applied, ["SpareNet"])
        stored = load_uplink_state(self.path)
        self.assertEqual(stored["active"], BACKUP)
        self.assertEqual(stored["primary"], HOME)

    def test_fails_over_when_address_but_no_internet(self):
        save_uplink_state(state(), self.path)
        self.uplink_ip = "192.0.2.10"
        self.internet = False
        with mock.patch("builtins.print") as out:
            for self.now in range(0, FAILOVER_AFTER_SECS + 15, 15):
                self.wd.tick()
        self.assertEqual(self.applied, ["SpareNet"])
        self.assertIn("no internet on the primary network", str(out.call_args_list))

    def test_stays_while_internet_works(self):
        save_uplink_state(state(), self.path)
        self.uplink_ip = "192.0.2.10"
        for self.now in range(0, 600, 15):
            self.wd.tick()
        self.assertEqual(self.applied, [])

    def test_returns_when_primary_in_range(self):
        save_uplink_state(state(BACKUP, primary=HOME), self.path)
        self.sta = dict(SPARE)
        self.uplink_ip = "192.0.2.10"
        self.wd.primary_in_range = mock.Mock(return_value=True)
        with mock.patch("builtins.print"):
            self.wd.tick()
        self.assertEqual(self.applied, ["HomeNet"])
        self.assertEqual(load_uplink_state(self.path)["active"], PRIMARY)

    def test_busy_scan_falls_back_to_cached_results(self):
        wd = UplinkWatchdog("/usr/sbin/iw")
        status = json.dumps({"radio0": {"interfaces": [
            {"section": "default_radio0", "ifname": "phy0-sta0"}]}})
        replies = {
            ("ubus", "call", "network.wireless", "status"): (0, status),
            ("/usr/sbin/iw", "dev", "phy0-sta0", "scan"): (240, "command failed: Resource busy (-16)\n"),
            ("/usr/sbin/iw", "dev", "phy0-sta0", "scan", "dump"): (0, "BSS 02:00:00:00:00:01\n\tSSID: HomeNet\n"),
        }
        with mock.patch.object(uf, "run_cmd", side_effect=lambda args, timeout: replies[tuple(args)]):
            self.assertTrue(wd.primary_in_range("HomeNet"))
            self.assertFalse(wd.primary_in_range("Elsewhere"))

    def test_skips_switch_while_a_user_action_runs(self):
        save_uplink_state(state(), self.path)
        with uf.action_lock, mock.patch("builtins.print"):
            for self.now in range(0, FAILOVER_AFTER_SECS + 15, 15):
                self.wd.tick()
        self.assertEqual(self.applied, [])
        with mock.patch("builtins.print"):
            self.wd.tick()
        self.assertEqual(self.applied, ["SpareNet"])


if __name__ == "__main__":
    unittest.main()
