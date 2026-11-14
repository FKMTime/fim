"""Record the hotspot's per-station Wi-Fi stats next to the running instance's logs.

The router keeps signal, bitrate and retry counters for each station only in
RAM, so after a competition they are gone. While an instance runs on OpenWrt,
a snapshot is taken every minute into memory and appended every ten minutes
to <instance>/logs/wifi-YYYY-MM-DD_HH.log.gz (UTC hour). Writing in rare
batches matters: the router's SD card stalls under frequent small writes.

Each flush appends one gzip member; concatenated members read back with zcat.
"""
import gzip
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from fim.commands import run_cmd
from fim.config import IS_OPENWRT
from fim.docker import instance_has_up_container
from fim.instances import get_instances, get_selected

SNAPSHOT_INTERVAL_SECS = 60
FLUSH_INTERVAL_SECS = 600
KEEP_DAYS = 30
MAX_TOTAL_BYTES = 200 * 1024 * 1024
# Upper bound on unwritten snapshots kept in RAM when the disk refuses writes.
MAX_BUFFER_BYTES = 8 * 1024 * 1024
CMD_TIMEOUT_SECS = 5

DHCP_LEASES_FILE = "/tmp/dhcp.leases"
LOADAVG_FILE = "/proc/loadavg"
# procd's PATH may lack the sbin directories, where OpenWrt installs iw.
IW_SEARCH_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")

WIFI_LOG_RE = re.compile(r"^wifi-\d{4}-\d{2}-\d{2}_\d{2}\.log\.gz$")


# ── Pure helpers ────────────────────────────────────────────────────────────

def parse_ap_interfaces(iw_dev_output):
    """Return the interfaces that `iw dev` reports in AP mode, in output order."""
    interfaces = []
    current = None
    for line in iw_dev_output.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "Interface":
            current = words[1]
        elif words[:2] == ["type", "AP"] and current and current not in interfaces:
            interfaces.append(current)
    return interfaces


def select_in_use_survey(survey_dump_output):
    """Keep only the survey block of the channel the interface is using."""
    blocks, block = [], []
    for line in survey_dump_output.splitlines():
        if line.startswith("Survey data from") and block:
            blocks.append(block)
            block = []
        block.append(line)
    if block:
        blocks.append(block)
    in_use = [b for b in blocks if any("[in use]" in line for line in b)]
    return "".join(line + "\n" for b in in_use for line in b)


def snapshot_header(now):
    return f"=== {now.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n"


def wifi_log_name(now):
    """File name for snapshots taken in the UTC hour of `now`."""
    return f"wifi-{now.astimezone(timezone.utc):%Y-%m-%d_%H}.log.gz"


def select_wifi_logs_to_delete(files, now_ts, keep_days=KEEP_DAYS, max_total_bytes=MAX_TOTAL_BYTES):
    """Pick wifi logs to delete from [(name, mtime, size)].

    Files older than `keep_days` go first; then the oldest remaining ones
    until the rest fits in `max_total_bytes`. Names that are not wifi logs
    are never selected.
    """
    ours = sorted((f for f in files if WIFI_LOG_RE.match(f[0])), key=lambda f: (f[1], f[0]))
    cutoff = now_ts - keep_days * 86400
    doomed = [f for f in ours if f[1] < cutoff]
    kept = [f for f in ours if f[1] >= cutoff]
    total = sum(f[2] for f in kept)
    while kept and total > max_total_bytes:
        oldest = kept.pop(0)
        total -= oldest[2]
        doomed.append(oldest)
    return [f[0] for f in doomed]


def append_gzip_member(path, data):
    """Append `data` as one gzip member; on failure, cut the file back to its old size."""
    payload = gzip.compress(data.encode("utf-8"))
    with open(path, "ab") as f:
        start = f.tell()
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        except OSError:
            # A half-written member would make zcat fail on the whole file.
            try:
                f.truncate(start)
            except OSError:
                pass
            raise


def prune_wifi_logs(logs_dir, now_ts):
    files = []
    for name in os.listdir(logs_dir):
        if not WIFI_LOG_RE.match(name):
            continue
        try:
            st = os.stat(os.path.join(logs_dir, name))
        except OSError:
            continue
        files.append((name, st.st_mtime, st.st_size))
    for name in select_wifi_logs_to_delete(files, now_ts):
        os.unlink(os.path.join(logs_dir, name))


def find_iw(path_env=None):
    """Locate `iw`, also in the sbin directories a service PATH may lack."""
    dirs = (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep)
    dirs += [d for d in IW_SEARCH_DIRS if d not in dirs]
    return shutil.which("iw", path=os.pathsep.join(d for d in dirs if d))


# ── Recorder ────────────────────────────────────────────────────────────────

