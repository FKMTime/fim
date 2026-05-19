"""FKMTime Instance Manager entry point."""
import os
import threading
import time
from http.server import ThreadingHTTPServer

from fim.adapter import is_adapter_enabled, start_adapter
from fim.auth import load_auth
from fim.config import (
    DATA_DIR,
    INSTANCES_DIR,
    IS_APPLE_SILICON,
    IS_MACOS,
    IS_OPENWRT,
    IS_ROOT,
    LOCK_FILE,
    PORT_MAIN,
)
from fim import instances as instances_mod
from fim.instances import get_instances, get_selected, get_templates, refresh_instances, set_selected
from fim.port80 import port80_monitor, update_port80
from fim.web.handler import Handler


def main():
    os.makedirs(INSTANCES_DIR, exist_ok=True)
    refresh_instances()
    if not os.path.exists(LOCK_FILE) and instances_mod._instances:
        set_selected(next(iter(instances_mod._instances)))
    load_auth()

    main_server = ThreadingHTTPServer(("0.0.0.0", PORT_MAIN), Handler)
    threading.Thread(target=main_server.serve_forever, daemon=True).start()
    print(f"FKMTime Instance Manager running on http://0.0.0.0:{PORT_MAIN}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Instances directory: {INSTANCES_DIR}")
    print(f"Templates: {list(get_templates().keys())}")
    print(f"Selected instance: {get_selected() or 'none'}")
    print(f"OpenWrt detected: {IS_OPENWRT}")
    print(f"Running as root: {IS_ROOT}")
    if not IS_ROOT:
        print("Port 80 listener disabled (not running as root)")
    if IS_APPLE_SILICON:
        if is_adapter_enabled():
            print("Starting mdns-docker-adapter (enabled)…")
            start_adapter()
        else:
            print("\033[33mTip: open the Adapter tab in the web UI to enable mdns-docker-adapter\033[0m")
            print("\033[33m  (Bluetooth and mDNS support for Docker containers)\033[0m")
    elif IS_MACOS:
        print("\033[33mTip: on macOS you can run mdns-docker-adapter for Bluetooth and mDNS support:\033[0m")
        print("\033[33m  https://github.com/filipton/docker-adapter/releases/latest\033[0m")

    threading.Thread(target=port80_monitor, daemon=True).start()
    update_port80()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
        main_server.shutdown()


if __name__ == "__main__":
    main()
