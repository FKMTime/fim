"""Background workers for long-running actions."""
import os
import tarfile
import threading
from fim.commands import run_cmd, run_cmd_live
from fim.openwrt_wifi import get_hotspot_radio_sections
from fim.config import DATA_DIR, LOCK_FILE
from fim.instances import get_instances, get_selected, refresh_instances, set_selected
from fim.docker import compose_status, force_rmtree
from fim.instances_mgmt import (
    get_instance_template_path,
    purge_extra_dirs,
)
from fim.progress import (
    action_lock,
    progress_done,
    progress_log_line,
    progress_reset,
    progress_stage,
)

# ── Async switch worker ─────────────────────────────────────────────────────

def do_switch_to(target):
    with action_lock:
        insts = get_instances()
        if target not in insts:
            progress_done(ok=False)
            return
        selected = get_selected()
        if selected == target or selected is None:
            progress_reset([f"Start {target}"])
            progress_stage(0, "running")
            code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=insts[target], stage_idx=0)
            progress_stage(0, "done" if code == 0 else "error")
            if code == 0:
                set_selected(target)
            progress_done(ok=code == 0)
            return
        # full switch
        progress_reset([f"Stop {selected}", f"Start {target}", "Update selection"])
        progress_stage(0, "running")
        code, out = run_cmd_live(["docker", "compose", "down"], cwd=insts[selected], stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        if code != 0:
            progress_done(ok=False)
            return
        progress_stage(1, "running")
        code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=insts[target], stage_idx=1)
        progress_stage(1, "done" if code == 0 else "error")
        if code != 0:
            progress_done(ok=False)
            return
        progress_stage(2, "running")
        set_selected(target)
        progress_stage(2, "done", f"Selected: {target}")
        progress_done(ok=True)

# ── Async action worker ─────────────────────────────────────────────────────

def do_action_async(action, target):
    with action_lock:
        insts = get_instances()
        use_name = target if (target and target in insts) else get_selected()
        if not use_name or use_name not in insts:
            progress_reset([action])
            progress_stage(0, "error", "No valid instance selected")
            progress_done(ok=False)
            return
        cwd = insts[use_name]
        label_map = {
            "pull": f"Pull images for {use_name}",
            "pull_up": f"Pull images for {use_name}",
            "stop": f"Stop {use_name}",
            "start": f"Start {use_name}",
            "down_volumes": f"Clear data for {use_name}",
        }
        cmd_map = {
            "pull": ["docker", "compose", "pull"],
            "pull_up": ["docker", "compose", "pull"],
            "stop": ["docker", "compose", "down"],
            "start": ["docker", "compose", "up", "-d"],
            "down_volumes": ["docker", "compose", "down", "--volumes"],
        }
        label = label_map.get(action, action)
        cmd = cmd_map.get(action)
        if not cmd:
            progress_reset([label])
            progress_stage(0, "error", "Unknown action")
            progress_done(ok=False)
            return
        # For down_volumes: check if containers were running so we can restart after
        was_running = False
        tmpl_path = None
        if action == "down_volumes":
            was_running, _ = compose_status(use_name)
            tmpl_path = get_instance_template_path(use_name)

        stages = [label]
        if action == "down_volumes" and tmpl_path:
            stages.append(f"Purge extra dirs for {use_name}")
        if action == "down_volumes" and was_running:
            stages.append(f"Restart {use_name}")
        if action == "pull_up":
            stages.append(f"Start {use_name}")

        progress_reset(stages)
        progress_stage(0, "running")
        code, out = run_cmd_live(cmd, cwd=cwd, stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        if code != 0:
            progress_done(ok=False)
            return

        si = 1
        if action == "down_volumes" and tmpl_path:
            progress_stage(si, "running")
            try:
                removed = purge_extra_dirs(cwd, tmpl_path)
                if removed:
                    progress_stage(si, "running", f"Removed dirs: {', '.join(removed)}")
                else:
                    progress_stage(si, "running", "No extra directories to remove")
                progress_stage(si, "done")
            except Exception as e:
                progress_stage(si, "error", str(e))
                progress_done(ok=False)
                return
            si += 1

        if (action == "down_volumes" and was_running) or action == "pull_up":
            progress_stage(si, "running")
            code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=cwd, stage_idx=si)
            progress_stage(si, "done" if code == 0 else "error")

        progress_done(ok=code == 0)

