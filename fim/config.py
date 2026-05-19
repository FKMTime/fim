"""Paths and platform flags."""
import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.environ.get("FIM_DATA_DIR", PROJECT_DIR)
INSTANCES_DIR = os.path.join(DATA_DIR, "instances")
TEMPLATES_DIR = os.path.join(PROJECT_DIR, "templates")
LOCK_FILE = os.path.join(DATA_DIR, ".instance_selected")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
PORT_MAIN = 8181
PORT_ALT = 80
IS_OPENWRT = os.path.isfile("/etc/openwrt_release")
IS_ROOT = os.geteuid() == 0
IS_MACOS = sys.platform == "darwin"
IS_APPLE_SILICON = IS_MACOS
