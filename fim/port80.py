"""Dynamic port-80 redirect server when no instance is up."""
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from fim.config import IS_ROOT, PORT_ALT
from fim.docker import any_instance_up

# ── Dynamic port 80 manager ─────────────────────────────────────────────────

_port80_server    = None
_port80_lock      = threading.Lock()
_port80_running   = False
_port80_suspended = 0  # >0 while an instance is starting and needs port 80

def _stop_port80_locked():
    global _port80_server, _port80_running
    if _port80_server is None:
        return
    _port80_server.shutdown()
    _port80_server.server_close()
    _port80_server  = None
    _port80_running = False
    print(f"Port {PORT_ALT} listener stopped")

def update_port80():
    from fim.web.handler import Handler
    if not IS_ROOT:
        return
    global _port80_server, _port80_running
    should_run = not any_instance_up()
    with _port80_lock:
        if _port80_suspended:
            return
        if should_run and _port80_server is None:
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", PORT_ALT), Handler)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                _port80_server  = srv
                _port80_running = True
                print(f"Port {PORT_ALT} listener started")
            except Exception as e:
                print(f"Could not start port {PORT_ALT}: {e}")
        elif not should_run:
            _stop_port80_locked()

@contextmanager
def port80_released():
    """Free port 80 for an instance start; the monitor won't retake it until exit."""
    global _port80_suspended
    with _port80_lock:
        _port80_suspended += 1
        _stop_port80_locked()
    try:
        yield
    finally:
        with _port80_lock:
            _port80_suspended -= 1

def port80_monitor():
    while True:
        time.sleep(4)
        try:
            update_port80()
        except Exception as e:
            print(f"Port 80 monitor error: {e}")
