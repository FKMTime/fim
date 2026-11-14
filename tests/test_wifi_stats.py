import gzip
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import fim.wifi_stats as wifi_stats
from fim.wifi_stats import (
    WifiStatsRecorder,
    append_gzip_member,
    find_iw,
    parse_ap_interfaces,
    prune_wifi_logs,
    select_in_use_survey,
    select_wifi_logs_to_delete,
    snapshot_header,
    wifi_log_name,
)

# A router with one client (STA) radio and two hotspot (AP) radios.
IW_DEV = """\
phy#2
\tInterface phy2-ap0
\t\tifindex 14
\t\twdev 0x200000002
\t\taddr 02:00:00:00:02:01
\t\tssid ExampleHotspot
\t\ttype AP
\t\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz
\t\ttxpower 20.00 dBm
phy#1
\tInterface phy1-ap0
\t\tifindex 12
\t\taddr 02:00:00:00:01:01
\t\tssid ExampleHotspot
\t\ttype AP
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
phy#0
\tInterface phy0-sta0
\t\tifindex 10
\t\taddr 02:00:00:00:00:01
\t\tssid ExampleUplink
\t\ttype managed
"""

SURVEY_DUMP = """\
Survey data from phy1-ap0
\tfrequency:\t\t\t2412 MHz
\tnoise:\t\t\t\t-95 dBm
Survey data from phy1-ap0
\tfrequency:\t\t\t2437 MHz [in use]
\tnoise:\t\t\t\t-91 dBm
\tchannel active time:\t\t1000 ms
\tchannel busy time:\t\t250 ms
Survey data from phy1-ap0
\tfrequency:\t\t\t2462 MHz
"""

UTC = timezone.utc
NOW = datetime(2026, 3, 7, 9, 41, 5, tzinfo=UTC)
DAY = 86400
MB = 1024 * 1024


class ApInterfaceTests(unittest.TestCase):
    def test_finds_ap_interfaces_only(self):
        self.assertEqual(parse_ap_interfaces(IW_DEV), ["phy2-ap0", "phy1-ap0"])

    def test_no_interfaces(self):
        self.assertEqual(parse_ap_interfaces(""), [])
        self.assertEqual(parse_ap_interfaces("command failed: No such device\n"), [])


class SurveyTests(unittest.TestCase):
    def test_keeps_only_in_use_channel(self):
        self.assertEqual(
            select_in_use_survey(SURVEY_DUMP),
            "Survey data from phy1-ap0\n"
            "\tfrequency:\t\t\t2437 MHz [in use]\n"
            "\tnoise:\t\t\t\t-91 dBm\n"
            "\tchannel active time:\t\t1000 ms\n"
            "\tchannel busy time:\t\t250 ms\n",
        )

    def test_nothing_in_use(self):
        self.assertEqual(select_in_use_survey("Survey data from x\n\tfrequency: 2412 MHz\n"), "")


class NamingTests(unittest.TestCase):
    def test_file_name_uses_utc_hour(self):
        self.assertEqual(wifi_log_name(NOW), "wifi-2026-03-07_09.log.gz")

    def test_file_name_converts_local_time_to_utc(self):
        local = NOW.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(wifi_log_name(local), "wifi-2026-03-07_09.log.gz")

    def test_snapshot_header(self):
        self.assertEqual(snapshot_header(NOW), "=== 2026-03-07T09:41:05Z\n")


