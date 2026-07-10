"""Create, delete, and maintain instances."""
import os
import shutil
from fim.commands import run_cmd
from fim.config import DOCKER_COMPOSE_TIMEOUT, INSTANCES_DIR, LOCK_FILE
from fim.docker import force_rmtree
from fim.instances import (
    get_instances,
    get_selected,
    get_templates,
    refresh_instances,
    set_selected,
)

def create_instance(name, template_key):
    name = name.strip()
    if not name or len(name) > 64 or not all(c.isalnum() or c in ('-', '_') for c in name):
        return False, "Invalid name (alphanumeric + - _ only, max 64 chars)"
    insts = get_instances()
    if name in insts:
        return False, "Instance already exists"
    src = get_templates().get(template_key)
    if not src or not os.path.isdir(src):
        return False, f"Template '{template_key}' not found or is not a directory"
    dst = os.path.join(INSTANCES_DIR, name)
    try:
        shutil.copytree(src, dst)
        # Store which template was used
        with open(os.path.join(dst, ".fkm_template"), "w") as f:
            f.write(template_key)
        env_template = os.path.join(dst, ".env.template")
        env_file = os.path.join(dst, ".env")
        if os.path.isfile(env_template) and not os.path.isfile(env_file):
            shutil.copy2(env_template, env_file)
        refresh_instances()
        if get_selected() is None:
            set_selected(name)
        return True, None
    except Exception as e:
        return False, str(e)

def get_instance_template_path(name):
    """Return the template directory path for an instance, or None."""
    insts = get_instances()
    if name not in insts:
        return None
    tmpl_file = os.path.join(insts[name], ".fkm_template")
    if os.path.isfile(tmpl_file):
        try:
            with open(tmpl_file) as f:
                tmpl_key = f.read().strip()
            tmpl_path = get_templates().get(tmpl_key)
            if tmpl_path and os.path.isdir(tmpl_path):
                return tmpl_path
        except Exception:
            pass
    return None

def purge_extra_dirs(instance_path, template_path):
    """Delete directories in instance that don't exist in the template. Returns list of removed dirs."""
    template_entries = set(os.listdir(template_path))
    removed = []
    for entry in os.listdir(instance_path):
        entry_path = os.path.join(instance_path, entry)
        if os.path.islink(entry_path):
            continue
        if os.path.isdir(entry_path) and entry not in template_entries:
            force_rmtree(entry_path)
            removed.append(entry)
    return removed

def delete_instance(name):
    name = name.strip()
    insts = get_instances()
    if name not in insts:
        return False, "Instance not found"
    path = insts[name]
    output = f"=== Down + Delete Volumes + Remove {name} ===\n"
    code, out = run_cmd(["docker", "compose", "down", "--volumes"], cwd=path, timeout=DOCKER_COMPOSE_TIMEOUT)
    output += out
    if code != 0:
        output += "WARNING: docker compose down --volumes failed (continuing with folder removal)\n"
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
        return True, output
    except Exception as e:
        return False, output + "\n" + str(e)
