"""Subprocess helpers with optional PTY streaming."""
import os
import select
import subprocess
import time
try:
    import pty
    HAS_PTY = True
except ImportError:
    HAS_PTY = False
from fim.progress import progress_log_line, progress_log_raw

def run_cmd(args, cwd=None, timeout=180):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "Command timed out\n"
    except Exception as e:
        return -1, str(e) + "\n"

def _pty_read_loop(master_fd, proc, deadline, on_chunk):
    """Read PTY master until process exits; call on_chunk(bytes) for each read."""
    output = bytearray()
    while True:
        if time.time() > deadline:
            proc.kill()
            on_chunk(b"\nCommand timed out\n")
            return -1, bytes(output)

        remaining = deadline - time.time()
        wait = min(0.25, max(0.05, remaining))
        try:
            ready, _, _ = select.select([master_fd], [], [], wait)
        except (ValueError, OSError):
            ready = []

        if ready:
            try:
                chunk = os.read(master_fd, 8192)
            except OSError:
                chunk = b""
            if chunk:
                output.extend(chunk)
                on_chunk(chunk)

        if proc.poll() is not None:
            break

    # Drain remaining output after process exit.
    for _ in range(50):
        try:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
        except (ValueError, OSError):
            break
        if not ready:
            break
        try:
            chunk = os.read(master_fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        output.extend(chunk)
        on_chunk(chunk)

    return proc.returncode, bytes(output)

def run_cmd_live(args, cwd=None, timeout=180, stage_idx=0):
    """Run a command under a PTY when available and stream raw output into the progress log."""
    def on_chunk(chunk):
        progress_log_raw(chunk.decode("utf-8", errors="replace"))

    deadline = time.time() + timeout
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")

    if HAS_PTY:
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                args, cwd=cwd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True, env=env,
            )
        except Exception as e:
            os.close(master_fd)
            os.close(slave_fd)
            progress_log_line(str(e))
            return -1, str(e) + "\n"
        os.close(slave_fd)
        try:
            return _pty_read_loop(master_fd, proc, deadline, on_chunk)
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            progress_log_line(str(e))
            return -1, str(e) + "\n"
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    # Fallback when PTY is unavailable (e.g. Windows).
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        output = ""
        for line in proc.stdout:
            output += line
            progress_log_line(line.rstrip("\n"))
            if time.time() > deadline:
                proc.kill()
                progress_log_line("Command timed out")
                return -1, output + "Command timed out\n"
        proc.wait(timeout=max(0, deadline - time.time()))
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        proc.kill()
        progress_log_line("Command timed out")
        return -1, output + "Command timed out\n"
    except Exception as e:
        progress_log_line(str(e))
        return -1, str(e) + "\n"
