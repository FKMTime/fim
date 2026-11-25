import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import fim.instances_mgmt as instances_mgmt

REPO_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
DEMO_MOUNT = "./nginx/demo:/etc/nginx/demo:ro"
DEMO_CONF = os.path.join("nginx", "demo", "dummy-wca.conf")


def service_block(compose_path, service):
    """Return the lines of one top-level service in a docker-compose.yml."""
    with open(compose_path) as f:
        lines = f.read().splitlines()
    start = lines.index(f"  {service}:")
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^ {0,2}\S", line):
            break
        block.append(line)
    return "\n".join(block)


class DevTemplateDummyWcaRoutesTests(unittest.TestCase):
    def test_dev_template_ships_the_dummy_wca_routes(self):
        with open(os.path.join(REPO_TEMPLATES, "dev", DEMO_CONF)) as f:
            conf = f.read()
        self.assertIn("location /dummy-wca {", conf)
        self.assertIn("location /dummy-wca-live {", conf)

    def test_dev_nginx_mounts_the_demo_routes(self):
        nginx = service_block(os.path.join(REPO_TEMPLATES, "dev", "docker-compose.yml"), "nginx")
        self.assertIn(f"- {DEMO_MOUNT}", nginx)

    def test_prod_template_has_no_dummy_wca_routes(self):
        compose = os.path.join(REPO_TEMPLATES, "prod", "docker-compose.yml")
        self.assertNotIn("/etc/nginx/demo", service_block(compose, "nginx"))
        self.assertFalse(os.path.exists(os.path.join(REPO_TEMPLATES, "prod", "nginx")))


class DevInstanceDummyWcaRoutesTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.instances_dir = os.path.join(self.root, "instances")
        os.makedirs(self.instances_dir)
        patches = [
            mock.patch.object(instances_mgmt, "INSTANCES_DIR", self.instances_dir),
            mock.patch.object(instances_mgmt, "get_instances", return_value={}),
            mock.patch.object(
                instances_mgmt, "get_templates",
                return_value={"dev": os.path.join(REPO_TEMPLATES, "dev")},
            ),
            mock.patch.object(instances_mgmt, "refresh_instances"),
            mock.patch.object(instances_mgmt, "get_selected", return_value="other"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def create_dev_instance(self):
        ok, err = instances_mgmt.create_instance("alpha", "dev")
        self.assertTrue(ok, err)
        return os.path.join(self.instances_dir, "alpha")

    def test_created_instance_gets_the_routes(self):
        instance = self.create_dev_instance()
        with open(os.path.join(REPO_TEMPLATES, "dev", DEMO_CONF)) as f:
            expected = f.read()
        with open(os.path.join(instance, DEMO_CONF)) as f:
            self.assertEqual(f.read(), expected)

    def test_purge_keeps_the_routes(self):
        # "Down + delete volumes" purges instance directories the template
        # lacks; the routes must survive it, while data directories still go.
        instance = self.create_dev_instance()
        os.makedirs(os.path.join(instance, "db"))
        removed = instances_mgmt.purge_extra_dirs(instance, os.path.join(REPO_TEMPLATES, "dev"))
        self.assertEqual(removed, ["db"])
        self.assertTrue(os.path.isfile(os.path.join(instance, DEMO_CONF)))


if __name__ == "__main__":
    unittest.main()
