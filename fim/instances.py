"""Instance directory cache and selection."""
import os
from fim.config import INSTANCES_DIR, LOCK_FILE, TEMPLATES_DIR

def get_templates():
    """Scan templates directory and return {name: path} for each subdirectory."""
    templates = {}
    if os.path.isdir(TEMPLATES_DIR):
        for name in sorted(os.listdir(TEMPLATES_DIR)):
            p = os.path.join(TEMPLATES_DIR, name)
            if os.path.isdir(p):
                templates[name] = p
    return templates


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
