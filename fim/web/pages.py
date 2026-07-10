"""Load HTML pages from template files."""
import html
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def load_login_html(*, is_openwrt: bool = False) -> str:
    page = _read("login.html")
    if is_openwrt:
        from fim.openwrt_auth import get_default_login_username

        default_user = html.escape(get_default_login_username(), quote=True)
        page = page.replace(
            "Sign in to manage instances",
            "Sign in with your OpenWrt / LuCI username and password",
        )
        page = page.replace(
            '<input type="text" id="u" autocomplete="username" autofocus>',
            f'<input type="text" id="u" autocomplete="username" value="{default_user}" autofocus>',
        )
    return page


def load_main_html(*, is_openwrt: bool, is_apple_silicon: bool) -> str:
    import json

    return (
        _read("index.html")
        .replace("__IS_OPENWRT__", json.dumps(is_openwrt))
        .replace("__IS_APPLE_SILICON__", json.dumps(is_apple_silicon))
    )
