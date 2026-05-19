"""Action progress tracking."""
import json
import threading

# ── Progress state ──────────────────────────────────────────────────────────

action_lock   = threading.Lock()
_progress_lock = threading.Lock()
_progress = {
    "active": False,
    "stages": [],
    "log":    "",
    "_raw_log": "",
    "done":   True,
    "ok":     True,
}
MAX_RAW_LOG_CHARS = 600_000

def progress_reset(stages):
    with _progress_lock:
        _progress.update(
            active=True, done=False, ok=True, log="", _raw_log="",
            stages=[{"label": s, "status": "pending"} for s in stages],
        )

def _append_raw(text):
    if not text:
        return
    raw = _progress["_raw_log"] + text
    if len(raw) > MAX_RAW_LOG_CHARS:
        raw = raw[-MAX_RAW_LOG_CHARS:]
    _progress["_raw_log"] = raw
    _progress["log"] = raw

def progress_log_raw(chunk):
    """Append raw PTY output; the UI replays it through a terminal emulator."""
    if not chunk:
        return
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    with _progress_lock:
        _append_raw(chunk)

def progress_log_line(line):
    """Append a single logical log line."""
    if not line:
        return
    with _progress_lock:
        _append_raw(line.rstrip("\n") + "\n")

def progress_stage(idx, status, log_line=None):
    with _progress_lock:
        if idx < len(_progress["stages"]):
            _progress["stages"][idx]["status"] = status
    if log_line:
        progress_log_line(log_line)

def progress_done(ok=True):
    with _progress_lock:
        _progress.update(done=True, active=False, ok=ok)

def get_progress():
    with _progress_lock:
        data = json.loads(json.dumps(_progress))
        data.pop("_raw_log", None)
        return data
