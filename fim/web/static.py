"""Serve static web assets."""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def resolve_static(rel_path: str):
    """Return absolute file path if safe and exists, else None."""
    rel_path = rel_path.lstrip("/")
    if not rel_path or ".." in rel_path.split("/"):
        return None
    root = STATIC_DIR.resolve()
    path = (STATIC_DIR / rel_path).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        return None
    return path


def static_mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")
