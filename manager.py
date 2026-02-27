#!/usr/bin/env python3

"""FKM Instance Manager - Port 8181 (+ dynamic port 80) - DYNAMIC INSTANCES"""

import os, json, subprocess, threading, hashlib, secrets, time, shutil

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── Config ─────────────────────────────────────────────────────────────────

INSTANCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instances")

TEMPLATES = {
    "fkmtest": "/root/fkmtest",
    "prod":    "/root/prod",
}

LOCK_FILE  = "/root/.compose_selected"
PORT_MAIN  = 8181
PORT_ALT   = 80
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE  = os.path.join(SCRIPT_DIR, "auth.json")

# ── Global instances cache ──────────────────────────────────────────────────

_instances = {}

def refresh_instances():
    global _instances
    _instances = {}
    if not os.path.exists(INSTANCES_DIR):
        os.makedirs(INSTANCES_DIR, exist_ok=True)
    for name in os.listdir(INSTANCES_DIR):
        p = os.path.join(INSTANCES_DIR, name)
        if os.path.isdir(p):
            _instances[name] = p

def get_instances():
    return dict(_instances)  # safe copy

# ── Auth ────────────────────────────────────────────────────────────────────

_sessions = {}          # token -> expiry epoch
SESSION_TTL = 86400 * 7 # 7 days

def load_auth():
    if not os.path.exists(AUTH_FILE):
        h = hashlib.sha256(b"root").hexdigest()
        with open(AUTH_FILE, "w") as f:
            json.dump({"username": "root", "password_hash": h}, f, indent=2)
        print(f"Created {AUTH_FILE} with defaults root/root")
    with open(AUTH_FILE) as f:
        return json.load(f)

def check_credentials(username, password):
    auth = load_auth()
    h = hashlib.sha256(password.encode()).hexdigest()
    return auth.get("username") == username and auth.get("password_hash") == h

def create_session():
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token

def validate_session(token):
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if time.time() > exp:
        del _sessions[token]
        return False
    return True

def get_cookie(headers, name):
    for part in headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name)+1:]
    return None

# ── Progress state ──────────────────────────────────────────────────────────

_action_lock   = threading.Lock()
_progress_lock = threading.Lock()
_progress = {
    "active": False,
    "stages": [],
    "log":    "",
    "done":   True,
    "ok":     True,
}

def progress_reset(stages):
    with _progress_lock:
        _progress.update(active=True, done=False, ok=True, log="",
                         stages=[{"label": s, "status": "pending"} for s in stages])

def progress_stage(idx, status, log_line=""):
    with _progress_lock:
        if idx < len(_progress["stages"]):
            _progress["stages"][idx]["status"] = status
        if log_line:
            _progress["log"] += log_line + "\n"

def progress_done(ok=True):
    with _progress_lock:
        _progress.update(done=True, active=False, ok=ok)

def get_progress():
    with _progress_lock:
        return json.loads(json.dumps(_progress))

# ── Selected helpers ────────────────────────────────────────────────────────

def get_selected():
    try:
        with open(LOCK_FILE) as f:
            v = f.read().strip()
        if v and v in get_instances():
            return v
    except Exception:
        pass
    insts = get_instances()
    if insts:
        first = next(iter(insts))
        set_selected(first)
        return first
    return None

def set_selected(name):
    if name and name in get_instances():
        with open(LOCK_FILE, "w") as f:
            f.write(name)
    elif os.path.exists(LOCK_FILE):
        try:
            os.unlink(LOCK_FILE)
        except Exception:
            pass

# ── Compose helpers ─────────────────────────────────────────────────────────

def run_cmd(args, cwd=None, timeout=180):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "Command timed out\n"
    except Exception as e:
        return -1, str(e) + "\n"

def run_cmd_live(args, cwd=None, timeout=180, stage_idx=0):
    """Run a command and stream its output line-by-line into the progress log."""
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        output = ""
        deadline = time.time() + timeout
        for line in proc.stdout:
            output += line
            progress_stage(stage_idx, "running", line.rstrip("\n"))
            if time.time() > deadline:
                proc.kill()
                output += "Command timed out\n"
                progress_stage(stage_idx, "running", "Command timed out")
                return -1, output
        proc.wait(timeout=max(0, deadline - time.time()))
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1, output + "Command timed out\n"
    except Exception as e:
        return -1, str(e) + "\n"

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

def read_env(name):
    insts = get_instances()
    if name not in insts:
        return ""
    try:
        with open(os.path.join(insts[name], ".env")) as f:
            return f.read()
    except FileNotFoundError:
        return ""

def read_template(name):
    insts = get_instances()
    if name not in insts:
        return "# Instance not found"
    try:
        with open(os.path.join(insts[name], ".env.template")) as f:
            return f.read()
    except FileNotFoundError:
        return "# .env.template not found"

def write_env(name, content):
    insts = get_instances()
    if name not in insts:
        raise FileNotFoundError("Instance not found")
    with open(os.path.join(insts[name], ".env"), "w") as f:
        f.write(content)

# ── Instance management ─────────────────────────────────────────────────────

def create_instance(name, template_key):
    name = name.strip()
    if not name or len(name) > 64 or not all(c.isalnum() or c in ('-', '_') for c in name):
        return False, "Invalid name (alphanumeric + - _ only, max 64 chars)"
    insts = get_instances()
    if name in insts:
        return False, "Instance already exists"
    src = TEMPLATES.get(template_key)
    if not src or not os.path.isdir(src):
        return False, f"Template '{template_key}' not found or is not a directory"
    dst = os.path.join(INSTANCES_DIR, name)
    try:
        shutil.copytree(src, dst)
        refresh_instances()
        if get_selected() is None:
            set_selected(name)
        return True, None
    except Exception as e:
        return False, str(e)

