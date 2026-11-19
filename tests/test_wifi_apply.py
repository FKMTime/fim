import unittest
from unittest import mock

import fim.workers as workers

HOTSPOT = {"default_radio1": {"ssid": "CompNet", "key": "comp-pass-1"}}
HOME = {"ssid": "HomeNet", "key": "home-pass-1", "encryption": "psk2"}


class WifiApplyTests(unittest.TestCase):
    """do_wifi_async with uci, Wi-Fi and compose replaced by recorders."""

    def setUp(self):
        self.commands = []
        self.stages = []
        self.done = []
        self.uplink = (False, None)

        def run_cmd(args, **kwargs):
            self.commands.append(args)
            return 0, ""

        def run_live(args, **kwargs):
            self.commands.append(args)
            return 0, ""

        patches = [
            mock.patch.object(workers, "get_hotspot_radio_sections", return_value=list(HOTSPOT)),
            mock.patch.object(workers, "get_hotspot_values", return_value=HOTSPOT),
            mock.patch.object(workers, "update_uplink_settings", side_effect=lambda *a: self.uplink),
            mock.patch.object(workers, "note_sta_reloaded"),
            mock.patch.object(workers, "run_cmd", side_effect=run_cmd),
            mock.patch.object(workers, "run_cmd_live", side_effect=run_live),
            mock.patch.object(workers, "run_compose_live", side_effect=run_live),
            mock.patch.object(workers, "compose_status", return_value=(True, "")),
            mock.patch.object(workers, "get_selected", return_value="demo"),
            mock.patch.object(workers, "get_instances", return_value={"demo": "/tmp/demo"}),
            mock.patch.object(workers, "port80_released", return_value=mock.MagicMock()),
            mock.patch.object(workers, "progress_reset", side_effect=lambda names: self.stages.append(names)),
            mock.patch.object(workers, "progress_stage"),
            mock.patch.object(workers, "progress_done", side_effect=lambda ok: self.done.append(ok)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def apply(self, hs_ssid="CompNet", hs_psk="comp-pass-1", sta_ssid="HomeNet",
              sta_psk="home-pass-1", backup=None):
        workers.do_wifi_async(hs_ssid, hs_psk, sta_ssid, sta_psk, backup)

    def test_saving_only_the_backup_touches_nothing(self):
        self.uplink = (True, None)
        self.apply(backup={"ssid": "PhoneNet", "key": "phone-pass-1"})
        self.assertEqual(self.commands, [])
        self.assertEqual(self.done, [True])

    def test_unchanged_form_touches_nothing(self):
        self.apply()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.done, [True])

    def test_hotspot_change_reloads_and_restarts(self):
        self.apply(hs_psk="new-pass-12")
        self.assertIn(["uci", "set", "wireless.default_radio1.key=new-pass-12"], self.commands)
        self.assertIn(["wifi", "reload"], self.commands)
        self.assertIn(["docker", "compose", "restart"], self.commands)
        self.assertEqual(self.done, [True])

    def test_uplink_change_is_applied(self):
        self.uplink = (False, dict(HOME, ssid="NewHome"))
        self.apply(sta_ssid="NewHome")
        self.assertIn(["uci", "set", "wireless.default_radio0.ssid=NewHome"], self.commands)
        self.assertIn(["wifi", "reload"], self.commands)
        workers.note_sta_reloaded.assert_called_once()


if __name__ == "__main__":
    unittest.main()
