import unittest

import fim.openwrt_auth as openwrt_auth
from fim.auth import change_password, validate_new_password
from fim.openwrt_auth import parse_shadow_hash, verify_linux_password


class ShadowParsingTests(unittest.TestCase):
    def test_parse_root_hash(self):
        shadow = "root:$6$rounds=5000$saltsalt$hashvalue:18000:0:99999:7:::\n"
        self.assertEqual(parse_shadow_hash(shadow), "$6$rounds=5000$saltsalt$hashvalue")

    def test_missing_user(self):
        self.assertIsNone(parse_shadow_hash("nobody:x:0:0:::\n", "root"))


class PasswordValidationTests(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            validate_new_password("")

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            validate_new_password("a" * 129)

    def test_accepts_normal_password(self):
        self.assertEqual(validate_new_password("secret123"), "secret123")


@unittest.skipUnless(openwrt_auth.crypt is not None, "crypt module unavailable")
class LinuxPasswordVerifyTests(unittest.TestCase):
    def test_verify_matching_password(self):
        shadow_hash = openwrt_auth.crypt.crypt("router-pass", openwrt_auth.crypt.mksalt())
        self.assertTrue(verify_linux_password("router-pass", shadow_hash))
        self.assertFalse(verify_linux_password("wrong", shadow_hash))


class LocalAuthChangeTests(unittest.TestCase):
    def test_change_password_wrong_current(self):
        ok, err = change_password("root", "wrong", "newpass")
        self.assertFalse(ok)
        self.assertIn("incorrect", err.lower())


if __name__ == "__main__":
    unittest.main()