def do_delete_async(name):
    with action_lock:
        insts = get_instances()
        if name not in insts:
            progress_reset([f"Delete {name}"])
            progress_stage(0, "error", "Instance not found")
            progress_done(ok=False)
            return
        path = insts[name]
        progress_reset([f"Stop containers for {name}", f"Remove {name}"])
        progress_stage(0, "running")
        code, out = run_cmd_live(["docker", "compose", "down", "--volumes"], cwd=path, timeout=90, stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        progress_stage(1, "running")
        try:
            force_rmtree(path)
            refresh_instances()
            if get_selected() == name:
                new_insts = get_instances()
                if new_insts:
                    set_selected(next(iter(new_insts)))
                else:
                    try:
                        os.unlink(LOCK_FILE)
                    except Exception:
                        pass
            progress_stage(1, "done", f"Instance {name} removed")
            progress_done(ok=True)
        except Exception as e:
            progress_stage(1, "error", str(e))
            progress_done(ok=False)

def do_wifi_async(hs_ssid, hs_psk, sta_ssid, sta_psk):
    with action_lock:
        stages = []
        cmds_set = []
        hotspot_radios = get_hotspot_radio_sections()
        if hs_ssid:
            cmds_set += [["uci", "set", f"wireless.{radio}.ssid={hs_ssid}"]
                         for radio in hotspot_radios]
        if hs_psk:
            cmds_set += [["uci", "set", f"wireless.{radio}.key={hs_psk}"]
                         for radio in hotspot_radios]
        if sta_ssid:
            cmds_set.append(["uci","set",f"wireless.default_radio0.ssid={sta_ssid}"])
        if sta_psk:
            cmds_set.append(["uci","set",f"wireless.default_radio0.key={sta_psk}"])
        if not cmds_set:
            progress_reset(["Apply WiFi"])
            progress_stage(0, "error", "Nothing to set.")
            progress_done(ok=False)
            return

        stages = ["Set WiFi parameters", "Commit & reload WiFi"]
        selected = get_selected()
        if selected:
            running, _ = compose_status(selected)
            if running:
                stages.append(f"Restart {selected} compose")
        progress_reset(stages)

        # Stage 0: uci set commands
        progress_stage(0, "running")
        ok = True
        for cmd in cmds_set:
            log_line = f"$ {' '.join(cmd)}"
            progress_stage(0, "running", log_line)
            code, out = run_cmd(cmd, timeout=10)
            if out.strip():
                progress_stage(0, "running", out.strip())
            if code != 0:
                ok = False
        progress_stage(0, "done" if ok else "error")
        if not ok:
            progress_done(ok=False)
            return

        # Stage 1: commit + reload
        progress_stage(1, "running")
        progress_stage(1, "running", "$ uci commit wireless")
        code, out = run_cmd(["uci","commit","wireless"], timeout=10)
        if out.strip():
            progress_stage(1, "running", out.strip())
        if code != 0:
            progress_stage(1, "error", "uci commit failed")
            progress_done(ok=False)
            return
        progress_stage(1, "running", "$ wifi reload")
        code, out = run_cmd_live(["wifi","reload"], timeout=15, stage_idx=1)
        progress_stage(1, "done")

        # Stage 2 (optional): restart compose
        if len(stages) > 2 and selected:
            progress_stage(2, "running")
            progress_stage(2, "running", f"$ docker compose restart ({selected})")
            code, out = run_cmd_live(["docker","compose","restart"], cwd=get_instances()[selected], stage_idx=2)
            progress_stage(2, "done" if code == 0 else "error")

        progress_done(ok=True)

def do_env_restart_async(name):
    """Restart compose after .env change using 'docker compose up -d' to pick up new env."""
    with action_lock:
        insts = get_instances()
        if name not in insts:
            progress_reset([f"Restart {name}"])
            progress_stage(0, "error", "Instance not found")
            progress_done(ok=False)
            return
        progress_reset([f"Apply .env changes to {name}"])
        progress_stage(0, "running")
        progress_stage(0, "running", f"$ docker compose up -d ({name})")
        code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=insts[name], stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        progress_done(ok=code == 0)

def do_compose_restart_async(name):
    """Down then up compose after docker-compose.yml change."""
    with action_lock:
        insts = get_instances()
        if name not in insts:
            progress_reset([f"Restart {name}"])
            progress_stage(0, "error", "Instance not found")
            progress_done(ok=False)
            return
        cwd = insts[name]
        progress_reset([f"Stop {name}", f"Start {name}"])
        progress_stage(0, "running")
        progress_stage(0, "running", f"$ docker compose down ({name})")
        code, out = run_cmd_live(["docker", "compose", "down"], cwd=cwd, stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        if code != 0:
            progress_done(ok=False)
            return
        progress_stage(1, "running")
        progress_stage(1, "running", f"$ docker compose up -d ({name})")
        code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=cwd, stage_idx=1)
        progress_stage(1, "done" if code == 0 else "error")
        progress_done(ok=code == 0)

# ── Backup state ────────────────────────────────────────────────────────────

backup_lock  = threading.Lock()
backup_ready = {}  # name -> tar_path

def do_backup_async(name):
    with action_lock:
        insts = get_instances()
        if name not in insts:
            progress_reset([f"Backup {name}"])
            progress_stage(0, "error", "Instance not found")
            progress_done(ok=False)
            return
        cwd = insts[name]
        running, _ = compose_status(name)

        stages = []
        if running:
            stages.append(f"Stop {name}")
        stages.append(f"Create archive for {name}")
        if running:
            stages.append(f"Restart {name}")
        progress_reset(stages)

        si = 0
        if running:
            progress_stage(si, "running")
            progress_stage(si, "running", f"$ docker compose down ({name})")
            code, out = run_cmd_live(["docker", "compose", "down"], cwd=cwd, stage_idx=si)
            progress_stage(si, "done" if code == 0 else "error")
            if code != 0:
                progress_done(ok=False)
                return
            si += 1

        progress_stage(si, "running")
        safe_name = os.path.basename(name)
        tar_path = os.path.join(DATA_DIR, f"{safe_name}.tar.gz")
        try:
            progress_stage(si, "running", f"Creating {name}.tar.gz …")
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(cwd, arcname=name)
            progress_stage(si, "done", f"Archive ready: {name}.tar.gz")
            with backup_lock:
                backup_ready[name] = tar_path
        except Exception as e:
            progress_stage(si, "error", str(e))
            progress_done(ok=False)
            return
        si += 1

        if running:
            progress_stage(si, "running")
            progress_stage(si, "running", f"$ docker compose up -d ({name})")
            code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=cwd, stage_idx=si)
            progress_stage(si, "done" if code == 0 else "error")

        progress_done(ok=True)
