"""Read/write instance .env and compose files."""
import os
from fim.instances import get_instances

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

def read_compose(name):
    insts = get_instances()
    if name not in insts:
        return ""
    try:
        with open(os.path.join(insts[name], "docker-compose.yml")) as f:
            return f.read()
    except FileNotFoundError:
        return ""

def write_compose(name, content):
    insts = get_instances()
    if name not in insts:
        raise FileNotFoundError("Instance not found")
    with open(os.path.join(insts[name], "docker-compose.yml"), "w") as f:
        f.write(content)
