"""Dependency-free Windows notification-area integration."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Any, ClassVar

from app.desktop.runtime import DesktopActions, Tray

LOGGER = logging.getLogger(__name__)


class WaitTray:
    """Headless fallback used outside Windows and by source-based development."""

    def __init__(self, _actions: DesktopActions) -> None:
        self._stopped = threading.Event()

    def run(self) -> None:
        try:
            self._stopped.wait()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._stopped.set()


def create_tray(actions: DesktopActions) -> Tray:
    if sys.platform == "win32":
        return WindowsTray(actions)
    return WaitTray(actions)


class WindowsTray:
    """A small Win32 tray icon with no third-party GUI runtime dependency."""

    _WM_TRAY = 0x8001
    _WM_COMMAND = 0x0111
    _WM_DESTROY = 0x0002
    _WM_CLOSE = 0x0010
    _WM_LBUTTONUP = 0x0202
    _WM_RBUTTONUP = 0x0205
    _NIM_ADD = 0x00000000
    _NIM_DELETE = 0x00000002
    _NIF_MESSAGE = 0x00000001
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004
    _MF_STRING = 0x00000000
    _MF_SEPARATOR = 0x00000800
    _TPM_RIGHTBUTTON = 0x0002
    _CMD_OPEN = 1001
    _CMD_UPDATE = 1002
    _CMD_LOGS = 1003
    _CMD_EXIT = 1004

    def __init__(self, actions: DesktopActions) -> None:
        self._actions = actions
        self._hwnd: int | None = None
        self._window_proc: Any | None = None

    def run(self) -> None:
        api = _Win32Api()
        self._window_proc = api.window_proc_type(self._dispatch)
        class_name = f"AIIntelligenceMonitorTray-{id(self)}"
        window_class = api.window_class_type()
        window_class.lpfnWndProc = self._window_proc
        window_class.hInstance = api.kernel32.GetModuleHandleW(None)
        window_class.lpszClassName = class_name
        atom = api.user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            raise OSError("Could not register the desktop tray window class")
        hwnd = api.user32.CreateWindowExW(
            0,
            class_name,
            "AI Intelligence Monitor",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            window_class.hInstance,
            None,
        )
        if not hwnd:
            raise OSError("Could not create the desktop tray window")
        self._hwnd = int(hwnd)
        notification = api.notification_data_type()
        notification.cbSize = ctypes.sizeof(notification)
        notification.hWnd = hwnd
        notification.uID = 1
        notification.uFlags = self._NIF_MESSAGE | self._NIF_ICON | self._NIF_TIP
        notification.uCallbackMessage = self._WM_TRAY
        notification.hIcon = api.user32.LoadIconW(None, ctypes.c_void_p(32512))
        notification.szTip = "AI 行业动态与成果申报情报工具"
        if not api.shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(notification)):
            raise OSError("Could not add the desktop notification-area icon")
        message = wintypes.MSG()
        try:
            while api.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                api.user32.TranslateMessage(ctypes.byref(message))
                api.user32.DispatchMessageW(ctypes.byref(message))
        finally:
            api.shell32.Shell_NotifyIconW(self._NIM_DELETE, ctypes.byref(notification))
            api.user32.DestroyWindow(hwnd)
            api.user32.UnregisterClassW(class_name, window_class.hInstance)
            self._hwnd = None

    def stop(self) -> None:
        if self._hwnd is not None:
            _Win32Api().user32.PostMessageW(self._hwnd, self._WM_CLOSE, 0, 0)

    def _dispatch(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        api = _Win32Api()
        if message == self._WM_TRAY and lparam in {self._WM_LBUTTONUP, self._WM_RBUTTONUP}:
            self._show_menu(hwnd, api)
            return 0
        if message == self._WM_COMMAND:
            command = wparam & 0xFFFF
            if command == self._CMD_OPEN:
                self._actions.open_page()
            elif command == self._CMD_UPDATE:
                self._actions.update_now()
            elif command == self._CMD_LOGS:
                self._actions.open_logs()
            elif command == self._CMD_EXIT:
                self._actions.exit()
            return 0
        if message == self._WM_CLOSE:
            api.user32.PostQuitMessage(0)
            return 0
        if message == self._WM_DESTROY:
            api.user32.PostQuitMessage(0)
            return 0
        return int(api.user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _show_menu(self, hwnd: int, api: _Win32Api) -> None:
        menu = api.user32.CreatePopupMenu()
        if not menu:
            LOGGER.error("Could not create the desktop tray menu")
            return
        try:
            api.user32.AppendMenuW(menu, self._MF_STRING, self._CMD_OPEN, "打开应用")
            api.user32.AppendMenuW(menu, self._MF_STRING, self._CMD_UPDATE, "立即更新")
            api.user32.AppendMenuW(menu, self._MF_STRING, self._CMD_LOGS, "打开日志目录")
            api.user32.AppendMenuW(menu, self._MF_SEPARATOR, 0, None)
            api.user32.AppendMenuW(menu, self._MF_STRING, self._CMD_EXIT, "退出")
            point = wintypes.POINT()
            api.user32.GetCursorPos(ctypes.byref(point))
            api.user32.SetForegroundWindow(hwnd)
            api.user32.TrackPopupMenu(
                menu,
                self._TPM_RIGHTBUTTON,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
        finally:
            api.user32.DestroyMenu(menu)


class _Win32Api:
    """Late-bound ctypes declarations so importing this module stays portable."""

    _type_lock: ClassVar[threading.Lock] = threading.Lock()
    _cached_types: ClassVar[tuple[Any, Any, Any] | None] = None

    def __init__(self) -> None:
        windll: Any = getattr(ctypes, "windll")  # noqa: B009
        self.user32 = windll.user32
        self.shell32 = windll.shell32
        self.kernel32 = windll.kernel32
        lresult = ctypes.c_ssize_t
        with self._type_lock:
            if self._cached_types is None:
                function_type: Any = getattr(ctypes, "WINFUNCTYPE")  # noqa: B009
                window_proc_type: Any = function_type(
                    lresult,
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )

                class WindowClass(ctypes.Structure):
                    _fields_ = [
                        ("style", wintypes.UINT),
                        ("lpfnWndProc", window_proc_type),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR),
                    ]

                class NotifyIconData(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON),
                    ]

                type(self)._cached_types = (
                    window_proc_type,
                    WindowClass,
                    NotifyIconData,
                )
        cached_types = self._cached_types
        assert cached_types is not None
        self.window_proc_type, self.window_class_type, self.notification_data_type = cached_types
        WindowClass = self.window_class_type
        NotifyIconData = self.notification_data_type
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        self.user32.LoadIconW.restype = wintypes.HICON
        self.shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(NotifyIconData),
        ]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = lresult
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.DefWindowProcW.restype = lresult
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.CreatePopupMenu.argtypes = []
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        ]
        self.user32.TrackPopupMenu.restype = wintypes.BOOL
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.DestroyMenu.restype = wintypes.BOOL
