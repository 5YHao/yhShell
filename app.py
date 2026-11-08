"""yhShell — a pywebview SSH client with an AI agent living next to the shell."""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any

import webview

from yhshell.api import Api
from yhshell.store import Store
from yhshell.window import WindowChrome

APP_TITLE = "yhShell"


def _resource_path(*parts: str) -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def build_window(store: Store | None = None, maximized: bool = False) -> tuple[Any, Api]:
    """Create the main window and API bridge (used by app.py and e2e tests)."""
    store = store or Store()
    api = Api(store)

    screens = getattr(webview, "screens", None) or []
    screen = screens[0] if screens else None
    screen_width = int(getattr(screen, "width", 1440) or 1440)
    screen_height = int(getattr(screen, "height", 900) or 900)
    width = min(1400, max(980, screen_width - 120))
    height = min(860, max(660, screen_height - 96))
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)

    window = webview.create_window(
        APP_TITLE,
        _resource_path("web", "index.html"),
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=(980, 660),
        maximized=maximized,
        # The HTML title bar replaces the native caption; WindowChrome brings
        # the native drag / resize / maximize behaviour back.
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#0b0e14",
        text_select=True,
        confirm_close=False,
    )
    if window is None:
        raise RuntimeError("窗口创建失败")
    api._window = window
    api._chrome = WindowChrome(window)

    def on_closing() -> bool:
        try:
            api._stop_background()
            api.shutdown_sessions()
        except Exception:  # noqa: BLE001 - never block closing
            pass
        return True

    window.events.closing += on_closing
    return window, api


def main() -> int:
    # --windowed builds have no console streams; keep startup failures diagnosable.
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        log_dir = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                               "yhShell", "logs")
        os.makedirs(log_dir, exist_ok=True)
        stream = open(os.path.join(log_dir, "startup.log"), "w", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    store = Store()
    try:
        _window, _api = build_window(store)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    debug = os.environ.get("YHSELL_DEBUG", "").strip() not in ("", "0", "false")
    try:
        # Private WebView2 profile: never reuse cached JS/CSS after an upgrade,
        # and don't leave a growing browser profile on disk.
        webview.start(
            debug=debug,
            http_server=True,
            private_mode=True,
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
