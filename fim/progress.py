"""Action progress tracking and terminal output emulation."""
import json
import threading

# ── Progress state ──────────────────────────────────────────────────────────

action_lock   = threading.Lock()
_progress_lock = threading.Lock()
_progress = {
    "active": False,
    "stages": [],
    "log":    "",
    "_term_buf": [],
    "_term_cy": 0,
    "_term_cx": 0,
    "done":   True,
    "ok":     True,
}
MAX_PROGRESS_LOG_LINES = 4000

def progress_reset(stages):
    with _progress_lock:
        _progress.update(
            active=True, done=False, ok=True, log="",
            _term_buf=[], _term_cy=0, _term_cx=0,
            stages=[{"label": s, "status": "pending"} for s in stages],
        )

def _term_ensure_row(buf, row):
    while len(buf) <= row:
        buf.append("")

def _term_write(buf, cy, cx, ch):
    _term_ensure_row(buf, cy)
    line = buf[cy]
    if cx >= len(line):
        line = line + (" " * (cx - len(line))) + ch
    else:
        line = line[:cx] + ch + line[cx + 1:]
    buf[cy] = line

def _term_erase_line(buf, row):
    _term_ensure_row(buf, row)
    buf[row] = ""

def _term_sync_log():
    buf = _progress["_term_buf"]
    if len(buf) > MAX_PROGRESS_LOG_LINES:
        drop = len(buf) - MAX_PROGRESS_LOG_LINES
        buf[:] = buf[drop:]
        _progress["_term_cy"] = max(0, _progress["_term_cy"] - drop)
    _progress["log"] = "\n".join(buf)

def _term_parse_params(raw):
    nums = []
    for part in raw.split(";"):
        part = part.lstrip("?")
        if part.isdigit():
            nums.append(int(part))
    return nums

def _term_feed(text):
    """Minimal VT100 parser for docker compose progress redraws."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    buf = _progress["_term_buf"]
    cy = _progress["_term_cy"]
    cx = _progress["_term_cx"]
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\x1b":
            if i + 1 < len(text) and text[i + 1] == "[":
                i += 2
                raw = ""
                while i < len(text) and text[i] in "0123456789;?":
                    raw += text[i]
                    i += 1
                if i >= len(text):
                    break
                cmd = text[i]
                i += 1
                nums = _term_parse_params(raw)
                n = nums[0] if nums else 1
                if cmd == "A":  # cursor up
                    cy = max(0, cy - (n if n else 1))
                elif cmd == "B":  # cursor down
                    cy += n if n else 1
                    _term_ensure_row(buf, cy)
                elif cmd in "G":  # cursor horizontal absolute
                    cx = max(0, (n - 1) if n else 0)
                elif cmd in "Hf":  # cursor position / home
                    row = (nums[0] - 1) if len(nums) >= 1 and nums[0] else 0
                    col = (nums[1] - 1) if len(nums) >= 2 and nums[1] else 0
                    cy, cx = max(0, row), max(0, col)
                    _term_ensure_row(buf, cy)
                elif cmd == "K":  # erase in line
                    _term_erase_line(buf, cy)
                elif cmd == "J":  # erase in display
                    if n == 2:
                        buf.clear()
                        cy, cx = 0, 0
                    elif n == 1:
                        del buf[cy + 1:]
                    else:
                        del buf[cy:]
                        _term_ensure_row(buf, cy)
                # else: ignore (e.g. ?25l/h cursor visibility)
            elif i + 1 < len(text) and text[i + 1] == "]":
                i += 2
                while i < len(text) and text[i] != "\x07":
                    if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "\\":
                        i += 2
                        break
                    i += 1
                if i < len(text) and text[i] == "\x07":
                    i += 1
            else:
                i += 1
        elif c == "\r":
            if i + 1 < len(text) and text[i + 1] == "\n":
                cy += 1
                cx = 0
                _term_ensure_row(buf, cy)
                i += 2
            else:
                cx = 0
                i += 1
        elif c == "\n":
            cy += 1
            cx = 0
            _term_ensure_row(buf, cy)
            i += 1
        elif c == "\b":
            cx = max(0, cx - 1)
            i += 1
        elif c == "\x07":
            i += 1
        elif c.isprintable() or c in "\t":
            _term_write(buf, cy, cx, c)
            cx += 1
            i += 1
        else:
            i += 1
    _progress["_term_buf"] = buf
    _progress["_term_cy"] = cy
    _progress["_term_cx"] = cx
    _term_sync_log()

def progress_log_raw(chunk):
    """Feed raw PTY output through the terminal emulator."""
    if not chunk:
        return
    with _progress_lock:
        _term_feed(chunk)

def progress_log_line(line):
    """Append a single logical log line."""
    if not line:
        return
    with _progress_lock:
        buf = _progress["_term_buf"]
        buf.append(line.rstrip("\n"))
        _progress["_term_cy"] = len(buf) - 1
        _progress["_term_cx"] = len(buf[-1]) if buf else 0
        _term_sync_log()

def progress_stage(idx, status, log_line=None):
    with _progress_lock:
        if idx < len(_progress["stages"]):
            _progress["stages"][idx]["status"] = status
    if log_line:
        progress_log_line(log_line)

def progress_done(ok=True):
    with _progress_lock:
        _term_sync_log()
        _progress.update(done=True, active=False, ok=ok)

def get_progress():
    with _progress_lock:
        data = json.loads(json.dumps(_progress))
        for k in ("_term_buf", "_term_cy", "_term_cx"):
            data.pop(k, None)
        return data
