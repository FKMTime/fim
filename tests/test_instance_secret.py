import os
import shutil
import tempfile
import unittest
from unittest import mock

import fim.instances_mgmt as instances_mgmt

REPO_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


def read_env(path):
    values = {}
    with open(path) as f:
        for line in f:
            key, sep, value = line.rstrip("\n").partition("=")
            if sep and not key.startswith("#"):
                values[key] = value
    return values


class CreateInstanceSecretTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.instances_dir = os.path.join(self.root, "instances")
        self.templates_dir = os.path.join(self.root, "templates")
        os.makedirs(self.instances_dir)
        os.makedirs(self.templates_dir)
        patches = [
            mock.patch.object(instances_mgmt, "INSTANCES_DIR", self.instances_dir),
            mock.patch.object(instances_mgmt, "get_instances", return_value={}),
            mock.patch.object(instances_mgmt, "get_templates", side_effect=self.templates),
            mock.patch.object(instances_mgmt, "refresh_instances"),
            mock.patch.object(instances_mgmt, "get_selected", return_value="other"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def templates(self):
        return {
            name: os.path.join(self.templates_dir, name)
            for name in os.listdir(self.templates_dir)
        }

    def add_template(self, name, env_text=None, files=None):
        path = os.path.join(self.templates_dir, name)
        os.makedirs(path)
        if env_text is not None:
            with open(os.path.join(path, ".env.template"), "w") as f:
                f.write(env_text)
        for filename, text in (files or {}).items():
            with open(os.path.join(path, filename), "w") as f:
                f.write(text)
        return path

    def create(self, template):
        ok, err = instances_mgmt.create_instance("alpha", template)
        self.assertTrue(ok, err)
        return os.path.join(self.instances_dir, "alpha", ".env")

    def assert_generated(self, secret):
        self.assertEqual(len(secret), 48)
        self.assertTrue(secret.isalnum())

    def test_shipped_templates_get_a_generated_secret(self):
        for name in ("dev", "prod"):
            with self.subTest(template=name):
                shutil.copytree(os.path.join(REPO_TEMPLATES, name), os.path.join(self.templates_dir, name))
                env_path = self.create(name)
                self.assert_generated(read_env(env_path)["SECRET"])
                shutil.rmtree(os.path.dirname(env_path))

    def test_legacy_example_secret_is_replaced(self):
        self.add_template("old", "TOKEN=1234\nSECRET=example-secret\nREDIRECT_SSL=true\n")
        env = read_env(self.create("old"))
        self.assert_generated(env["SECRET"])
        self.assertEqual(env["TOKEN"], "1234")
        self.assertEqual(env["REDIRECT_SSL"], "true")

    def test_missing_secret_is_appended(self):
        self.add_template("bare", "TOKEN=1234")
        env = read_env(self.create("bare"))
        self.assertEqual(env["TOKEN"], "1234")
        self.assert_generated(env["SECRET"])

    def test_other_secret_keys_are_untouched(self):
        self.add_template("wca", "SECRET=\nWCA_CLIENT_SECRET=example-secret\n")
        env = read_env(self.create("wca"))
        self.assert_generated(env["SECRET"])
        self.assertEqual(env["WCA_CLIENT_SECRET"], "example-secret")

    def test_real_secret_in_template_is_kept(self):
        real = "invented-test-secret-0123456789-abcdef"
        self.add_template("custom", f"TOKEN=1234\nSECRET={real}\n")
        env = read_env(self.create("custom"))
        self.assertEqual(env["SECRET"], real)

    def test_each_instance_gets_its_own_secret(self):
        self.add_template("dev", "SECRET=\n")
        first = read_env(self.create("dev"))["SECRET"]
        shutil.rmtree(os.path.join(self.instances_dir, "alpha"))
        second = read_env(self.create("dev"))["SECRET"]
        self.assertNotEqual(first, second)

    def test_env_shipped_with_template_is_not_modified(self):
        # A template that already carries a .env is copied as-is: create_instance
        # only generates a secret for a .env it produced from .env.template.
        existing = "TOKEN=1234\nSECRET=example-secret\n"
        self.add_template("prefilled", "SECRET=\n", files={".env": existing})
        with open(self.create("prefilled")) as f:
            self.assertEqual(f.read(), existing)

    def test_existing_instance_env_is_not_modified(self):
        self.add_template("dev", "SECRET=\n")
        instance_dir = os.path.join(self.instances_dir, "alpha")
        os.makedirs(instance_dir)
        env_path = os.path.join(instance_dir, ".env")
        existing = "TOKEN=1234\nSECRET=example-secret\n"
        with open(env_path, "w") as f:
            f.write(existing)
        with mock.patch.object(instances_mgmt, "get_instances", return_value={"alpha": instance_dir}):
            ok, err = instances_mgmt.create_instance("alpha", "dev")
        self.assertFalse(ok)
        self.assertEqual(err, "Instance already exists")
        with open(env_path) as f:
            self.assertEqual(f.read(), existing)


if __name__ == "__main__":
    unittest.main()
