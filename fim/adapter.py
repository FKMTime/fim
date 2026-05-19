"""mdns-docker-adapter manager (Apple Silicon)."""
import os
import subprocess
import threading
import urllib.request
from fim.config import DATA_DIR, IS_APPLE_SILICON

# ── Adapter manager (Apple Silicon only) ────────────────────────────────────

ADAPTER_ENABLED_FILE = os.path.join(DATA_DIR, ".adapter_enabled")
ADAPTER_BIN          = os.path.join(DATA_DIR, "docker-adapter")
ADAPTER_DOWNLOAD_URL = (
    "https://github.com/filipton/docker-adapter/releases/latest"
    "/download/docker-adapter-apple-darwin"
)
ADAPTER_LOG_MAX = 300

_adapter_lock   = threading.Lock()
_adapter_proc   = None
_adapter_stop   = threading.Event()
_adapter_thread = None
_adapter_state  = {
    "enabled":     False,
    "running":     False,
    "downloading": False,
    "pid":         None,
    "log":         [],
    "error":       "",
}

def _adapter_log(line):
    with _adapter_lock:
        _adapter_state["log"].append(line)
        if len(_adapter_state["log"]) > ADAPTER_LOG_MAX:
            _adapter_state["log"] = _adapter_state["log"][-ADAPTER_LOG_MAX:]

def is_adapter_enabled():
    return os.path.isfile(ADAPTER_ENABLED_FILE)

def set_adapter_enabled_flag(enabled):
    if enabled:
        with open(ADAPTER_ENABLED_FILE, "w") as f:
            f.write("1")
    else:
        try:
            os.unlink(ADAPTER_ENABLED_FILE)
        except FileNotFoundError:
            pass

def _download_adapter():
    _adapter_log("Downloading mdns-docker-adapter from GitHub…")
    try:
        tmp = ADAPTER_BIN + ".tmp"
        urllib.request.urlretrieve(ADAPTER_DOWNLOAD_URL, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, ADAPTER_BIN)
        _adapter_log("Download complete.")
        return True
    except Exception as e:
        _adapter_log(f"Download failed: {e}")
        return False

def _adapter_manager():
    global _adapter_proc
    if not os.path.isfile(ADAPTER_BIN):
        with _adapter_lock:
            _adapter_state["downloading"] = True
        ok = _download_adapter()
        with _adapter_lock:
            _adapter_state["downloading"] = False
        if not ok:
            with _adapter_lock:
                _adapter_state["error"] = "Binary download failed"
            return

    while not _adapter_stop.is_set():
        try:
            _adapter_log("Starting mdns-docker-adapter…")
            proc = subprocess.Popen(
                [ADAPTER_BIN],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            with _adapter_lock:
                _adapter_proc             = proc
                _adapter_state["running"] = True
                _adapter_state["pid"]     = proc.pid
                _adapter_state["error"]   = ""

            for line in proc.stdout:
                _adapter_log(line.rstrip("\n"))
                if _adapter_stop.is_set():
                    break

            proc.wait()
            exit_code = proc.returncode
            with _adapter_lock:
                _adapter_state["running"] = False
                _adapter_state["pid"]     = None
                _adapter_proc             = None

            if _adapter_stop.is_set():
                break

            _adapter_log(f"Adapter exited (code {exit_code}), restarting in 3 s…")
            _adapter_stop.wait(3)

        except Exception as e:
            _adapter_log(f"Adapter error: {e}")
            with _adapter_lock:
                _adapter_state["running"] = False
                _adapter_state["pid"]     = None
                _adapter_proc             = None
            if _adapter_stop.is_set():
                break
            _adapter_stop.wait(3)

def start_adapter():
    global _adapter_thread
    if not IS_APPLE_SILICON:
        return
    if _adapter_thread and _adapter_thread.is_alive():
        return
    _adapter_stop.clear()
    with _adapter_lock:
        _adapter_state["enabled"] = True
        _adapter_state["log"]     = []
        _adapter_state["error"]   = ""
    _adapter_thread = threading.Thread(target=_adapter_manager, daemon=True)
    _adapter_thread.start()

def stop_adapter():
    if not IS_APPLE_SILICON:
        return
    _adapter_stop.set()
    with _adapter_lock:
        _adapter_state["enabled"] = False
        proc = _adapter_proc
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    with _adapter_lock:
        _adapter_state["running"] = False
        _adapter_state["pid"]     = None

def get_adapter_state():
    with _adapter_lock:
        return {
            "enabled":     _adapter_state["enabled"],
            "running":     _adapter_state["running"],
            "downloading": _adapter_state["downloading"],
            "pid":         _adapter_state["pid"],
            "log":         "\n".join(_adapter_state["log"]),
            "error":       _adapter_state["error"],
        }
