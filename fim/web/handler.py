"""HTTP request handler."""
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from fim.adapter import (
    get_adapter_state,
    is_adapter_enabled,
    set_adapter_enabled_flag,
    start_adapter,
    stop_adapter,
)
from fim.auth import (
    SESSION_TTL,
    check_credentials,
    create_session,
    get_cookie,
    sessions,
    validate_session,
)
from fim.commands import run_cmd
from fim.config import IS_APPLE_SILICON, IS_OPENWRT
from fim.docker import compose_status
from fim.files import read_compose, read_env, read_template, write_compose, write_env
from fim.instances import get_instances, get_selected, get_templates
from fim.instances_mgmt import create_instance
from fim.progress import action_lock, get_progress
from fim.web.pages import load_login_html, load_main_html
from fim.web.static import resolve_static, static_mime
from fim.workers import (
    backup_lock,
    backup_ready,
    do_action_async,
    do_backup_async,
    do_compose_restart_async,
    do_delete_async,
    do_env_restart_async,
    do_switch_to,
    do_wifi_async,
)

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
    def _send_static(self, rel_path: str):
        file_path = resolve_static(rel_path)
        if not file_path:
            self.send_response(404)
            self.end_headers()
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", static_mime(file_path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        if path in ("/", ""):
            if self.is_auth():
                self.send_html(load_main_html(
                    is_openwrt=IS_OPENWRT,
                    is_apple_silicon=IS_APPLE_SILICON,
                ))
            else:
                self.send_html(load_login_html())
            return
        if not self.require_auth():
            return
        if path == "/api/status":
            selected  = get_selected()
            instances = {}
            for name, inst_path in get_instances().items():
                running, status_text = compose_status(name)
                instances[name] = {"running": running, "status_text": status_text, "path": inst_path}
            self.send_json({"selected": selected, "instances": instances})
        elif path == "/api/env":
            selected = get_selected()
            self.send_json({"selected": selected,
                            "template": read_template(selected),
                            "content":  read_env(selected)})
        elif path == "/api/compose":
            selected = get_selected()
            self.send_json({"selected": selected,
                            "content":  read_compose(selected)})
        elif path == "/api/wifi/current":
            if not IS_OPENWRT:
                self.send_json({"ok": False, "error": "WiFi management requires OpenWrt"})
                return
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
        elif path == "/api/templates":
            self.send_json({"templates": list(get_templates().keys())})
        elif path == "/api/adapter":
            if not IS_APPLE_SILICON:
                self.send_json({"ok": False, "error": "Only available on Apple Silicon Mac"})
                return
            self.send_json(get_adapter_state())
        elif path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            insts = get_instances()
            if not name or name not in insts:
                self.send_json({"ok": False, "error": "Instance not found"}, code=404)
                return
            cwd = insts[name]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                proc = subprocess.Popen(
                    ["docker", "compose", "logs", "-f", "--tail", "200"],
                    cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1
                )
                for line in proc.stdout:
                    data = line.rstrip("\n").replace("\n", "\ndata: ")
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        elif path == "/api/instance/backup/download":
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            with backup_lock:
                tar_path = backup_ready.get(name)
            if not tar_path or not os.path.isfile(tar_path):
                self.send_json({"ok": False, "error": "No backup ready"}, code=404)
                return
            with backup_lock:
                backup_ready.pop(name, None)
            try:
                fsize = os.path.getsize(tar_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                safe_fname = os.path.basename(name).replace('"', '_')
                self.send_header("Content-Disposition", f'attachment; filename="{safe_fname}.tar.gz"')
                self.send_header("Content-Length", str(fsize))
                self.end_headers()
                with open(tar_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            finally:
                try:
                    os.unlink(tar_path)
                except Exception:
                    pass
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
            if not self.require_auth():
                return
            token = get_cookie(self.headers, "session")
            if token and token in sessions:
                del sessions[token]
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
            if action_lock.locked():
                self.send_json({"ok": False, "error": "Action already running"})
                return
            threading.Thread(target=do_switch_to, args=(target,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/action":
            action = body.get("action")
            target = body.get("instance")
            if not action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            action_lock.release()
            threading.Thread(target=do_action_async, args=(action, target), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/env/save":
            selected = get_selected()
            try:
                write_env(selected, body.get("content", ""))
                # Check if instance is running and trigger restart
                restarting = False
                if selected:
                    running, _ = compose_status(selected)
                    if running and action_lock.acquire(blocking=False):
                        action_lock.release()
                        threading.Thread(target=do_env_restart_async, args=(selected,), daemon=True).start()
                        restarting = True
                self.send_json({"ok": True, "restarting": restarting})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
        elif path == "/api/compose/save":
            selected = get_selected()
            try:
                write_compose(selected, body.get("content", ""))
                restarting = False
                if selected:
                    running, _ = compose_status(selected)
                    if running and action_lock.acquire(blocking=False):
                        action_lock.release()
                        threading.Thread(target=do_compose_restart_async, args=(selected,), daemon=True).start()
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
            if not action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            action_lock.release()
            threading.Thread(target=do_delete_async, args=(name,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/adapter/set":
            if not IS_APPLE_SILICON:
                self.send_json({"ok": False, "error": "Only available on Apple Silicon Mac"})
                return
            enabled = bool(body.get("enabled", False))
            set_adapter_enabled_flag(enabled)
            if enabled:
                start_adapter()
            else:
                stop_adapter()
            state = get_adapter_state()
            state["ok"] = True
            self.send_json(state)
        elif path == "/api/instance/backup":
            name = body.get("name", "").strip()
            if not name or name not in get_instances():
                self.send_json({"ok": False, "error": "Instance not found"})
                return
            if not action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            action_lock.release()
            threading.Thread(target=do_backup_async, args=(name,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/wifi":
            if not IS_OPENWRT:
                self.send_json({"ok": False, "error": "WiFi management requires OpenWrt"})
                return
            try:
                hs_ssid  = sanitize_wifi_value(body.get("hs_ssid", ""), "hs_ssid", max_len=32)
                hs_psk   = sanitize_wifi_value(body.get("hs_psk", ""), "hs_psk", max_len=63)
                sta_ssid = sanitize_wifi_value(body.get("sta_ssid", ""), "sta_ssid", max_len=32)
                sta_psk  = sanitize_wifi_value(body.get("sta_psk", ""), "sta_psk", max_len=63)
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)})
                return
            if not any([hs_ssid, hs_psk, sta_ssid, sta_psk]):
                self.send_json({"ok": False, "error": "Nothing to set"})
                return
            if not action_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "Action already running"})
                return
            action_lock.release()
            threading.Thread(target=do_wifi_async,
                             args=(hs_ssid, hs_psk, sta_ssid, sta_psk),
                             daemon=True).start()
            self.send_json({"ok": True})
        else:
            self.send_response(404); self.end_headers()
