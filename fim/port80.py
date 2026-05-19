"""Dynamic port-80 redirect server when no instance is running."""
import threading
import time
from http.server import ThreadingHTTPServer
from fim.config import IS_ROOT, PORT_ALT
from fim.docker import any_instance_running

# ── Dynamic port 80 manager ─────────────────────────────────────────────────

_port80_server   = None
_port80_lock     = threading.Lock()
_port80_running  = False

def update_port80():
    from fim.web.handler import Handler
    if not IS_ROOT:
        return
    global _port80_server, _port80_running
    should_run = not any_instance_running()
    with _port80_lock:
        if should_run and _port80_server is None:
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", PORT_ALT), Handler)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                _port80_server  = srv
                _port80_running = True
                print(f"Port {PORT_ALT} listener started")
            except Exception as e:
                print(f"Could not start port {PORT_ALT}: {e}")
        elif not should_run and _port80_server is not None:
            _port80_server.shutdown()
            _port80_server  = None
            _port80_running = False
            print(f"Port {PORT_ALT} listener stopped")

def port80_monitor():
    while True:
        time.sleep(4)
        try:
            update_port80()
        except Exception as e:
            print(f"Port 80 monitor error: {e}")
