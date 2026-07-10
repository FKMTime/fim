import unittest
from unittest import mock

import fim.openwrt_auth as openwrt_auth
from fim.auth import (
    change_password,
    create_session,
    get_session_username,
    validate_new_password,
    validate_session,
)
from fim.openwrt_auth import (
    find_rpcd_login,
    list_rpcd_usernames,
    parse_rpcd_logins,
    parse_shadow_hash,
    verify_linux_password,
    verify_rpcd_password,
)


class ShadowParsingTests(unittest.TestCase):
    def test_parse_root_hash(self):
        shadow = "root:$6$rounds=5000$saltsalt$hashvalue:18000:0:99999:7:::\n"
        self.assertEqual(parse_shadow_hash(shadow, "root"), "$6$rounds=5000$saltsalt$hashvalue")

    def test_missing_user(self):
        self.assertIsNone(parse_shadow_hash("nobody:x:0:0:::\n", "root"))


class RpcdParsingTests(unittest.TestCase):
    UCI = "\n".join([
        "rpcd.@login[0]=login",
        "rpcd.@login[0].username='root'",
        "rpcd.@login[0].password='$p$root'",
        "rpcd.@login[1]=login",
        "rpcd.@login[1].username='admin'",
        "rpcd.@login[1].password='$1$salt$hash'",
    ])

    def test_parse_login_sections(self):
        logins = parse_rpcd_logins(self.UCI)
        self.assertEqual(set(logins.keys()), {"@login[0]", "@login[1]"})
        self.assertEqual(logins["@login[0]"]["username"], "root")
        self.assertEqual(logins["@login[1]"]["password"], "$1$salt$hash")

    def test_list_usernames(self):
        self.assertEqual(list_rpcd_usernames(self.UCI), ["root", "admin"])

    def test_find_login(self):
        section, opts = find_rpcd_login(self.UCI, "admin")
        self.assertEqual(section, "@login[1]")
        self.assertEqual(opts["password"], "$1$salt$hash")


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

    def test_verify_rpcd_shadow_reference(self):
        shadow_hash = openwrt_auth.crypt.crypt("router-pass", openwrt_auth.crypt.mksalt())
        with mock.patch(
            "fim.openwrt_auth.read_shadow_hash",
            return_value=shadow_hash,
        ):
            self.assertTrue(verify_rpcd_password("$p$root", "router-pass"))


class SessionTests(unittest.TestCase):
    def test_session_stores_username(self):
        token = create_session("admin")
        self.assertTrue(validate_session(token))
        self.assertEqual(get_session_username(token), "admin")


class LocalAuthChangeTests(unittest.TestCase):
    def test_change_password_wrong_current(self):
        ok, err = change_password("root", "wrong", "newpass")
        self.assertFalse(ok)
        self.assertIn("incorrect", err.lower())


if __name__ == "__main__":
    unittest.main()