class RetentionTests(unittest.TestCase):
    now = 100 * DAY

    def test_deletes_files_older_than_30_days(self):
        files = [
            ("wifi-2026-01-01_10.log.gz", self.now - 31 * DAY, 10),
            ("wifi-2026-02-01_10.log.gz", self.now - 29 * DAY, 10),
        ]
        self.assertEqual(select_wifi_logs_to_delete(files, self.now), ["wifi-2026-01-01_10.log.gz"])

    def test_deletes_oldest_while_over_size_limit(self):
        files = [
            ("wifi-2026-03-01_12.log.gz", self.now - 1 * DAY, 80 * MB),
            ("wifi-2026-03-01_10.log.gz", self.now - 3 * DAY, 80 * MB),
            ("wifi-2026-03-01_11.log.gz", self.now - 2 * DAY, 80 * MB),
        ]
        self.assertEqual(select_wifi_logs_to_delete(files, self.now), ["wifi-2026-03-01_10.log.gz"])

    def test_keeps_everything_within_limits(self):
        files = [("wifi-2026-03-01_10.log.gz", self.now - DAY, 200 * MB)]
        self.assertEqual(select_wifi_logs_to_delete(files, self.now), [])

    def test_never_selects_other_files(self):
        files = [
            ("device_1234.log", self.now - 90 * DAY, 500 * MB),
            ("wifi-notes.txt", self.now - 90 * DAY, 1),
            ("wifi-2026-01-01_10.log", self.now - 90 * DAY, 1),
        ]
        self.assertEqual(select_wifi_logs_to_delete(files, self.now), [])

    def test_prune_removes_only_selected_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("wifi-2026-01-01_10.log.gz", "wifi-2026-03-01_10.log.gz", "device_1234.log"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            old = self.now - 40 * DAY
            for name in ("wifi-2026-01-01_10.log.gz", "device_1234.log"):
                os.utime(os.path.join(d, name), (old, old))
            prune_wifi_logs(d, self.now)
            self.assertEqual(sorted(os.listdir(d)), ["device_1234.log", "wifi-2026-03-01_10.log.gz"])


class GzipAppendTests(unittest.TestCase):
    def test_appended_members_read_back_as_one_stream(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wifi-2026-03-07_09.log.gz")
            append_gzip_member(path, "=== first\nline a\n")
            append_gzip_member(path, "=== second\nline b\n")
            with gzip.open(path, "rt") as f:
                self.assertEqual(f.read(), "=== first\nline a\n=== second\nline b\n")


class FindIwTests(unittest.TestCase):
    def test_finds_iw_outside_path(self):
        with tempfile.TemporaryDirectory() as d:
            iw = os.path.join(d, "iw")
            with open(iw, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(iw, stat.S_IRWXU)
            with mock.patch.object(wifi_stats, "IW_SEARCH_DIRS", (d,)):
                self.assertEqual(find_iw(path_env=""), iw)

    def test_missing_iw(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(wifi_stats, "IW_SEARCH_DIRS", (d,)):
                self.assertIsNone(find_iw(path_env=""))


class RecorderBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inst_a = os.path.join(self.tmp.name, "a")
        self.inst_b = os.path.join(self.tmp.name, "b")
        self.rec = WifiStatsRecorder("/usr/sbin/iw")

    def read(self, inst, name):
        with gzip.open(os.path.join(inst, "logs", name), "rt") as f:
            return f.read()

    def test_buffers_until_flush(self):
        self.rec.add(self.inst_a, NOW, "s1\n")
        self.rec.add(self.inst_a, NOW, "s2\n")
        self.assertFalse(os.path.exists(os.path.join(self.inst_a, "logs")))
        self.rec.flush()
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_09.log.gz"), "s1\ns2\n")

    def test_writes_after_flush_interval(self):
        self.rec._last_flush -= wifi_stats.FLUSH_INTERVAL_SECS
        self.rec.add(self.inst_a, NOW, "s1\n")
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_09.log.gz"), "s1\n")

    def test_new_hour_flushes_to_previous_hour_file(self):
        self.rec.add(self.inst_a, NOW, "s1\n")
        self.rec.add(self.inst_a, NOW + timedelta(hours=1), "s2\n")
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_09.log.gz"), "s1\n")
        self.rec.flush()
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_10.log.gz"), "s2\n")

    def test_instance_change_flushes_to_previous_instance(self):
        self.rec.add(self.inst_a, NOW, "s1\n")
        self.rec.add(self.inst_b, NOW, "s2\n")
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_09.log.gz"), "s1\n")
        self.rec.stop()
        self.assertEqual(self.read(self.inst_b, "wifi-2026-03-07_09.log.gz"), "s2\n")

    def test_nothing_recorded_after_stop(self):
        self.rec.stop()
        self.rec.add(self.inst_a, NOW, "s1\n")
        self.rec.flush()
        self.assertFalse(os.path.exists(os.path.join(self.inst_a, "logs")))

    def test_write_failure_keeps_buffer_and_does_not_raise(self):
        self.rec.add(self.inst_a, NOW, "s1\n")
        with mock.patch.object(wifi_stats, "append_gzip_member", side_effect=OSError(28, "No space left")), \
                mock.patch("builtins.print") as printed:
            self.rec.flush()
            self.rec.flush()
        self.assertEqual(printed.call_count, 1)
        self.rec.flush()
        self.assertEqual(self.read(self.inst_a, "wifi-2026-03-07_09.log.gz"), "s1\n")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_layout(self):
        outputs = {
            ("dev",): IW_DEV,
            ("dev", "phy2-ap0", "info"): "Interface phy2-ap0",
            ("dev", "phy2-ap0", "station", "dump"): "Station 02:00:00:00:aa:01 (on phy2-ap0)\n",
            ("dev", "phy2-ap0", "survey", "dump"): "",
            ("dev", "phy1-ap0", "info"): "Interface phy1-ap0\n",
            ("dev", "phy1-ap0", "station", "dump"): "",
            ("dev", "phy1-ap0", "survey", "dump"): SURVEY_DUMP,
        }

        def fake_run_cmd(args, timeout=None):
            return 0, outputs[tuple(args[1:])]

        with tempfile.TemporaryDirectory() as d:
            leases = os.path.join(d, "dhcp.leases")
            with open(leases, "w") as f:
                f.write("1772870000 02:00:00:00:aa:01 192.0.2.10 station-1 *\n")
            with mock.patch.object(wifi_stats, "run_cmd", fake_run_cmd), \
                    mock.patch.object(wifi_stats, "DHCP_LEASES_FILE", leases), \
                    mock.patch.object(wifi_stats, "LOADAVG_FILE", os.path.join(d, "missing")):
                text = WifiStatsRecorder("/usr/sbin/iw").take_snapshot(NOW)

        headers = [line for line in text.splitlines() if line.startswith(("===", "---"))]
        self.assertEqual(headers, [
            "=== 2026-03-07T09:41:05Z",
            "--- phy2-ap0 info",
            "--- phy2-ap0 station dump",
            "--- phy2-ap0 survey dump (in use)",
            "--- phy1-ap0 info",
            "--- phy1-ap0 station dump",
            "--- phy1-ap0 survey dump (in use)",
            "--- dhcp leases",
            "--- load",
        ])
        self.assertIn("Station 02:00:00:00:aa:01 (on phy2-ap0)\n", text)
        self.assertIn("2437 MHz [in use]", text)
        self.assertNotIn("2412 MHz", text)
        self.assertIn("192.0.2.10", text)


if __name__ == "__main__":
    unittest.main()