def delete_instance(name):
    name = name.strip()
    insts = get_instances()
    if name not in insts:
        return False, "Instance not found"
    path = insts[name]
    output = f"=== Down + Delete Volumes + Remove {name} ===\n"
    code, out = run_cmd(["docker", "compose", "down", "--volumes"], cwd=path, timeout=90)
    output += out
    if code != 0:
        output += "WARNING: docker compose down --volumes failed (continuing with folder removal)\n"
    try:
        shutil.rmtree(path)
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
        return True, output
    except Exception as e:
        return False, output + "\n" + str(e)

# ── Async switch worker ─────────────────────────────────────────────────────

def _do_switch_to(target):
    with _action_lock:
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

def _do_action_async(action, target):
    with _action_lock:
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
            "stop": f"Stop {use_name}",
            "start": f"Start {use_name}",
            "down_volumes": f"Clear data for {use_name}",
        }
        cmd_map = {
            "pull": ["docker", "compose", "pull"],
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
        if action == "down_volumes":
            was_running, _ = compose_status(use_name)

        stages = [label]
        if action == "down_volumes" and was_running:
            stages.append(f"Restart {use_name}")

        progress_reset(stages)
        progress_stage(0, "running")
        code, out = run_cmd_live(cmd, cwd=cwd, stage_idx=0)
        progress_stage(0, "done" if code == 0 else "error")
        if code != 0:
            progress_done(ok=False)
            return

        if action == "down_volumes" and was_running:
            progress_stage(1, "running")
            code, out = run_cmd_live(["docker", "compose", "up", "-d"], cwd=cwd, stage_idx=1)
            progress_stage(1, "done" if code == 0 else "error")

        progress_done(ok=code == 0)

def _do_delete_async(name):
    with _action_lock:
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
            shutil.rmtree(path)
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

def _do_wifi_async(hs_ssid, hs_psk, sta_ssid, sta_psk):
    with _action_lock:
        stages = []
        cmds_set = []
        if hs_ssid:
            cmds_set += [["uci","set",f"wireless.default_radio1.ssid={hs_ssid}"],
                         ["uci","set",f"wireless.default_radio2.ssid={hs_ssid}"]]
        if hs_psk:
            cmds_set += [["uci","set",f"wireless.default_radio1.key={hs_psk}"],
                         ["uci","set",f"wireless.default_radio2.key={hs_psk}"]]
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

def _do_env_restart_async(name):
    """Restart compose after .env change using 'docker compose up -d' to pick up new env."""
    with _action_lock:
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

# ── Dynamic port 80 manager ─────────────────────────────────────────────────

_port80_server   = None
_port80_lock     = threading.Lock()
_port80_running  = False

def _update_port80():
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

def _port80_monitor():
    while True:
        time.sleep(4)
        try:
            _update_port80()
        except Exception as e:
            print(f"Port 80 monitor error: {e}")

# ── HTML pages ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FKM Instance Manager — Login</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;
    min-height:100vh;display:flex;align-items:center;justify-content:center}
  .box{background:#1a1d27;border:1px solid #2a2d3a;border-radius:12px;
    padding:36px 32px;width:100%;max-width:360px}
  h1{font-size:1.1rem;font-weight:600;margin-bottom:6px;color:#fff}
  p{font-size:.82rem;color:#555;margin-bottom:24px}
  label{font-size:.82rem;color:#777;display:block;margin-bottom:4px}
  input{width:100%;background:#0f1117;color:#e0e0e0;border:1px solid #2a2d3a;
    border-radius:6px;padding:9px 11px;font-size:.9rem;margin-bottom:14px}
  input:focus{outline:none;border-color:#3a5caa}
  button{width:100%;padding:10px;background:#2a5caa;color:#fff;border:none;
    border-radius:6px;font-size:.9rem;font-weight:600;cursor:pointer}
  button:hover{background:#3a6cba}
  #err{color:#ff7070;font-size:.82rem;margin-top:10px;min-height:18px}
</style>
</head>
<body>
<div class="box">
  <h1>🐳 FKM Instance Manager</h1>
  <p>Enter your credentials to continue</p>
  <label>Username</label>
  <input type="text" id="u" autofocus>
  <label>Password</label>
  <input type="password" id="p" onkeydown="if(event.key==='Enter')login()">
  <button onclick="login()">Sign In</button>
  <div id="err"></div>
</div>
<script>
async function login() {
  const r = await fetch('/api/login', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: document.getElementById('u').value,
                          password: document.getElementById('p').value})
  });
  const d = await r.json();
  if (d.ok) { window.location.href = '/'; }
  else { document.getElementById('err').textContent = 'Invalid credentials'; }
}
</script>
</body>
</html>"""

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FKM Instance Manager</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}
  header{background:#1a1d27;border-bottom:1px solid #2a2d3a;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  header h1{font-size:1.2rem;font-weight:600;letter-spacing:.5px;color:#fff}
  .badge{padding:4px 12px;border-radius:20px;font-size:.75rem;font-weight:700;letter-spacing:.5px;transition:all .3s ease}
  .badge-fkmtest{background:#1a3a5c;color:#60aaff}
  .badge-prod{background:#3a1a1a;color:#ff6060}
  .badge-ok{background:#1a3a1a;color:#60ff90}
  .badge-down{background:#2a2a2a;color:#999}
  main{max-width:960px;margin:0 auto;padding:24px 16px}
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #2a2d3a}
  .tab{padding:10px 20px;cursor:pointer;border-radius:8px 8px 0 0;font-size:.9rem;background:#1a1d27;color:#888;border:1px solid #2a2d3a;border-bottom:none;transition:all .2s}
  .tab.active{background:#22253a;color:#fff;border-color:#3a3d5a}
  .tab:hover:not(.active){color:#ccc}
  .panel{display:none;animation:fadeIn .25s ease}.panel.active{display:block}
  @keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
  .card{background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:20px;margin-bottom:16px}
  .card h2{font-size:1rem;font-weight:600;margin-bottom:14px;color:#aab}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
  button{padding:8px 18px;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600;transition:all .2s}
  .btn-primary{background:#2a5caa;color:#fff}.btn-primary:hover{background:#3a6cba}
  .btn-danger{background:#aa2a2a;color:#fff}.btn-danger:hover{background:#ba3a3a}
  .btn-success{background:#2a7a2a;color:#fff}.btn-success:hover{background:#3a8a3a}
  .btn-warn{background:#8a6a00;color:#fff}.btn-warn:hover{background:#aa8000}
  .btn-neutral{background:#2a2d3a;color:#ccc}.btn-neutral:hover{background:#3a3d4a}
  button:disabled{opacity:.4;cursor:not-allowed}
  button .spinner{display:inline-block;width:12px;height:12px;border:2px solid transparent;border-top-color:currentColor;border-radius:50%;animation:spin .6s linear infinite;margin-right:6px;vertical-align:middle}
  @keyframes spin{to{transform:rotate(360deg)}}
  textarea{width:100%;background:#0f1117;color:#d0e0d0;border:1px solid #2a2d3a;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:.82rem;resize:vertical;line-height:1.5}
  .status-box{background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:12px;font-family:'Courier New',monospace;font-size:.8rem;white-space:pre-wrap;max-height:180px;overflow-y:auto;color:#90c090;transition:color .3s ease}
  .status-box.err{color:#c09090}
  input[type=text],input[type=password],select{background:#0f1117;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:8px 10px;font-size:.85rem;width:100%}
  label{font-size:.85rem;color:#888;margin-bottom:4px;display:block}
  .field{margin-bottom:12px}
  .split{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:600px){.split{grid-template-columns:1fr}}
  .instance-list{display:flex;flex-direction:column;gap:6px}
  .instance-row{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#1a1d27;border:1px solid #2a2d3a;border-radius:8px;border-left:4px solid #333;transition:border-color .3s ease}
  .instance-row.animate-in{animation:rowIn .3s ease both}
  @keyframes rowIn{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:translateX(0)}}
  .instance-row.selected{border-left-color:#60aaff}
  .instance-row.selected.prod{border-left-color:#ff6060}
  .instance-row .inst-name{font-weight:600;font-size:.9rem;min-width:80px}
  .instance-row .inst-actions{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
  .instance-row .inst-actions button{padding:6px 12px;font-size:.78rem;white-space:nowrap}
  .section-title{font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:10px}
  .progress-modal-overlay{display:none;position:fixed;inset:0;background:#000b;z-index:150;align-items:center;justify-content:center;opacity:0;transition:opacity .2s ease}
  .progress-modal-overlay.show{display:flex;opacity:1}
  .progress-modal{background:#1a1d27;border:1px solid #2a2d3a;border-radius:12px;padding:28px;max-width:480px;width:90%;box-shadow:0 8px 40px #0009;transform:scale(.95);transition:transform .2s ease}
  .progress-modal-overlay.show .progress-modal{transform:scale(1)}
  .progress-modal h3{font-size:1rem;font-weight:600;color:#fff;margin-bottom:16px}
  .progress-stages{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
  .stage-row{display:flex;align-items:center;gap:10px;font-size:.85rem}
  .stage-icon{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.75rem;flex-shrink:0;transition:all .3s;font-weight:700}
  .stage-icon.pending{background:#2a2d3a;color:#555}
  .stage-icon.running{background:#1a3a5c;color:#60aaff;animation:pulse 1s ease-in-out infinite}
  .stage-icon.done{background:#1a3a1a;color:#60ff90}
  .stage-icon.error{background:#3a1a1a;color:#ff6060}
  .stage-label{color:#ccc}
  .stage-label.running{color:#60aaff;font-weight:600}
  .stage-label.done{color:#777}
  .stage-label.error{color:#ff6060;font-weight:600}
  .progress-track{height:5px;background:#2a2d3a;border-radius:3px;overflow:hidden;margin-bottom:12px}
  .progress-fill{height:100%;background:linear-gradient(90deg,#2a5caa,#60aaff);border-radius:3px;transition:width .5s ease;width:0%}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 #2a5caa88}50%{box-shadow:0 0 0 6px #2a5caa00}}
  .output-panel{position:fixed;bottom:0;left:0;right:0;z-index:50;background:#131620;border-top:1px solid #2a2d3a;transition:transform .3s ease;transform:translateY(100%)}
  .output-panel.show{transform:translateY(0)}
  .output-panel-header{display:flex;align-items:center;padding:8px 16px;cursor:pointer;gap:10px;user-select:none}
  .output-panel-header span{font-size:.8rem;font-weight:600;color:#888}
  .output-panel-header .chevron{transition:transform .2s;color:#666}
  .output-panel-header .chevron.up{transform:rotate(180deg)}
  .output-panel-body{max-height:220px;overflow-y:auto;padding:0 16px 12px;font-family:'Courier New',monospace;font-size:.78rem;white-space:pre-wrap;color:#b0c0a0;transition:max-height .3s ease}
  .output-panel-body.collapsed{max-height:0;padding-bottom:0;overflow:hidden}
  .modal-overlay{display:none;position:fixed;inset:0;background:#000b;z-index:100;align-items:center;justify-content:center;opacity:0;transition:opacity .2s ease}
  .modal-overlay.show{display:flex;opacity:1}
  .modal{background:#1a1d27;border:1px solid #3a2020;border-radius:12px;padding:28px;max-width:420px;width:90%;box-shadow:0 8px 40px #0009;transform:scale(.95);transition:transform .2s ease}
  .modal-overlay.show .modal{transform:scale(1)}
  .modal h3{font-size:1rem;color:#ff9090;margin-bottom:10px}
  .modal p{font-size:.85rem;color:#aaa;margin-bottom:18px;line-height:1.6}
  .modal .row{margin-bottom:0}
  .modal-input-wrap{margin-bottom:16px}
  .modal-input-wrap input,.modal-input-wrap select{border-color:#3a2020;width:100%;background:#0f1117;color:#e0e0e0;border:1px solid #2a2d3a;border-radius:6px;padding:9px 11px}
  .redirect-banner{display:none;position:fixed;top:0;left:0;right:0;z-index:200;
    background:#2a5caa;color:#fff;text-align:center;padding:12px;font-size:.9rem;font-weight:600}
  .toast-container{position:fixed;top:16px;right:16px;z-index:300;display:flex;flex-direction:column;gap:8px;pointer-events:none}
  .toast{pointer-events:auto;padding:10px 18px;border-radius:8px;font-size:.85rem;font-weight:600;color:#fff;box-shadow:0 4px 20px #0006;animation:toastIn .3s ease forwards,toastOut .3s ease forwards;animation-delay:0s,3s;opacity:0}
  .toast-success{background:#1a5a2a}
  .toast-error{background:#7a2020}
  .toast-info{background:#1a3a6a}
  @keyframes toastIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}
  @keyframes toastOut{from{opacity:1;transform:translateX(0)}to{opacity:0;transform:translateX(20px)}}
</style>
</head>
<body>
<div class="redirect-banner" id="redirect-banner">
  Redirecting to port 8181…
</div>
<div class="toast-container" id="toast-container"></div>
<header>
  <h1>🐳 FKM Instance Manager</h1>
  <span id="hdr-selected" class="badge">…</span>
  <span id="hdr-status" class="badge badge-down">…</span>
  <span style="flex:1"></span>
  <button class="btn-neutral" style="padding:6px 12px;font-size:.8rem" onclick="refreshAll()">↻ Refresh</button>
  <button class="btn-neutral" style="padding:6px 12px;font-size:.8rem" onclick="logout()">Sign Out</button>
</header>

<!-- Volumes modal (selected only) -->
<div id="modal-overlay" class="modal-overlay">
  <div class="modal">
    <h3>⚠ Delete All Volume Data?</h3>
    <p>This will run <code>docker compose down --volumes</code> on the selected instance, permanently destroying all container volumes.<br><br>Type <strong style="color:#ff9090">DELETE</strong> to confirm.</p>
    <div class="modal-input-wrap">
      <input type="text" id="modal-confirm-input" placeholder="Type DELETE here…" oninput="checkModalInput()">
    </div>
    <div class="row">
      <button class="btn-danger" id="modal-ok-btn" onclick="confirmDownVolumes()" disabled>Delete Everything</button>
      <button class="btn-neutral" onclick="closeModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- Create modal -->
<div id="create-modal-overlay" class="modal-overlay">
  <div class="modal">
    <h3>➕ Create New Instance</h3>
    <div class="modal-input-wrap">
      <label>Template</label>
      <select id="create-template">
        <option value="fkmtest">fkmtest</option>
        <option value="prod">prod</option>
      </select>
    </div>
    <div class="modal-input-wrap">
      <label>Instance Name</label>
      <input type="text" id="create-name" placeholder="e.g. dev-v2, staging" oninput="checkCreateReady()">
    </div>
    <div class="row">
      <button class="btn-success" id="create-ok-btn" onclick="confirmCreate()" disabled>Create Instance</button>
      <button class="btn-neutral" onclick="closeCreateModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- Delete modal -->
<div id="delete-modal-overlay" class="modal-overlay">
  <div class="modal">
    <h3>🗑 Delete Instance <span id="delete-instance-name" style="color:#ff6060"></span></h3>
    <p>This will run <code>docker compose down --volumes</code> and then permanently remove the entire instance folder.<br><br>Type <strong style="color:#ff9090">DELETE</strong> to confirm.</p>
    <div class="modal-input-wrap">
      <input type="text" id="delete-confirm-input" placeholder="Type DELETE here…" oninput="checkDeleteInput()">
    </div>
    <div class="row">
      <button class="btn-danger" id="delete-ok-btn" onclick="confirmDeleteInstance()" disabled>Delete Instance</button>
      <button class="btn-neutral" onclick="closeDeleteModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- Progress modal overlay -->
<div id="progress-modal-overlay" class="progress-modal-overlay">
  <div class="progress-modal">
    <h3 id="progress-modal-title">⏳ Action in progress…</h3>
    <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="progress-stages" id="progress-stages"></div>
  </div>
</div>

<main>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('control')">Control</div>
    <div class="tab" onclick="switchTab('env')">Env Editor</div>
    <div class="tab" onclick="switchTab('wifi')">WiFi</div>
  </div>
  <div class="panel active" id="tab-control">
    <div class="card">
      <h2>Instances <span id="action-selected" style="color:#666;font-weight:400;font-size:.85rem">— loading…</span></h2>
      <div class="row" style="justify-content:flex-end;margin-bottom:12px">
        <button class="btn-neutral" onclick="showCreateModal()">+ New Instance</button>
      </div>
      <div id="instances-container" class="instance-list"></div>
    </div>
  </div>
  <div class="panel" id="tab-env">
    <div class="card">
      <h2>Env Editor — <span id="env-instance-label" style="color:#888">…</span></h2>
      <div class="section-title">Template (.env.template)</div>
      <textarea id="env-template" rows="12" readonly style="opacity:.7;margin-bottom:14px"></textarea>
      <div class="section-title">Current .env</div>
      <textarea id="env-content" rows="14" placeholder="# .env is empty or missing"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn-success" onclick="saveEnv()">💾 Save .env</button>
        <button class="btn-neutral" onclick="loadEnv()">↻ Reload</button>
        <span id="env-save-msg" style="font-size:.82rem"></span>
      </div>
    </div>
  </div>
  <div class="panel" id="tab-wifi">
    <div class="card">
      <h2>WiFi Configuration</h2>
      <div class="section-title">Current IP Addresses</div>
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:18px;font-size:.88rem">
        <div><strong>LAN:</strong> <span id="ip-lan" style="color:#888">—</span></div>
        <div><strong>WAN:</strong> <span id="ip-wan" style="color:#888">—</span></div>
        <div><strong>WAN WiFi:</strong> <span id="ip-wan-wifi" style="color:#888">—</span></div>
      </div>
      <p style="font-size:.82rem;color:#666;margin-bottom:16px">Changes apply via <code>uci</code> and will restart the current compose (if running).</p>
      <div class="split">
        <div>
          <div class="section-title">Hotspot (AP) — radio1 / radio2</div>
          <div class="field"><label>SSID</label><input type="text" id="hs-ssid" placeholder="loading…"></div>
          <div class="field"><label>Password</label><input type="text" id="hs-psk" placeholder="loading…"></div>
        </div>
        <div>
          <div class="section-title">Uplink WiFi (STA) — radio0</div>
          <div class="field"><label>SSID</label><input type="text" id="sta-ssid" placeholder="loading…"></div>
          <div class="field"><label>Password</label><input type="text" id="sta-psk" placeholder="loading…"></div>
        </div>
      </div>
      <div class="row" style="margin-top:6px">
        <button class="btn-primary" onclick="applyWifi()">Apply WiFi Settings</button>
        <button class="btn-neutral" onclick="loadWifi()">↻ Reload Current</button>
        <span id="wifi-msg" style="font-size:.82rem"></span>
      </div>
    </div>
  </div>
</main>

<!-- Collapsible output panel -->
<div id="output-panel" class="output-panel">
  <div class="output-panel-header" onclick="toggleOutputPanel()">
    <span>📋 Command Output</span>
    <span class="chevron" id="output-chevron">▲</span>
    <span style="flex:1"></span>
    <button class="btn-neutral" style="padding:3px 10px;font-size:.72rem" onclick="event.stopPropagation();clearOutput()">Clear</button>
  </div>
  <div class="output-panel-body" id="output-body"></div>
</div>

<script>
const ON_PORT80 = (location.port === '' || location.port === '80');
const LS_TAB    = 'fkm_active_tab';
const LS_LOG    = 'fkm_action_log';
const LS_STATUS = 'fkm_last_status';
let currentSelected = null;
let selectedRunning  = false;
let _busy            = false;
let _pollTimer       = null;
let _busyButtons     = new Set();
let _firstRender     = true;

let _outputCollapsed = false;

// ── Output panel helpers ───────────────────────────────────────────────
function toggleOutputPanel() {
  _outputCollapsed = !_outputCollapsed;
  document.getElementById('output-body').classList.toggle('collapsed', _outputCollapsed);
  document.getElementById('output-chevron').classList.toggle('up', !_outputCollapsed);
}
function showOutput(text) {
  const panel = document.getElementById('output-panel');
  const body = document.getElementById('output-body');
  body.textContent = text;
  panel.classList.add('show');
  _outputCollapsed = false;
  document.getElementById('output-body').classList.remove('collapsed');
  document.getElementById('output-chevron').classList.add('up');
  body.scrollTop = body.scrollHeight;
  lsSet(LS_LOG, text);
}
function clearOutput() {
  document.getElementById('output-panel').classList.remove('show');
  document.getElementById('output-body').textContent = '';
  lsSet(LS_LOG, '');
}

document.addEventListener('click', function(e) {
  if (_busy) return;
  if (e.target.closest('#output-panel')) return;
  if (!_outputCollapsed && document.getElementById('output-panel').classList.contains('show')) {
    _outputCollapsed = true;
    document.getElementById('output-body').classList.add('collapsed');
    document.getElementById('output-chevron').classList.remove('up');
  }
});

// ── localStorage helpers ───────────────────────────────────────────────
function lsSet(k,v){ try{ localStorage.setItem(k,JSON.stringify(v)); }catch(e){} }
function lsGet(k,d){ try{ const v=localStorage.getItem(k); return v!==null?JSON.parse(v):d; }catch(e){ return d; } }

// ── Toast notifications ────────────────────────────────────────────────
function showToast(msg, type='info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { if(t.parentNode) t.remove(); }, 3300);
}

// ── Tab switching with persistence ─────────────────────────────────────
function switchTab(name) {
  ['control','env','wifi'].forEach((n,i) => {
    document.querySelectorAll('.tab')[i].classList.toggle('active', n===name);
    const panel = document.querySelectorAll('.panel')[i];
    panel.classList.toggle('active', n===name);
  });
  lsSet(LS_TAB, name);
  if (name==='env')  loadEnv();
  if (name==='wifi') loadWifi();
}

async function api(path, body=null) {
  const opts = body
    ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}
    : {};
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = '/'; return null; }
  return r.json();
}

async function logout() {
  await api('/api/logout', {});
  window.location.href = '/';
}

function applyStatusData(data) {
  currentSelected = data.selected;
  selectedRunning  = data.instances[data.selected] ? data.instances[data.selected].running : false;

  const hdrSel = document.getElementById('hdr-selected');
  hdrSel.textContent = (data.selected || 'NONE').toUpperCase();
  hdrSel.className   = 'badge badge-'+(data.selected || 'fkmtest');

  const hdrSt = document.getElementById('hdr-status');
  hdrSt.textContent = selectedRunning ? 'RUNNING' : 'STOPPED';
  hdrSt.className   = 'badge '+(selectedRunning ? 'badge-ok' : 'badge-down');

  document.getElementById('action-selected').textContent = currentSelected ? `— selected: ${currentSelected}` : '— no instances';

  renderInstances(data.instances, currentSelected);
}

async function refreshAll() {
  try {
    const data = await api('/api/status');
    if (!data) return;
    lsSet(LS_STATUS, data);
    applyStatusData(data);
  } catch(e) {}
}

function renderInstances(instances, selected) {
  const container = document.getElementById('instances-container');
  let html = '';
  let idx = 0;
  const animCls = _firstRender ? ' animate-in' : '';
  for (const [name, info] of Object.entries(instances || {})) {
    const isSel = name === selected;
    let btns = '';
    if (isSel) {
      btns += info.running
        ? `<button class="btn-danger" data-action="stop-${name}" onclick="doAction('stop','${name}',this)">⏹ Stop</button>`
        : `<button class="btn-success" data-action="start-${name}" onclick="doAction('start','${name}',this)">▶ Start</button>`;
    } else {
      btns += `<button class="btn-primary" data-action="activate-${name}" onclick="startSwitchTo('${name}',this)">⇄ Activate</button>`;
    }
    btns += `<button class="btn-neutral" data-action="pull-${name}" onclick="doAction('pull','${name}',this)">⬇ Pull</button>`;
    if (isSel) {
      btns += `<button class="btn-warn" title="Clear Data (docker compose down --volumes)" onclick="openModal()">🧹 Clear</button>`;
    }
    btns += `<button class="btn-danger" onclick="showDeleteModal('${name}')">🗑</button>`;

    html += `
<div class="instance-row${isSel ? ' selected' : ''}${isSel && name === 'prod' ? ' prod' : ''}${animCls}" style="${_firstRender ? 'animation-delay:'+Math.min(idx*40,200)+'ms' : ''}">
  <span class="inst-name" style="color:${name === 'prod' ? '#ff6060' : '#60aaff'}">${name}</span>
  <span class="badge ${info.running ? 'badge-ok' : 'badge-down'}">${info.running ? 'UP' : 'DOWN'}${isSel ? ' ★' : ''}</span>
  <div class="inst-actions">${btns}</div>
</div>`;
    idx++;
  }
  container.innerHTML = html;
  _firstRender = false;
}

function setBtnLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    btn._origHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>' + btn.innerText.replace(/[^\w\s]/g,'').trim();
    _busyButtons.add(btn.dataset.action);
  } else {
    btn.disabled = false;
    if (btn._origHTML) btn.innerHTML = btn._origHTML;
    _busyButtons.delete(btn.dataset.action);
  }
}

function startProgressPolling() {
  _busy = true;
  if (_pollTimer) clearInterval(_pollTimer);
  document.getElementById('progress-modal-overlay').classList.add('show');
  document.getElementById('progress-stages').innerHTML = '';
  document.getElementById('progress-fill').style.width = '0%';
  _pollTimer = setInterval(pollProgress, 600);
}

async function doAction(action, target=null, triggerBtn=null) {
  if (_busy) return;
  setBtnLoading(triggerBtn, true);
  if (action === 'start' && ON_PORT80) {
    document.getElementById('redirect-banner').style.display = 'block';
    setTimeout(() => {
      window.location.href = 'http://' + location.hostname + ':8181/';
    }, 2200);
  }
  const body = target ? {action, instance: target} : {action};
  const data = await api('/api/action', body);
  setBtnLoading(triggerBtn, false);
  if (!data || !data.ok) {
    showToast(data?.error || 'Action failed', 'error');
    return;
  }
  startProgressPolling();
}

// Switch / Activate
async function startSwitchTo(name, triggerBtn=null) {
  if (_busy) return;
  setBtnLoading(triggerBtn, true);
  if (ON_PORT80) {
    document.getElementById('redirect-banner').style.display = 'block';
    setTimeout(() => {
      window.location.href = 'http://' + location.hostname + ':8181/';
    }, 2800);
  }
  await api('/api/switch_to', {target: name});
  setBtnLoading(triggerBtn, false);
  startProgressPolling();
}

function renderProgress(data) {
  const container = document.getElementById('progress-stages');
  container.innerHTML = '';
  let doneCount = 0;
  const icons = {pending:'○', running:'◉', done:'✓', error:'✕'};
  data.stages.forEach(s => {
    if (s.status==='done') doneCount++;
    container.innerHTML += '<div class="stage-row">' +
      '<div class="stage-icon '+s.status+'">'+icons[s.status]+'</div>' +
      '<span class="stage-label '+s.status+'">'+s.label+'</span></div>';
  });
  const pct = data.stages.length ? Math.round(doneCount/data.stages.length*100) : 0;
  document.getElementById('progress-fill').style.width = pct+'%';
}

async function pollProgress() {
  let data;
  try { data = await api('/api/progress'); } catch(e) { return; }
  if (!data) return;
  renderProgress(data);
  // Live-update output panel while action is running
  if (data.log) {
    showOutput(data.log);
  }
  if (data.done) {
    clearInterval(_pollTimer);
    _pollTimer = null;
    _busy = false;
    setTimeout(() => {
      document.getElementById('progress-modal-overlay').classList.remove('show');
    }, 1200);
    showToast(data.ok ? 'Action completed successfully' : 'Action failed', data.ok ? 'success' : 'error');
    await refreshAll();
  }
}

async function maybeRestoreProgress() {
  const data = await api('/api/progress');
  if (!data || data.done) return;
  _busy = true;
  document.getElementById('progress-modal-overlay').classList.add('show');
  renderProgress(data);
  _pollTimer = setInterval(pollProgress, 600);
}

// Env
async function loadEnv() {
  const data = await api('/api/env');
  if (!data) return;
  document.getElementById('env-template').value = data.template;
  document.getElementById('env-content').value  = data.content;
  document.getElementById('env-instance-label').textContent = data.selected || 'none';
  document.getElementById('env-save-msg').textContent = '';
}

async function saveEnv() {
  const content = document.getElementById('env-content').value;
  const data    = await api('/api/env/save', {content});
  if (!data) return;
  const msg     = document.getElementById('env-save-msg');
  msg.textContent = data.ok ? '✓ Saved' : ('✗ '+data.error);
  msg.style.color = data.ok ? '#60ff90' : '#ff6060';
  if (data.ok && data.restarting) {
    showToast('Saved — restarting compose to apply changes…', 'info');
    startProgressPolling();
  } else {
    showToast(data.ok ? 'Environment saved' : 'Failed to save .env', data.ok ? 'success' : 'error');
  }
}

// WiFi
async function loadWifi() {
  const msg = document.getElementById('wifi-msg');
  msg.textContent = 'Loading...'; msg.style.color = '#888';
  const data = await api('/api/wifi/current');
  if (!data) return;
  document.getElementById('hs-ssid').value  = data.hs_ssid  || '';
  document.getElementById('hs-psk').value   = data.hs_psk   || '';
  document.getElementById('sta-ssid').value = data.sta_ssid || '';
  document.getElementById('sta-psk').value  = data.sta_psk  || '';
  document.getElementById('ip-lan').textContent     = data.lan_ip      || '—';
  document.getElementById('ip-wan').textContent     = data.wan_ip      || '—';
  document.getElementById('ip-wan-wifi').textContent = data.wan_wifi_ip || '—';
  if (data.ok) { msg.textContent = ''; }
  else { msg.textContent = '⚠ uci unavailable'; msg.style.color='#ffaa00'; }
}

async function applyWifi() {
  if (_busy) return;
  const msg = document.getElementById('wifi-msg');
  msg.textContent = '';
  const body = {
    hs_ssid: document.getElementById('hs-ssid').value,
    hs_psk:  document.getElementById('hs-psk').value,
    sta_ssid:document.getElementById('sta-ssid').value,
    sta_psk: document.getElementById('sta-psk').value,
  };
  const data = await api('/api/wifi', body);
  if (!data || !data.ok) {
    showToast(data?.error || 'WiFi apply failed', 'error');
    msg.textContent = '✗ ' + (data?.error || 'Error');
    msg.style.color = '#ff6060';
    return;
  }
  startProgressPolling();
}

// Modals
function openModal() {
  document.getElementById('modal-confirm-input').value = '';
  document.getElementById('modal-ok-btn').disabled = true;
  document.getElementById('modal-overlay').classList.add('show');
  setTimeout(() => document.getElementById('modal-confirm-input').focus(), 50);
}
function closeModal() { document.getElementById('modal-overlay').classList.remove('show'); }
function checkModalInput() {
  document.getElementById('modal-ok-btn').disabled =
    document.getElementById('modal-confirm-input').value !== 'DELETE';
}
async function confirmDownVolumes() { closeModal(); await doAction('down_volumes'); }

function showCreateModal() {
  document.getElementById('create-name').value = '';
  document.getElementById('create-ok-btn').disabled = true;
  document.getElementById('create-modal-overlay').classList.add('show');
}
function closeCreateModal() { document.getElementById('create-modal-overlay').classList.remove('show'); }
function checkCreateReady() {
  document.getElementById('create-ok-btn').disabled = !document.getElementById('create-name').value.trim();
}
function confirmCreate() {
  const name = document.getElementById('create-name').value.trim();
  const templ = document.getElementById('create-template').value;
  closeCreateModal();
  createNewInstance(name, templ);
}
async function createNewInstance(name, template) {
  const data = await api('/api/instance/create', {name, template});
  if (!data) return;
  if (data.ok) {
    showToast(`Instance "${name}" created`, 'success');
    await refreshAll();
  } else {
    showToast('Create failed: ' + (data.error || 'Unknown error'), 'error');
  }
}

function showDeleteModal(name) {
  document.getElementById('delete-instance-name').textContent = name;
  document.getElementById('delete-confirm-input').value = '';
  document.getElementById('delete-ok-btn').disabled = true;
  document.getElementById('delete-modal-overlay').classList.add('show');
}
function closeDeleteModal() { document.getElementById('delete-modal-overlay').classList.remove('show'); }
function checkDeleteInput() {
  document.getElementById('delete-ok-btn').disabled =
    document.getElementById('delete-confirm-input').value !== 'DELETE';
}
function confirmDeleteInstance() {
  const name = document.getElementById('delete-instance-name').textContent;
  closeDeleteModal();
  deleteInstance(name);
}
async function deleteInstance(name) {
  if (_busy) return;
  const data = await api('/api/instance/delete', {name});
  if (!data || !data.ok) {
    showToast('Delete failed: ' + (data?.error || 'Unknown error'), 'error');
    return;
  }
  startProgressPolling();
}

// ── Boot: restore saved state then fetch live data ─────────────────────
(async () => {
  // Restore saved tab
  const savedTab = lsGet(LS_TAB, 'control');
  if (savedTab !== 'control') switchTab(savedTab);

  // Restore cached status instantly (prevents blank screen on reload)
  const cached = lsGet(LS_STATUS, null);
  if (cached && cached.instances) {
    applyStatusData(cached);
  }

  // Restore saved output panel
  const savedLog = lsGet(LS_LOG, '');
  if (savedLog) {
    showOutput(savedLog);
  }

  await maybeRestoreProgress();
  await refreshAll();
  setInterval(refreshAll, 10000);
})();
</script>
</body>
</html>
"""

# ── Request handler ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200, extra_headers=None):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def is_auth(self):
        return validate_session(get_cookie(self.headers, "session"))

    def require_auth(self):
        if self.is_auth():
            return True
        self.send_json({"error": "unauthorized"}, code=401)
        return False

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            if self.is_auth():
                self.send_html(MAIN_HTML)
            else:
                self.send_html(LOGIN_HTML)
            return
        if not self.require_auth():
            return
        if path == "/api/status":
            selected  = get_selected()
            instances = {}
            for name, path in get_instances().items():
                running, status_text = compose_status(name)
                instances[name] = {"running": running, "status_text": status_text, "path": path}
            self.send_json({"selected": selected, "instances": instances})
        elif path == "/api/env":
            selected = get_selected()
            self.send_json({"selected": selected,
                            "template": read_template(selected),
                            "content":  read_env(selected)})
        elif path == "/api/wifi/current":
            def uci_get(key):
                code, out = run_cmd(["uci", "get", key], timeout=5)
                return out.strip() if code == 0 else ""
            def ifstatus_ip(iface):
                code, out = run_cmd(["ifstatus", iface], timeout=5)
                if code != 0:
                    return ""
                try:
                    info = json.loads(out)
                    addrs = info.get("ipv4-address", [])
                    return addrs[0]["address"] if addrs else ""
                except Exception:
                    return ""
            try:
                hs_ssid  = uci_get("wireless.default_radio1.ssid")
                hs_psk   = uci_get("wireless.default_radio1.key")
                sta_ssid = uci_get("wireless.default_radio0.ssid")
                sta_psk  = uci_get("wireless.default_radio0.key")
                lan_ip      = ifstatus_ip("lan")
                wan_ip      = ifstatus_ip("wan")
                wan_wifi_ip = ifstatus_ip("wanWIFI")
                ok = True
            except Exception:
                hs_ssid = hs_psk = sta_ssid = sta_psk = lan_ip = wan_ip = wan_wifi_ip = ""; ok = False
            self.send_json({"ok": ok, "hs_ssid": hs_ssid, "hs_psk": hs_psk,
                            "sta_ssid": sta_ssid, "sta_psk": sta_psk,
                            "lan_ip": lan_ip, "wan_ip": wan_ip,
                            "wan_wifi_ip": wan_wifi_ip})
        elif path == "/api/progress":
            self.send_json(get_progress())
        else:
            self.send_response(404); self.end_headers()

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        body = self.read_body()
        if path == "/api/login":
            u = body.get("username", "")
            p = body.get("password", "")
            if check_credentials(u, p):
                token = create_session()
                body_bytes = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.send_header("Set-Cookie",
                    f"session={token}; Path=/; HttpOnly; Max-Age={SESSION_TTL}")
                self.end_headers()
                self.wfile.write(body_bytes)
            else:
                self.send_json({"ok": False})
            return

        if path == "/api/logout":
            token = get_cookie(self.headers, "session")
            if token and token in _sessions:
                del _sessions[token]
            body_bytes = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Set-Cookie",
                "session=deleted; Path=/; HttpOnly; Max-Age=0")
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        if not self.require_auth():
            return

        if path == "/api/switch_to":
            target = body.get("target")
            if not target or target not in get_instances():
                self.send_json({"ok": False, "error": "Invalid target"})
                return
            if _action_lock.locked():
                self.send_json({"ok": False, "error": "Action already running"})
                return
            threading.Thread(target=_do_switch_to, args=(target,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/action":
            action = body.get("action")
            target = body.get("instance")
            if not _action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            _action_lock.release()
            threading.Thread(target=_do_action_async, args=(action, target), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/env/save":
            selected = get_selected()
            try:
                write_env(selected, body.get("content", ""))
                # Check if instance is running and trigger restart
                restarting = False
                if selected:
                    running, _ = compose_status(selected)
                    if running and _action_lock.acquire(blocking=False):
                        _action_lock.release()
                        threading.Thread(target=_do_env_restart_async, args=(selected,), daemon=True).start()
                        restarting = True
                self.send_json({"ok": True, "restarting": restarting})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
        elif path == "/api/instance/create":
            name = body.get("name", "").strip()
            templ = body.get("template", "")
            ok, err = create_instance(name, templ)
            self.send_json({"ok": ok, "error": err if not ok else None})
        elif path == "/api/instance/delete":
            name = body.get("name", "").strip()
            if not name or name not in get_instances():
                self.send_json({"ok": False, "error": "Instance not found"})
                return
            if not _action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            _action_lock.release()
            threading.Thread(target=_do_delete_async, args=(name,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/wifi":
            hs_ssid  = body.get("hs_ssid", "")
            hs_psk   = body.get("hs_psk", "")
            sta_ssid = body.get("sta_ssid", "")
            sta_psk  = body.get("sta_psk", "")
            if not any([hs_ssid, hs_psk, sta_ssid, sta_psk]):
                self.send_json({"ok": False, "error": "Nothing to set"})
                return
            if not _action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            _action_lock.release()
            threading.Thread(target=_do_wifi_async,
                             args=(hs_ssid, hs_psk, sta_ssid, sta_psk),
                             daemon=True).start()
            self.send_json({"ok": True})
        else:
            self.send_response(404); self.end_headers()

# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(INSTANCES_DIR, exist_ok=True)
    refresh_instances()
    if not os.path.exists(LOCK_FILE) and _instances:
        set_selected(next(iter(_instances)))
    load_auth()

    main_server = ThreadingHTTPServer(("0.0.0.0", PORT_MAIN), Handler)
    threading.Thread(target=main_server.serve_forever, daemon=True).start()
    print(f"FKM Instance Manager running on http://0.0.0.0:{PORT_MAIN}")
    print(f"Instances directory: {INSTANCES_DIR}")
    print(f"Templates: {list(TEMPLATES.keys())}")
    print(f"Selected instance: {get_selected() or 'none'}")

    threading.Thread(target=_port80_monitor, daemon=True).start()
    _update_port80()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
        main_server.shutdown()