def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        return f"({e})\n"


def _with_newline(text):
    return text if not text or text.endswith("\n") else text + "\n"


class WifiStatsRecorder:
    """Buffers snapshots for one instance at a time and flushes them in batches."""

    def __init__(self, iw_path):
        self.iw_path = iw_path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._warned = set()
        self._stopped = False
        self._instance_dir = None
        self._hour_name = None
        self._chunks = []
        self._buffered_bytes = 0
        self._last_flush = time.monotonic()

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            print(f"Wi-Fi stats: {message}")

    def _iw(self, *args):
        code, out = run_cmd([self.iw_path, *args], timeout=CMD_TIMEOUT_SECS)
        if code != 0:
            self._warn_once(("iw",) + args[:1] + args[2:], f"`iw {' '.join(args)}` failed: {out.strip()}")
        return out

    def take_snapshot(self, now):
        parts = [snapshot_header(now)]
        for ifc in parse_ap_interfaces(self._iw("dev")):
            parts.append(f"--- {ifc} info\n")
            parts.append(_with_newline(self._iw("dev", ifc, "info")))
            parts.append(f"--- {ifc} station dump\n")
            parts.append(_with_newline(self._iw("dev", ifc, "station", "dump")))
            parts.append(f"--- {ifc} survey dump (in use)\n")
            parts.append(select_in_use_survey(self._iw("dev", ifc, "survey", "dump")))
        parts.append("--- dhcp leases\n")
        parts.append(_with_newline(_read_file(DHCP_LEASES_FILE)))
        parts.append("--- load\n")
        parts.append(_with_newline(_read_file(LOADAVG_FILE)))
        return "".join(parts)

    def running_instance_dir(self):
        """Directory of the instance that is up, preferring the selected one."""
        insts = get_instances()
        selected = get_selected()
        names = ([selected] if selected in insts else []) + [n for n in insts if n != selected]
        for name in names:
            if instance_has_up_container(name):
                return insts[name]
        return None

    def add(self, instance_dir, now, text):
        """Buffer a snapshot, flushing first when the instance or the UTC hour changed."""
        with self._lock:
            if self._stopped:
                return
            hour_name = wifi_log_name(now)
            if self._chunks and (instance_dir != self._instance_dir or hour_name != self._hour_name):
                self._flush_locked()
            self._instance_dir = instance_dir
            self._hour_name = hour_name
            self._chunks.append(text)
            self._buffered_bytes += len(text)
            while self._buffered_bytes > MAX_BUFFER_BYTES and len(self._chunks) > 1:
                self._buffered_bytes -= len(self._chunks.pop(0))
            if time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECS:
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        self._last_flush = time.monotonic()
        if not self._chunks or not self._instance_dir:
            return
        logs_dir = os.path.join(self._instance_dir, "logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
            append_gzip_member(os.path.join(logs_dir, self._hour_name), "".join(self._chunks))
        except OSError as e:
            # Keep the buffer (capped in add) and retry on the next flush.
            self._warn_once("write", f"cannot write to {logs_dir}: {e}")
            return
        self._chunks = []
        self._buffered_bytes = 0
        try:
            prune_wifi_logs(logs_dir, time.time())
        except OSError as e:
            self._warn_once("prune", f"cannot prune old files in {logs_dir}: {e}")

    def run(self):
        while not self._stop.is_set():
            try:
                instance_dir = self.running_instance_dir()
                if instance_dir:
                    now = datetime.now(timezone.utc)
                    self.add(instance_dir, now, self.take_snapshot(now))
                else:
                    self.flush()  # the instance stopped: write what it collected
            except Exception as e:
                self._warn_once(("loop", type(e).__name__), f"snapshot failed: {e}")
            self._stop.wait(SNAPSHOT_INTERVAL_SECS)

    def stop(self):
        """Write the buffer and stop recording; safe to call from another thread."""
        self._stop.set()
        with self._lock:
            self._flush_locked()
            self._stopped = True


_recorder = None


def start_wifi_stats():
    """Start recording on OpenWrt when `iw` is available; otherwise do nothing."""
    global _recorder
    if not IS_OPENWRT or _recorder is not None:
        return
    iw_path = find_iw()
    if not iw_path:
        print("Wi-Fi stats: `iw` not found, not recording station stats")
        return
    _recorder = WifiStatsRecorder(iw_path)
    threading.Thread(target=_recorder.run, name="wifi-stats", daemon=True).start()
    print(f"Wi-Fi stats: recording every {SNAPSHOT_INTERVAL_SECS}s into <instance>/logs/wifi-*.log.gz")


def stop_wifi_stats():
    if _recorder is not None:
        try:
            _recorder.stop()
        except Exception as e:
            print(f"Wi-Fi stats: final write failed: {e}")
