"""Docker compose status helpers."""
import json
import os
import shutil
from fim.commands import run_cmd, run_cmd_live
from fim.config import DOCKER_COMPOSE_TIMEOUT, IS_ROOT
from fim.instances import get_instances


def run_compose_live(args, cwd=None, stage_idx=0):
    """Run docker compose with no hard timeout by default."""
    return run_cmd_live(args, cwd=cwd, timeout=DOCKER_COMPOSE_TIMEOUT, stage_idx=stage_idx)

def sanitize_wifi_value(value, field, max_len=64):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}")
    if len(value) > max_len:
        raise ValueError(f"{field} too long")
    if any(c in value for c in {"\x00", "\n", "\r", "=", ";", "&", "|", "$", "`", "(", ")", "<", ">", '"', "'", "\\"}):
        raise ValueError(f"Invalid {field}")
    return value

def force_rmtree(path):
    """Remove a directory tree, falling back to docker if not running as root."""
    try:
        shutil.rmtree(path)
    except PermissionError:
        if IS_ROOT:
            raise
        abspath = os.path.abspath(path)
        parent  = os.path.dirname(abspath)
        name    = os.path.basename(abspath)
        code, out = run_cmd(
            ["docker", "run", "--rm", "-v", f"{parent}:/mnt", "alpine",
             "rm", "-rf", f"/mnt/{name}"],
            timeout=60,
        )
        if code != 0:
            raise PermissionError(
                f"Permission denied and docker fallback failed: {out.strip()}"
            )


def compose_status(name):
    insts = get_instances()
    if name not in insts:
        return False, "Instance not found"
    code, out = run_cmd(["docker", "compose", "ps", "--format", "json"],
                        cwd=insts[name], timeout=10)
    if code != 0:
        return False, out.strip() or "Error running docker compose ps"
    lines = [l for l in out.strip().splitlines() if l.strip()]
    if not lines:
        return False, "No containers"
    rows, all_up = [], True
    for line in lines:
        try:
            obj = json.loads(line)
            if obj.get("State") != "running":
                all_up = False
            rows.append(f"{obj.get('Name','?')}: {obj.get('State','?')} ({obj.get('Status','?')})")
        except Exception:
            rows.append(line)
    return all_up, "\n".join(rows)

def any_instance_running():
    for name in get_instances():
        running, _ = compose_status(name)
        if running:
            return True
    return False
