"""Frameless window chrome for the main window (Windows only).

The frontend draws its own title bar, so :mod:`app` creates the window with
``frameless=True``.  Everything the native caption used to provide has to be
re-implemented here: dragging, resizing from the edges, maximising into the
monitor work area (so the taskbar stays visible) and the DWM shadow / rounded
corners / border colour.

Win32 modal move and size loops, as well as :func:`ReleaseCapture`, are
thread-affine: they must run on the WinForms UI thread that owns the window.
The frontend therefore only *posts* a custom message; the window procedure
installed below performs the native gesture on the right thread:

=========================  ====================================================
``window_drag``            native move loop (``HTCAPTION``)
``window_resize(edge)``    native size loop (``HTLEFT`` … ``HTBOTTOMRIGHT``)
``window_toggle_maximize`` maximize / restore, clamped to the work area
``window_set_theme``       repaint the 1px DWM border to match the theme
=========================  ====================================================

The window procedure is chained with ``SetWindowLongPtrW(GWLP_WNDPROC)`` so
WinForms keeps handling every message we do not care about.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any, Callable

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------- constants
GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
WM_NCDESTROY = 0x0082
WM_APP = 0x8000
WM_YHSH_MOVE = WM_APP + 0x51
WM_YHSH_SIZE = WM_APP + 0x52
WM_YHSH_THEME = WM_APP + 0x53

HT_CAPTION = 2
HT_LEFT, HT_RIGHT, HT_TOP = 10, 11, 12
HT_TOPLEFT, HT_TOPRIGHT = 13, 14
HT_BOTTOM, HT_BOTTOMLEFT, HT_BOTTOMRIGHT = 15, 16, 17

MONITOR_DEFAULTTONEAREST = 2

DWMWA_NCRENDERING_POLICY = 2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMNCRP_ENABLED = 2
DWMWCP_ROUND = 2

#: Frontend edge ids mapped to the matching ``WM_NCHITTEST`` hit-test code.
EDGE_HIT_TEST = {
    "left": HT_LEFT,
    "right": HT_RIGHT,
    "top": HT_TOP,
    "bottom": HT_BOTTOM,
    "top-left": HT_TOPLEFT,
    "top-right": HT_TOPRIGHT,
    "bottom-left": HT_BOTTOMLEFT,
    "bottom-right": HT_BOTTOMRIGHT,
}

#: ``COLORREF`` (0x00BBGGRR) values matching the dark/light app themes.
BORDER_COLORS = {"dark": 0x003A2A21, "light": 0x00E9DFD8}

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    _LRESULT = ctypes.c_ssize_t
    _WPARAM = ctypes.c_size_t
    _LPARAM = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)

    class _MARGINS(ctypes.Structure):
        _fields_ = [
            ("cxLeftWidth", ctypes.c_int),
            ("cxRightWidth", ctypes.c_int),
            ("cyTopHeight", ctypes.c_int),
            ("cyBottomHeight", ctypes.c_int),
        ]

    class _MINMAXINFO(ctypes.Structure):
        _fields_ = [
            ("ptReserved", wintypes.POINT),
            ("ptMaxSize", wintypes.POINT),
            ("ptMaxPosition", wintypes.POINT),
            ("ptMinTrackSize", wintypes.POINT),
            ("ptMaxTrackSize", wintypes.POINT),
        ]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]

    _user32.CallWindowProcW.restype = _LRESULT
    _user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        _WPARAM,
        _LPARAM,
    ]
    _user32.SendMessageW.restype = _LRESULT
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.IsZoomed.restype = wintypes.BOOL
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]

    if hasattr(_user32, "SetWindowLongPtrW"):
        _set_long = _user32.SetWindowLongPtrW
        _get_long = _user32.GetWindowLongPtrW
    else:  # 32-bit Python
        _set_long = _user32.SetWindowLongW
        _get_long = _user32.GetWindowLongW

    _set_long.restype = _LRESULT
    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _get_long.restype = _LRESULT
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]

    _dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
    _dwmapi.DwmExtendFrameIntoClientArea.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_MARGINS),
    ]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]


def _pack_point(x: int, y: int) -> int:
    """Pack screen coordinates into a 32-bit ``LPARAM``."""
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def _dwm_set(hwnd: int, attribute: int, value: int) -> None:
    data = ctypes.c_int(value)
    try:
        _dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
        )
    except Exception:  # noqa: BLE001 - older Windows builds simply ignore this
        logger.debug("DwmSetWindowAttribute(0x%x) 失败", attribute, exc_info=True)


def _dwm_extend(hwnd: int) -> None:
    margins = _MARGINS(1, 1, 1, 1)
    try:
        _dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except Exception:  # noqa: BLE001
        logger.debug("DwmExtendFrameIntoClientArea 失败", exc_info=True)


class WindowChrome:
    """Native behaviour behind the HTML title bar of a frameless window."""

    def __init__(self, window: Any) -> None:
        self._window = window
        self._hwnd = 0
        self._old_proc: int | None = None
        self._proc: Any = None
        self._attached = False
        self._theme = "dark"
        try:
            window.events.shown += self._on_shown
        except Exception:  # noqa: BLE001 - never block window creation
            logger.exception("注册窗口显示事件失败")

    # ------------------------------------------------------------ lifecycle
    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def attached(self) -> bool:
        return self._attached

    def _on_shown(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            self._run_on_ui(self._attach)
        except Exception:  # noqa: BLE001
            logger.exception("启用自定义窗口失败")

    def _run_on_ui(self, func: Callable[[], None]) -> None:
        """Run ``func`` on the WinForms UI thread that owns the window."""
        form = getattr(self._window, "native", None)
        if form is None:
            return
        if not form.IsHandleCreated:
            return
        if form.InvokeRequired:
            from System import Action  # pythonnet is loaded by pywebview

            form.Invoke(Action(func))
        else:
            func()

    def _attach(self) -> None:
        form = self._window.native
        hwnd = int(form.Handle.ToInt32())
        self._hwnd = hwnd

        # A border-less WinForms form drops the min/max/system bits; restore
        # them so Win+Up, Win+Down and the taskbar menu keep working.
        # WS_THICKFRAME is required by the native sizing loop; the frame itself
        # is removed again by the WM_NCCALCSIZE handler below.
        style = int(_get_long(hwnd, GWL_STYLE)) & 0xFFFFFFFF
        _set_long(
            hwnd,
            GWL_STYLE,
            ctypes.c_void_p(
                style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU | WS_THICKFRAME
            ),
        )

        self._proc = _WNDPROC(self._dispatch)
        self._old_proc = int(
            _set_long(hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))
        )
        self._attached = True

        # DWM decoration: shadow, Windows 11 rounded corners, themed 1px edge.
        _dwm_extend(hwnd)
        _dwm_set(hwnd, DWMWA_NCRENDERING_POLICY, DWMNCRP_ENABLED)
        _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        self._apply_border(hwnd)

        # Recalculate the frame through our WM_NCCALCSIZE handler so the client
        # area keeps covering the whole window.
        _user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        logger.debug("自定义窗口已启用 (hwnd=%s)", hwnd)

    def _detach(self, hwnd: int) -> None:
        if self._old_proc:
            try:
                _set_long(hwnd, GWLP_WNDPROC, ctypes.c_void_p(self._old_proc))
            except Exception:  # noqa: BLE001
                logger.debug("恢复窗口过程失败", exc_info=True)
        self._attached = False

    # ------------------------------------------------------- window proc
    def _dispatch(self, hwnd, msg, wparam, lparam):  # noqa: ANN001 - ctypes signature
        try:
            if msg == WM_NCCALCSIZE and int(wparam):
                # Keep the proposed client rectangle (it equals the window
                # rect): this removes the native frame while WS_THICKFRAME
                # still enables the native sizing loop.
                return 0
            if msg == WM_GETMINMAXINFO:
                result = self._call_old(hwnd, msg, wparam, lparam)
                self._clamp_to_work_area(hwnd, lparam)
                return result
            if msg == WM_YHSH_MOVE:
                self._native_move(hwnd)
                return 0
            if msg == WM_YHSH_SIZE:
                self._native_resize(hwnd, int(wparam))
                return 0
            if msg == WM_YHSH_THEME:
                self._theme = "light" if int(wparam) else "dark"
                self._apply_border(hwnd)
                return 0
            if msg == WM_NCDESTROY:
                result = self._call_old(hwnd, msg, wparam, lparam)
                self._detach(hwnd)
                return result
        except Exception:  # noqa: BLE001 - never break the message loop
            logger.exception("自定义窗口消息处理失败 (0x%04x)", msg)
        return self._call_old(hwnd, msg, wparam, lparam)

    def _call_old(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        if not self._old_proc:
            return 0
        return _user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def _clamp_to_work_area(self, hwnd: int, lparam: int) -> None:
        """Maximise into ``rcWork`` instead of over the taskbar."""
        info = _MINMAXINFO.from_address(int(lparam))
        monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return
        mon = _MONITORINFO()
        mon.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(mon)):
            return
        work = mon.rcWork
        screen = mon.rcMonitor
        info.ptMaxPosition.x = work.left - screen.left
        info.ptMaxPosition.y = work.top - screen.top
        info.ptMaxSize.x = work.right - work.left
        info.ptMaxSize.y = work.bottom - work.top

    @staticmethod
    def _cursor_lparam() -> int:
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return _pack_point(point.x, point.y)

    def _native_move(self, hwnd: int) -> None:
        _user32.ReleaseCapture()
        _user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HT_CAPTION, self._cursor_lparam())

    def _native_resize(self, hwnd: int, hit: int) -> None:
        if hit not in EDGE_HIT_TEST.values() or _user32.IsZoomed(hwnd):
            return
        _user32.ReleaseCapture()
        _user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, hit, self._cursor_lparam())
        # The sizing loop may skip its final WM_NCCALCSIZE; one frame change
        # makes sure the client area really covers the whole window.
        _user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

    def _apply_border(self, hwnd: int) -> None:
        _dwm_set(hwnd, DWMWA_BORDER_COLOR, BORDER_COLORS.get(self._theme, BORDER_COLORS["dark"]))

    # -------------------------------------------------------------- public
    def start_drag(self) -> None:
        """Ask the UI thread to start the native window move loop."""
        if self._attached and self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_YHSH_MOVE, 0, 0)

    def start_resize(self, edge: str) -> None:
        """Ask the UI thread to start the native sizing loop for ``edge``."""
        hit = EDGE_HIT_TEST.get(str(edge or "").strip().lower())
        if hit is None or not self._attached or not self._hwnd:
            return
        _user32.PostMessageW(self._hwnd, WM_YHSH_SIZE, hit, 0)

    def is_maximized(self) -> bool:
        return bool(self._attached and self._hwnd and _user32.IsZoomed(self._hwnd))

    def toggle_maximize(self) -> bool:
        if not self._attached or not self._hwnd:
            return False
        if _user32.IsZoomed(self._hwnd) or _user32.IsIconic(self._hwnd):
            self._window.restore()
        else:
            self._window.maximize()
        return self.is_maximized()

    def set_theme(self, theme: str) -> None:
        self._theme = "light" if str(theme or "").strip().lower() == "light" else "dark"
        if self._attached and self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_YHSH_THEME, 1 if self._theme == "light" else 0, 0)
