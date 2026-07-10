"""Load HTML pages from template files."""
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def load_login_html(*, is_openwrt: bool = False) -> str:
    html = _read("login.html")
    if is_openwrt:
        html = html.replace(
            '<input type="text" id="u" autocomplete="username" autofocus>',
            '<input type="text" id="u" autocomplete="username" value="root" readonly>',
        )
        html = html.replace(
            "Sign in to manage instances",
            "Sign in with your OpenWrt root password",
        )
    return html


def load_main_html(*, is_openwrt: bool, is_apple_silicon: bool) -> str:
    import json

    return (
        _read("index.html")
        .replace("__IS_OPENWRT__", json.dumps(is_openwrt))
        .replace("__IS_APPLE_SILICON__", json.dumps(is_apple_silicon))
    )
