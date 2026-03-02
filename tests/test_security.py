import unittest

from manager import sanitize_wifi_value


class WifiSanitizationTests(unittest.TestCase):
    def test_accepts_valid_values(self):
        self.assertEqual(sanitize_wifi_value("MySSID-01", "hs_ssid", max_len=32), "MySSID-01")
        self.assertEqual(sanitize_wifi_value("secretpass123", "hs_psk", max_len=63), "secretpass123")

    def test_rejects_disallowed_chars(self):
        for bad in ("ssid=evil", "bad\nssid", "bad\rssid", "bad;ssid", "bad&ssid", "bad|ssid", "bad`ssid",
                    "bad$ssid", "bad(ssid", "bad)ssid", "bad<ssid", "bad>ssid", "bad\\ssid", "bad\x00ssid",
                    "bad'ssid", 'bad"ssid'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sanitize_wifi_value(bad, "hs_ssid", max_len=32)

    def test_rejects_invalid_type_and_length(self):
        with self.assertRaises(ValueError):
            sanitize_wifi_value(123, "hs_ssid", max_len=32)
        with self.assertRaises(ValueError):
            sanitize_wifi_value("a" * 33, "hs_ssid", max_len=32)


if __name__ == "__main__":
    unittest.main()
