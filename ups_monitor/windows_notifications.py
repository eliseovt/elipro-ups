"""Nonblocking Windows tray notifications; no browser or third-party packages.

The hidden window and Shell calls belong to one dedicated message-loop thread.
Shell accepting a balloon does not prove the user saw it: Windows can suppress
notifications. Real-time balloons are discarded rather than deferred when they
cannot be shown immediately, so an old outage cannot appear after recovery.
Sound is requested separately, with the Shell sound disabled; its own failure
does not turn an accepted visual request into a failed visual notification.

Win32 contracts: https://learn.microsoft.com/windows/win32/api/shellapi/ns-shellapi-notifyicondataw
https://learn.microsoft.com/windows/win32/shell/notification-area
https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate
"""

from __future__ import annotations

import ctypes
from collections import OrderedDict, deque
from ctypes import wintypes
from datetime import datetime, timezone
import os
import queue
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from .runtime import resource_root
from .notification_priority import age_notice_metadata, normalize_notice_metadata
from .notification_popup import PopupView, countdown_presentation


WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A
WM_THEMECHANGED = 0x031A
WM_DPICHANGED = 0x02E0
WM_SETFONT = 0x0030
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP_NOTIFY = 0x8001
WM_APP_TRAY = 0x8002
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIN_BALLOONSHOW = 0x0402
NIN_BALLOONHIDE = 0x0403
NIN_BALLOONTIMEOUT = 0x0404
NIN_BALLOONUSERCLICK = 0x0405
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_REALTIME, NIF_SHOWTIP = 1, 2, 4, 16, 64, 128
NIIF_NOSOUND = 0x10
SEVERITIES = {"info": (1, "SystemAsterisk"),
              "warning": (2, "SystemExclamation"),
              "critical": (3, "SystemHand"), "error": (3, "SystemHand")}
USER_NOTIFICATION_STATES = {
    1: "not_present", 2: "busy", 3: "fullscreen", 4: "presentation_mode",
    5: "accepts_notifications", 6: "quiet_time", 7: "app",
}
DEFAULT_TRAY_TIP = "Elipro UPS · monitor activo"
DEFAULT_EXIT_LABEL = "Salir de Elipro…"
DEFAULT_EXIT_MESSAGE = (
    "Salir detendrá la lectura del UPS, los avisos y la protección de Elipro. "
    "Windows seguirá encendido. ¿Quieres salir de Elipro?"
)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
COUNTDOWN_REFRESH_GRACE_SECONDS = 4

# Fixed-width Windows scalar types also permit import on Unix for test discovery.
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
LONG = ctypes.c_int32
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    LRESULT, ctypes.c_void_p, UINT, WPARAM, LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _TimeoutVersion(ctypes.Union):
    _fields_ = [("uTimeout", UINT), ("uVersion", UINT)]


class NOTIFYICONDATAW(ctypes.Structure):
    _anonymous_ = ("timeout_version",)
    _fields_ = [("cbSize", DWORD), ("hWnd", ctypes.c_void_p), ("uID", UINT),
                ("uFlags", UINT), ("uCallbackMessage", UINT),
                ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
                ("dwState", DWORD), ("dwStateMask", DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("timeout_version", _TimeoutVersion),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", DWORD),
                ("guidItem", GUID), ("hBalloonIcon", ctypes.c_void_p)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", UINT),
                ("wParam", WPARAM), ("lParam", LPARAM), ("time", DWORD),
                ("pt", POINT), ("lPrivate", DWORD)]


def _bind(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes = args
    function.restype = result
    return function


def _load_apis():
    if os.name != "nt":
        raise OSError("Los avisos de escritorio requieren Windows")
    user = ctypes.WinDLL("user32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr, text = ctypes.c_void_p, ctypes.c_wchar_p
    _bind(kernel, "GetModuleHandleW", [text], ptr)
    _bind(user, "RegisterClassW", [ctypes.POINTER(WNDCLASSW)], ctypes.c_uint16)
    _bind(user, "UnregisterClassW", [text, ptr], wintypes.BOOL)
    _bind(user, "CreateWindowExW", [DWORD, text, text, DWORD, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, ptr, ptr, ptr, ptr], ptr)
    _bind(user, "DefWindowProcW", [ptr, UINT, WPARAM, LPARAM], LRESULT)
    _bind(user, "DestroyWindow", [ptr], wintypes.BOOL)
    _bind(user, "LoadIconW", [ptr, ptr], ptr)
    _bind(user, "LoadImageW", [ptr, text, UINT, ctypes.c_int, ctypes.c_int, UINT], ptr)
    _bind(user, "DestroyIcon", [ptr], wintypes.BOOL)
    _bind(user, "RegisterWindowMessageW", [text], UINT)
    _bind(user, "PostMessageW", [ptr, UINT, WPARAM, LPARAM], wintypes.BOOL)
    _bind(user, "PostQuitMessage", [ctypes.c_int], None)
    _bind(user, "GetMessageW", [ctypes.POINTER(MSG), ptr, UINT, UINT], ctypes.c_int)
    _bind(user, "TranslateMessage", [ctypes.POINTER(MSG)], wintypes.BOOL)
    _bind(user, "DispatchMessageW", [ctypes.POINTER(MSG)], LRESULT)
    _bind(user, "IsDialogMessageW", [ptr, ctypes.POINTER(MSG)], wintypes.BOOL)
    _bind(user, "CreatePopupMenu", [], ptr)
    _bind(user, "AppendMenuW", [ptr, UINT, ctypes.c_size_t, text], wintypes.BOOL)
    _bind(user, "GetCursorPos", [ctypes.POINTER(POINT)], wintypes.BOOL)
    _bind(user, "SetForegroundWindow", [ptr], wintypes.BOOL)
    _bind(user, "TrackPopupMenu", [ptr, UINT, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ptr, ptr], UINT)
    _bind(user, "DestroyMenu", [ptr], wintypes.BOOL)
    _bind(user, "MessageBoxW", [ptr, text, text, UINT], ctypes.c_int)
    _bind(user, "SetWindowTextW", [ptr, text], wintypes.BOOL)
    _bind(user, "ShowWindow", [ptr, ctypes.c_int], wintypes.BOOL)
    _bind(user, "EnableWindow", [ptr, wintypes.BOOL], wintypes.BOOL)
    _bind(user, "SetWindowPos", [ptr, ptr, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, UINT], wintypes.BOOL)
    _bind(user, "SystemParametersInfoW", [UINT, UINT, ptr, UINT], wintypes.BOOL)
    _bind(user, "SetTimer", [ptr, ctypes.c_size_t, UINT, ptr], ctypes.c_size_t)
    _bind(user, "KillTimer", [ptr, ctypes.c_size_t], wintypes.BOOL)
    _bind(user, "SendMessageW", [ptr, UINT, WPARAM, LPARAM], LRESULT)
    _bind(user, "MonitorFromWindow", [ptr, DWORD], ptr)
    _bind(user, "GetMonitorInfoW", [ptr, ctypes.POINTER(MONITORINFO)], wintypes.BOOL)
    _bind(user, "RedrawWindow", [ptr, ptr, ptr, UINT], wintypes.BOOL)
    for name, args, result in (
            ("SetProcessDpiAwarenessContext", [ptr], wintypes.BOOL),
            ("SetProcessDPIAware", [], wintypes.BOOL),
            ("GetDpiForWindow", [ptr], UINT), ("GetDpiForSystem", [], UINT)):
        try:
            _bind(user, name, args, result)
        except AttributeError:
            pass
    _bind(shell, "Shell_NotifyIconW", [DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL)
    try:
        _bind(shell, "SHQueryUserNotificationState", [ctypes.POINTER(ctypes.c_int)], LONG)
    except AttributeError:
        pass  # Optional diagnostic must not prevent notification delivery.
    return user, shell, kernel


def enable_per_monitor_dpi(user):
    """Request PMv2 before creating any HWND; tolerate manifest/older OS state."""
    try:
        if user.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return {"mode": "per_monitor_v2", "error": None}
        error = ctypes.get_last_error()
        if error == 5:  # Manifest or an earlier host initialization already set it.
            return {"mode": "already_configured", "error": None}
    except (AttributeError, OSError):
        pass
    try:
        if user.SetProcessDPIAware():
            return {"mode": "system_aware_fallback", "error": None}
    except (AttributeError, OSError):
        pass
    return {"mode": "unchanged", "error": "Windows no permitió configurar la escala DPI"}


def _load_font_apis():
    try:
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        _bind(gdi, "CreateFontW", [ctypes.c_int] * 5 + [DWORD] * 8 + [ctypes.c_wchar_p], ctypes.c_void_p)
        _bind(gdi, "DeleteObject", [ctypes.c_void_p], wintypes.BOOL)
        return gdi
    except (AttributeError, OSError):
        return None


def _utf16_clip(value, units):
    """Clip for WCHAR arrays without cutting a surrogate pair or retaining NULs."""
    return str(value).replace("\0", "").encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")


class _Win32Backend:
    supports_delivery_validation = True
    supports_notification_priority = True

    def __init__(self, owner):
        self.owner = owner
        self.user, self.shell, self.kernel = _load_apis()
        self.owner._set_dpi_status(enable_per_monitor_dpi(self.user))
        self.gdi = _load_font_apis()
        self.hwnd = None
        self.icon_added = False
        self.icon_handle = None
        self.owns_icon = False
        self.class_registered = False
        self.popup_hwnd = None
        self.popup_title = None
        self.popup_body = None
        self.popup_buttons = ()
        self.popup_cancel = None
        self.popup_dpi = 96
        self.popup_font = None
        self.popup_view = None
        self.popup_countdown = None
        self.popup_badge = None
        self.popup_has_countdown = False
        self.popup_width = 480
        self.popup_height = 360
        self._popup_theme = None
        self._popup_layout_signature = None
        self._popup_position = None
        self._popup_visible = False
        self._popup_refresh_interval = None
        self.popup_persistent_key = None
        self.last_notice = None
        self._shown_keys = set()
        self.user_notification_code = None
        self.class_name = f"EliproNotifications-{os.getpid()}-{id(self)}"
        self.instance = self.kernel.GetModuleHandleW(None)
        # Keep this callback alive until the window has been destroyed.
        self.window_proc = WNDPROC(self._window_proc)
        self.taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")
        if not self.taskbar_created:
            raise OSError("No se pudo registrar TaskbarCreated")

    def _data(self):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        return data

    def _add_icon(self):
        data = self._data()
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        data.uCallbackMessage = WM_APP_TRAY
        data.hIcon = self._load_icon()
        data.szTip = _utf16_clip(self._owner_text("tray_tip", DEFAULT_TRAY_TIP), 127)
        if not data.hIcon or not self.shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            self.icon_added = False
            raise OSError("Windows no aceptó el icono de Elipro")
        self.icon_added = True
        data.uVersion = 4
        if not self.shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data)):
            raise OSError("Windows no aceptó la versión del icono de Elipro")

    def _owner_text(self, name, fallback):
        value = getattr(self.owner, name, None)
        return value if isinstance(value, str) and value else fallback

    def _load_icon(self):
        if self.icon_handle:
            return self.icon_handle
        # No LR_SHARED: this file-backed handle belongs to this backend. Retain
        # it through Explorer restarts and release it only after tray/window use.
        path = resource_root() / "static" / "assets" / "elipro.ico"
        try:
            handle = self.user.LoadImageW(None, str(path), 1, 0, 0, 0x0010 | 0x0040)
        except (OSError, AttributeError):
            handle = None
        if isinstance(handle, int) and handle:
            self.icon_handle, self.owns_icon = handle, True
        else:
            # IDI_INFORMATION is shared and must never be passed to DestroyIcon.
            self.icon_handle = self.user.LoadIconW(None, ctypes.c_void_p(32516))
            self.owns_icon = False
        return self.icon_handle

    def _release_icon(self):
        if self.owns_icon and self.icon_handle:
            self.user.DestroyIcon(self.icon_handle)
        self.icon_handle, self.owns_icon = None, False

    def initialize(self):
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self.window_proc
        window_class.hInstance = self.instance
        window_class.hbrBackground = ctypes.c_void_p(6)  # Shared COLOR_WINDOW brush
        window_class.lpszClassName = self.class_name
        if not self.user.RegisterClassW(ctypes.byref(window_class)):
            raise OSError("No se pudo registrar la ventana de avisos")
        self.class_registered = True
        # A hidden top-level window receives TaskbarCreated broadcasts. A
        # message-only HWND would miss Explorer restarts.
        self.hwnd = self.user.CreateWindowExW(0, self.class_name, "Elipro UPS", 0,
                                             0, 0, 0, 0, None, None, self.instance, None)
        if not self.hwnd:
            raise OSError("No se pudo crear la ventana de avisos")
        self._add_icon()
        self._query_user_notification_state()
        self.owner._set_popup_available(True)

    def _query_user_notification_state(self):
        """Read-only diagnostic, sampled at startup and notification requests.

        It is advisory and never alters system settings or suppresses our own
        request; Windows still applies its notification policy. The timestamp
        prevents a cached fullscreen/quiet state being mistaken for live data.
        """
        state = ctypes.c_int()
        diagnostic = {"state": "unknown", "code": None,
                      "checked_at": datetime.now(timezone.utc).isoformat(), "error": None}
        try:
            result = self.shell.SHQueryUserNotificationState(ctypes.byref(state))
            if result != 0:
                diagnostic["error"] = f"SHQueryUserNotificationState: HRESULT 0x{result & 0xFFFFFFFF:08X}"
            else:
                diagnostic.update(state=USER_NOTIFICATION_STATES.get(state.value, "unknown"), code=state.value)
        except Exception as exc:
            diagnostic["error"] = str(exc)
        self.owner._set_user_notification_state(diagnostic)
        self.user_notification_code = diagnostic["code"]

    def wake(self):
        return bool(self.hwnd and self.user.PostMessageW(self.hwnd, WM_APP_NOTIFY, 0, 0))

    def request_stop(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def run(self):
        self.owner._drain(self)
        msg = MSG()
        while True:
            result = self.user.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                return
            if result == -1:
                raise OSError("Falló el bucle de avisos de Windows")
            if self.popup_hwnd and self.user.IsDialogMessageW(self.popup_hwnd, ctypes.byref(msg)):
                continue
            self.user.TranslateMessage(ctypes.byref(msg))
            self.user.DispatchMessageW(ctypes.byref(msg))

    def deliver(self, title, message, severity, sound, persistent=False, key=None, generation=None,
                *, validity_callback=None):
        if not self.icon_added:
            raise OSError("El icono de avisos no está disponible")
        def current():
            return (self.owner._session_currently_available() is not False and
                    (validity_callback is None or validity_callback() is True))
        if not current():
            return False
        if persistent and not self.owner._persistent_active(key, generation):
            return False
        self._query_user_notification_state()
        if not persistent and self.popup_persistent_key is not None and self.user_notification_code == 5:
            return False  # A transient must not sound over the dominant incident.
        if persistent and self.user_notification_code == 5:
            winner = self.owner._next_persistent(self.popup_persistent_key)
            if winner is not None and winner[0] != key:
                self.sync_persistent()
                return False  # Deferred is not a native delivery receipt.
        data = self._data()
        data.uFlags = NIF_INFO | NIF_REALTIME
        data.szInfoTitle = _utf16_clip(title, 63)
        data.szInfo = _utf16_clip(message, 255)
        data.dwInfoFlags = SEVERITIES[severity][0] | NIIF_NOSOUND
        self.last_notice = {"title": title, "message": message, "requested_at": time.monotonic(),
                            "severity": severity,
                            "clicked": False, "popup_handled": bool(persistent), "shell_shown": False,
                            "persistent_key": key if persistent else None,
                            "validity_callback": validity_callback}
        accepted = False
        if not current():
            return False
        if self.user_notification_code == 5:
            # All notices use the branded window on an available desktop.
            # Windows still owns delivery policy in quiet/fullscreen states.
            # Never add a balloon or Shell fallback timer beside our own view.
            self.user.KillTimer(self.hwnd, 2)
            if not persistent:
                accepted = self._show_popup(title, message, validity_callback=validity_callback) is True
            elif not self.owner._persistent_acknowledged(key):
                current_notice = self.owner._persistent_message(key)
                if current_notice is not None:
                    accepted = self._show_popup(current_notice["title"], current_notice["message"], persistent=True, key=key,
                                                validity_callback=validity_callback) is True
            if accepted:
                self.last_notice["popup_handled"] = True
        elif not self.shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            self.last_notice = None
            raise OSError("Windows no aceptó el aviso de Elipro")
        else:
            accepted = True
        if not accepted or not current():
            return False
        if persistent and self.user_notification_code == 5:
            notice = self.owner._persistent_message(key) or {}
            notice.setdefault("severity", severity)
            self._persistent_sound(key, notice, requested=sound)
        elif sound:
            self._play_sound(severity)
        return True

    def _play_sound(self, severity):
        try:
            import winsound
            winsound.PlaySound(SEVERITIES.get(severity, SEVERITIES["info"])[1],
                               winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            self.owner._set_sound_error(None)
        except Exception as exc:
            self.owner._set_sound_error(str(exc))

    def _persistent_sound(self, key, notice, *, requested=None):
        if self.popup_persistent_key != key:
            return
        enabled = notice.get("sound", requested is True)
        if not enabled or notice.get("confirmed", True) is not True:
            return
        now = time.monotonic()
        previous = self.owner._persistent_sound_at.get(key)
        if previous is not None and (requested is None or now - previous < notice.get("repeat_seconds", 30)):
            return
        self.owner._persistent_sound_at[key] = now
        self._play_sound(notice.get("severity", "critical"))

    def _window_dpi(self, hwnd):
        for name, args in (("GetDpiForWindow", (hwnd,)), ("GetDpiForSystem", ())):
            try:
                value = getattr(self.user, name)(*args)
                if type(value) is int and 48 <= value <= 768:
                    return value
            except (OSError, AttributeError):
                pass
        return 96

    def _scale(self, value):
        return max(1, round(value * self.popup_dpi / 96))

    def _set_popup_font(self):
        if self.popup_view is not None:
            self.popup_view.set_fonts()
            self.popup_font = self.popup_view.fonts.get("body")

    def _popup_workarea(self):
        # Follow the popup's monitor after a user/DPI move. Before first show,
        # MonitorFromWindow chooses the primary monitor for our hidden owner.
        monitor = self.user.MonitorFromWindow(self.popup_hwnd or self.hwnd, 2)
        if isinstance(monitor, int) and monitor:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                if info.rcWork.right > info.rcWork.left and info.rcWork.bottom > info.rcWork.top:
                    return info.rcWork
        work = RECT()
        if (not self.user.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
                or work.right <= work.left or work.bottom <= work.top):
            raise OSError("No se pudo localizar el área de trabajo para el aviso")
        return work

    def _apply_popup_theme(self, *, force=False):
        if self.popup_view is not None:
            preference = getattr(self.owner, "notification_theme", "auto")
            preference = preference if preference in ("auto", "light", "dark") else "auto"
            self.popup_view.apply_theme(preference)
            if force or preference != self._popup_theme:
                self.user.RedrawWindow(self.popup_hwnd, None, None, 0x0001 | 0x0004 | 0x0080)
            self._popup_theme = preference

    def _layout_popup(self, dpi, work=None):
        if self.popup_view is None:
            return False
        work = work if work is not None else self._popup_workarea()
        signature = (dpi, work.left, work.top, work.right, work.bottom, self.popup_has_countdown)
        if signature == self._popup_layout_signature and not self.popup_view.layout_dirty:
            return False
        self.popup_dpi = dpi
        self._set_popup_font()
        available_width = (work.right - work.left) * 96 // dpi
        available_height = (work.bottom - work.top) * 96 // dpi
        self.popup_width = min(480, max(1, available_width - 32))
        self.popup_height = self.popup_view.layout(self.popup_has_countdown,
            width=self.popup_width, max_height=max(1, available_height - 32))
        self._popup_layout_signature = signature
        return True

    def _position_popup(self, work=None, *, show=False):
        work = work if work is not None else self._popup_workarea()
        width = min(self._scale(self.popup_width), work.right - work.left)
        height = min(self._scale(self.popup_height), work.bottom - work.top)
        left = max(work.left, work.right - width - self._scale(16))
        top = max(work.top, work.bottom - height - self._scale(16))
        position = (left, top, width, height, work.left, work.top, work.right, work.bottom)
        if position == self._popup_position and (not show or self._popup_visible):
            return False
        if not self.user.SetWindowPos(self.popup_hwnd, ctypes.c_void_p(-1),
                left, top, width, height,
                0x0010 | (0x0040 if show else 0)):
            raise OSError("Windows no pudo mostrar el aviso de Elipro")
        self._popup_position = position
        self._popup_visible = self._popup_visible or show
        return True

    def _set_popup_refresh_timer(self, interval):
        if interval == self._popup_refresh_interval:
            return
        if interval is None:
            if self.popup_hwnd:
                self.user.KillTimer(self.popup_hwnd, 3)
            self._popup_refresh_interval = None
            return
        if not self.user.SetTimer(self.popup_hwnd, 3, interval, None):
            self._hide_popup()
            raise OSError("No se pudo programar la actualización del aviso de Elipro")
        self._popup_refresh_interval = interval

    def _create_popup(self):
        # A tool window stays out of the taskbar; NOACTIVATE preserves whichever
        # application the user is working in. The DPI-specific font is owned.
        self.popup_dpi = self._window_dpi(self.hwnd)
        self.popup_hwnd = self.user.CreateWindowExW(
            0x08000000 | 0x00000080 | 0x00000008 | 0x00010000, self.class_name, "Aviso de Elipro UPS",
            0x80000000 | 0x00800000 | 0x02000000, 0, 0,
            self._scale(480), self._scale(360), None, None, self.instance, None)
        if not self.popup_hwnd:
            raise OSError("No se pudo crear el aviso de escritorio de Elipro")
        try:
            self.popup_view = PopupView(self)
            controls = self.popup_view.create_controls()
            self.popup_title, self.popup_body = controls["title"], controls["body"]
            self.popup_countdown, self.popup_badge = controls["countdown"], controls["badge"]
            self.popup_cancel = controls["cancel"]
            self.popup_buttons = (controls["open"], controls["ack"], self.popup_cancel)
            self._apply_popup_theme()
            self._set_popup_font()
        except Exception:
            self._destroy_popup()
            raise

    def _show_popup(self, title, message, persistent=False, key=None, *, validity_callback=None):
        if self.owner._stop.is_set() or self.owner._session_currently_available() is False:
            return False
        previous_key = self.popup_persistent_key
        if self.popup_persistent_key is not None and key != self.popup_persistent_key:
            current = self.owner._persistent_message(self.popup_persistent_key)
            incoming = self.owner._persistent_message(key) if persistent else None
            if (not persistent or incoming is None or current is not None
                    and not self.owner._persistent_acknowledged(self.popup_persistent_key)
                    and incoming.get("priority", 0) <= current.get("priority", 0)):
                return False
        if not self.popup_hwnd:
            self._create_popup()
        dpi = self._window_dpi(self.popup_hwnd)
        self._apply_popup_theme()
        notice = self.owner._persistent_message(key) if persistent else None
        if notice is not None:
            self._update_persistent_contents(notice, reposition=False)
        else:
            self.popup_has_countdown = False
            self.popup_view.set_content(_utf16_clip(title, 100), _utf16_clip(message, 400),
                severity=(self.last_notice or {}).get("severity", "warning"),
                footer="Los incidentes se conservan en el historial.")
        work = self._popup_workarea()
        self._layout_popup(dpi, work)
        if validity_callback is not None and validity_callback() is not True:
            return False
        # HWND_TOPMOST=-1; SHOWWINDOW | NOACTIVATE. A new warning replaces the
        # previous one and refreshes the same timer rather than stacking windows.
        self._position_popup(work, show=True)
        self.popup_persistent_key = key if persistent else None
        self.user.KillTimer(self.popup_hwnd, 1)
        if not persistent:
            self._set_popup_refresh_timer(None)
            if not self.user.SetTimer(self.popup_hwnd, 1, 12000, None):
                self._hide_popup()
                raise OSError("No se pudo programar el cierre del aviso de Elipro")
        self.owner._record_popup_event("shown")
        if persistent:
            notice = self.owner._persistent_message(key) or {}
            if key in self._shown_keys and previous_key != key:
                # Restoring an earlier incident does not produce another sound.
                self.owner._persistent_sound_at[key] = time.monotonic()
            self._shown_keys.add(key)
            if self.popup_has_countdown or "priority" in notice:
                self._set_popup_refresh_timer(100 if self.popup_has_countdown else 1000)
                self._persistent_sound(key, notice)
            else:
                self._set_popup_refresh_timer(None)
        return True

    def _hide_popup(self):
        if self.popup_hwnd:
            self.user.KillTimer(self.popup_hwnd, 1)
            self.user.KillTimer(self.popup_hwnd, 3)
            self.user.ShowWindow(self.popup_hwnd, 0)
            self.owner._record_popup_event("hidden")
        self.popup_persistent_key = None
        self._popup_refresh_interval = None
        self._popup_position = None
        self._popup_layout_signature = None
        self._popup_visible = False

    def _update_persistent_contents(self, notice, *, reposition=True):
        title, message = notice["title"], notice["message"]
        countdown = notice.get("countdown")
        footer = "Entendido confirma la lectura de este aviso."
        self.popup_has_countdown = countdown is not None
        if countdown is not None:
            current = (notice.get("confirmed", True) is True and
                       countdown["valid_for_ms"] > 0 and countdown["remaining_seconds"] > 0)
            if not current:
                countdown = {**countdown, "valid_for_ms": 0}
            presentation = countdown_presentation(countdown)
            title, message, footer = presentation["title"], presentation["message"], presentation["footer"]
            if current and not countdown["cancelable"]:
                message += " El monitor no permite cancelarla."
            feedback, inflight = self.owner._countdown_feedback(countdown["countdown_id"])
            if feedback:
                message += " " + feedback
            if self.popup_cancel:
                self.popup_view.set_action("cancel", "Cancelando…" if inflight else "Cancelar protección",
                    bool(self.owner._countdown_cancel_request_allowed(notice) and not inflight))
        self.popup_view.set_content(_utf16_clip(title, 100), _utf16_clip(message, 400),
            severity=notice.get("severity", "warning"), countdown=countdown, footer=footer)
        if reposition:
            work = self._popup_workarea()
            self._layout_popup(self.popup_dpi, work)
            self._position_popup(work)
            self._set_popup_refresh_timer((100 if self.popup_has_countdown else 1000)
                if self.popup_has_countdown or "priority" in notice else None)

    def sync_persistent(self):
        self._shown_keys.intersection_update(self.owner._persistent_keys())
        if self.last_notice is not None:
            validator = self.last_notice.get("validity_callback")
            if validator is not None and validator() is not True:
                key = self.last_notice.get("persistent_key")
                self.last_notice = None
                self.user.KillTimer(self.hwnd, 2)
                if self.popup_persistent_key == key:
                    self._hide_popup()
        key = self.popup_persistent_key
        if key is None:
            notice = self.owner._next_persistent()
            if notice is not None and notice[0].startswith("notice:"):
                self._query_user_notification_state()
                if self.user_notification_code == 5:
                    self._show_popup(notice[1]["title"], notice[1]["message"], persistent=True, key=notice[0])
            return
        notice = self.owner._persistent_message(key)
        if notice is None or self.owner._persistent_acknowledged(key):
            self._hide_popup()
            if self.last_notice and self.last_notice.get("persistent_key") == key:
                self.last_notice = None
                self.user.KillTimer(self.hwnd, 2)
            # Acknowledging one item reveals the next unread incident.
            self.sync_persistent()
            return
        winner = self.owner._next_persistent(key)
        if winner is not None and winner[0] != key:
            self._query_user_notification_state()
            if self.user_notification_code == 5:
                self._show_popup(winner[1]["title"], winner[1]["message"], persistent=True, key=winner[0])
            return
        self._update_persistent_contents(notice)

    def _destroy_popup(self):
        if self.popup_hwnd:
            hwnd = self.popup_hwnd
            self.user.KillTimer(hwnd, 1)
            self.user.KillTimer(hwnd, 3)
            self.user.DestroyWindow(hwnd)
            self.popup_hwnd = None
            self.popup_title = self.popup_body = None
            self.popup_buttons = ()
            self.popup_cancel = None
            self.popup_countdown = self.popup_badge = None
        if self.popup_view is not None:
            self.popup_view.close()
            self.popup_view = None
        self.popup_font = None
        self._popup_theme = None
        self.popup_has_countdown = False
        self.popup_persistent_key = None
        self._popup_refresh_interval = None
        self._popup_position = None
        self._popup_layout_signature = None
        self._popup_visible = False

    def suspend_session(self):
        self.user.KillTimer(self.hwnd, 2)
        self.last_notice = None
        self._hide_popup()

    def _maybe_fallback_popup(self):
        notice = self.last_notice
        if (not notice or notice["clicked"] or notice["popup_handled"]
                or self.owner._stop.is_set() or self.user_notification_code != 5):
            return
        validator = notice.get("validity_callback")
        if validator is not None and validator() is not True:
            notice["popup_handled"] = True
            self.user.KillTimer(self.hwnd, 2)
            return
        age = time.monotonic() - notice["requested_at"]
        if not 0 <= age < 1:
            return
        notice["popup_handled"] = True
        self.user.KillTimer(self.hwnd, 2)
        self._show_popup(notice["title"], notice["message"], validity_callback=validator)

    def _check_unconfirmed_popup(self):
        self.user.KillTimer(self.hwnd, 2)
        notice = self.last_notice
        if (not notice or notice["shell_shown"] or notice["clicked"]
                or notice["popup_handled"] or self.owner._stop.is_set()):
            return
        validator = notice.get("validity_callback")
        if validator is not None and validator() is not True:
            notice["popup_handled"] = True
            return
        elapsed = time.monotonic() - notice["requested_at"]
        if elapsed < 1:
            # A queued timer from the previous warning must not trigger an
            # early popup for a newly submitted warning.
            self.user.SetTimer(self.hwnd, 2, max(1, int((1 - elapsed) * 1000)), None)
            return
        self._query_user_notification_state()
        if self.user_notification_code != 5:
            return
        notice["popup_handled"] = True
        self._show_popup(notice["title"], notice["message"], validity_callback=validator)

    def _menu(self):
        menu = self.user.CreatePopupMenu()
        if not menu:
            raise OSError("No se pudo abrir el menú de Elipro")
        try:
            self.user.AppendMenuW(menu, 0, 1, "Abrir Elipro")
            if self.owner.cancel_callback is not None:
                self.user.AppendMenuW(menu, 0, 2, "Cancelar acción pendiente")
            if self.owner.exit_callback is not None:
                self.user.AppendMenuW(menu, 0x0800, 0, None)
                self.user.AppendMenuW(menu, 0, 3, self._owner_text("exit_label", DEFAULT_EXIT_LABEL))
            point = POINT()
            if not self.user.GetCursorPos(ctypes.byref(point)):
                raise OSError("No se pudo localizar el menú de Elipro")
            self.user.SetForegroundWindow(self.hwnd)
            command = self.user.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y,
                                              0, self.hwnd, None)  # RETURNCMD | RIGHTBUTTON
            self.user.PostMessageW(self.hwnd, 0, 0, 0)
            if command == 1:
                self.owner._open_panel()
            elif (command == 2 and self.owner.cancel_callback is not None
                  and not self.owner._stop.is_set()):
                self.owner.cancel_callback()
            elif (command == 3 and self.owner.exit_callback is not None
                  and not self.owner._stop.is_set()):
                exit_label = self._owner_text("exit_label", DEFAULT_EXIT_LABEL)
                caption = "Salir de Elipro UPS" if exit_label == DEFAULT_EXIT_LABEL else exit_label.rstrip("…")
                result = self.user.MessageBoxW(self.hwnd,
                    self._owner_text("exit_message", DEFAULT_EXIT_MESSAGE),
                    caption, 0x0004 | 0x0030 | 0x0100)  # YESNO | WARNING | default NO
                if result == 6 and not self.owner._stop.is_set():
                    self.owner._request_exit()
        finally:
            self.user.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if self.popup_hwnd and hwnd == self.popup_hwnd:
                if self.popup_view is not None:
                    painted = self.popup_view.handle_message(message, wparam, lparam)
                    if painted is not None:
                        return painted
                if message == 0x0021:  # WM_MOUSEACTIVATE: user interaction may activate it.
                    return 1  # MA_ACTIVATE; showing it still never steals focus.
                if message == WM_DPICHANGED:
                    dpi = wparam & 0xFFFF
                    if not 48 <= dpi <= 768 or not lparam:
                        return 0
                    suggested = ctypes.cast(lparam, ctypes.POINTER(RECT)).contents
                    width, height = suggested.right - suggested.left, suggested.bottom - suggested.top
                    if width <= 0 or height <= 0:
                        return 0
                    self._popup_layout_signature = None
                    self._popup_position = None
                    if not self.user.SetWindowPos(hwnd, ctypes.c_void_p(-1), suggested.left,
                                                  suggested.top, width, height, 0x0010 | 0x0004):
                        raise OSError("Windows no pudo aplicar el cambio de DPI del aviso")
                    work = self._popup_workarea()
                    self._layout_popup(dpi, work)
                    self._position_popup(work)
                    return 0
                if message == WM_COMMAND:
                    command = wparam & 0xFFFF
                    if command == 101:
                        if self.popup_persistent_key is None:
                            self._hide_popup()
                        if not self.owner._stop.is_set():
                            self.owner._open_panel()
                        return 0
                    if command == 102:
                        if self.popup_persistent_key is not None:
                            self.owner._ack_persistent(self.popup_persistent_key)
                            self.sync_persistent()
                        else:
                            self._hide_popup()
                        return 0
                    if command == 103:
                        if self.popup_persistent_key is not None:
                            self.owner._cancel_persistent_countdown(self.popup_persistent_key)
                            self.sync_persistent()
                        return 0
                elif message == WM_TIMER and wparam == 3:
                    self.sync_persistent()
                    return 0
                elif message in (WM_CLOSE, WM_TIMER):
                    if self.popup_persistent_key is not None:
                        return 0  # Only the explicit Entendido button acknowledges.
                    self._hide_popup()
                    return 0
                elif message == WM_DESTROY:
                    # Closing this auxiliary window never ends the main loop.
                    return 0
            if message == self.taskbar_created:
                self.icon_added = False
                self._add_icon()
                self.owner._set_ready(True)
                return 0
            if message == WM_APP_NOTIFY:
                self.owner._drain(self)
                return 0
            if message == WM_TIMER and wparam == 2:
                self._check_unconfirmed_popup()
                return 0
            if message in (WM_SETTINGCHANGE, WM_THEMECHANGED):
                self._apply_popup_theme(force=True)
                if message == WM_SETTINGCHANGE:
                    self._query_user_notification_state()
                    if self.user_notification_code != 5 and self.popup_persistent_key is not None:
                        self._hide_popup()
                return 0
            if message == WM_APP_TRAY:
                event = lparam & 0xFFFF  # NOTIFYICON_VERSION_4
                display_event = {NIN_BALLOONSHOW: "shown", NIN_BALLOONHIDE: "hidden",
                                 NIN_BALLOONTIMEOUT: "timeout", NIN_BALLOONUSERCLICK: "clicked"}.get(event)
                if display_event:
                    self.owner._record_display_event(display_event)
                if event == NIN_BALLOONSHOW and self.last_notice:
                    self.last_notice["shell_shown"] = True
                if event == NIN_BALLOONUSERCLICK and self.last_notice:
                    self.last_notice["clicked"] = True
                elif event == NIN_BALLOONTIMEOUT:
                    self._maybe_fallback_popup()
                if event in (NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK):
                    self.owner._open_panel()
                elif event in (WM_CONTEXTMENU, WM_RBUTTONUP):
                    self._menu()
                return 0
            if message == WM_CLOSE:
                self.user.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self.user.KillTimer(hwnd, 2)
                if self.icon_added:
                    data = self._data()
                    data.hWnd = hwnd
                    self.shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                    self.icon_added = False
                self.hwnd = None
                self.user.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self.owner._set_error(str(exc))
            if message == self.taskbar_created:
                self.owner._set_ready(False)
            return 0
        return self.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def close(self):
        self._destroy_popup()
        self.owner._set_popup_available(False)
        if self.hwnd:
            self.user.KillTimer(self.hwnd, 2)
        if self.icon_added:
            self.shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data()))
            self.icon_added = False
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.class_registered:
            self.user.UnregisterClassW(self.class_name, self.instance)
            self.class_registered = False
        self._release_icon()


class WindowsNotifier:
    """One tray icon, bounded queue, and an isolated native thread.

    notify() returns acceptance into the local queue, never proof of visibility.
    status.last_delivered_at means Shell accepted the notification request.
    last_shown_at records Shell's NIN_BALLOONSHOW, not human acknowledgement.
    Display callbacks describe this tray icon; Windows does not correlate them
    with a particular queued notification. With no callback, visibility remains
    unconfirmed; this does not by itself prove the notification was suppressed.
    """

    supports_notification_priority = True

    def __init__(self, url, cancel_callback=None, exit_callback=None, *,
                 exit_label=DEFAULT_EXIT_LABEL, exit_message=DEFAULT_EXIT_MESSAGE,
                 tray_tip=DEFAULT_TRAY_TIP):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("El panel de avisos debe usar una URL HTTP local")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Puerto local inválido")
        if any(not isinstance(value, str) or not value.strip()
               for value in (exit_label, exit_message, tray_tip)):
            raise ValueError("Los textos de la bandeja deben ser cadenas no vacías")
        self.url = url
        self.cancel_callback = cancel_callback
        self.cancel_countdown_callback = None
        self.exit_callback = exit_callback
        self.exit_label, self.exit_message, self.tray_tip = exit_label, exit_message, tray_tip
        self._queue = queue.Queue(maxsize=32)
        self._lock = threading.Lock()
        self._thread = None
        self._backend = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._ready = False
        self._last_error = None
        self._last_sound_error = None
        self._last_delivered_at = None
        self._last_shown_at = None
        self._last_hidden_at = None
        self._last_display_event = None
        self._user_notification_state = None
        self._popup_available = False
        self._last_popup_shown_at = None
        self._last_popup_hidden_at = None
        self._persistent_messages = {}
        self._persistent_received = {}
        self._persistent_countdowns = {}
        self._persistent_confirmations = {}
        self._persistent_cancel_permissions = {}
        self._confirmed_countdown_cancellations = OrderedDict()
        self._persistent_sound_at = {}
        self._persistent_dismissed = set()
        self._persistent_versions = {}
        self._persistent_serial = 0
        self._dpi_status = None
        self._notification_theme = "auto"
        self._session_available = True
        self._session_generation = 0
        self._session_probe = None
        self._pending_deliveries = {}
        self._delivery_serial = 0
        self._completed_deliveries = OrderedDict()
        self._delivery_receipts = deque(maxlen=256)
        self.acknowledge_callback = None
        self._ack_inflight = set()
        self._countdown_cancel_inflight = set()
        self._countdown_cancel_feedback = {}

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if os.name != "nt":
                self._last_error = "Los avisos de escritorio requieren Windows"
                return
            self._stop.clear()
            self._started.clear()
            self._last_error = None
            self._thread = threading.Thread(target=self._run, name="elipro-windows-notifications", daemon=True)
            self._thread.start()
        # Bounded startup only, never part of USB polling or notify().
        self._started.wait(2)

    def _set_error(self, message):
        with self._lock:
            self._last_error = message

    def _set_dpi_status(self, value):
        with self._lock:
            self._dpi_status = dict(value)

    @property
    def notification_theme(self):
        with self._lock:
            return self._notification_theme

    def set_theme(self, value):
        """Wake the UI thread; settings/HTTP threads never touch HWNDs or GDI."""
        if not isinstance(value, str) or value not in ("auto", "light", "dark"):
            raise ValueError("El tema de los avisos debe ser auto, light o dark")
        with self._lock:
            if value == self._notification_theme:
                return
            self._notification_theme = value
            backend = self._backend
        if backend is not None:
            backend.wake()

    def set_session_probe(self, probe):
        """Install the agent's fresh WTS/desktop probe, called at delivery time."""
        if probe is not None and not callable(probe):
            raise ValueError("La comprobación de sesión debe ser una función")
        with self._lock:
            self._session_probe = probe

    def _session_currently_available(self):
        with self._lock:
            available, probe = self._session_available, self._session_probe
        if not available:
            return False
        try:
            return probe() is True if probe is not None else True
        except Exception:
            return False

    def _receipt_locked(self, delivery_id, outcome, error=None):
        receipt = {"delivery_id": delivery_id, "outcome": outcome,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        if error is not None:
            receipt["error"] = str(error)[:300]
        self._delivery_receipts.append(receipt)

    def drain_receipts(self):
        """ACK means native acceptance, never queue admission or human reading."""
        with self._lock:
            result = [dict(item) for item in self._delivery_receipts]
            self._delivery_receipts.clear()
            return result

    def cancel_delivery(self, delivery_id):
        """Invalidate a queued payload before the agent replaces its context."""
        with self._lock:
            pending = self._pending_deliveries.pop(delivery_id, None)
            completed = self._completed_deliveries.pop(delivery_id, None)
            self._delivery_receipts = deque((item for item in self._delivery_receipts
                                            if item["delivery_id"] != delivery_id), maxlen=256)
            if pending is not None or completed is not None:
                self._receipt_locked(delivery_id, "cancelled")
            backend = self._backend
        if backend is not None:
            backend.wake()

    def _delivery_is_current(self, delivery_id, ticket, session_generation):
        if not self._session_currently_available():
            return False
        with self._lock:
            completed = self._completed_deliveries.get(delivery_id)
            return (not self._stop.is_set() and self._session_generation == session_generation and
                    (self._pending_deliveries.get(delivery_id) == (session_generation, ticket) or
                     (completed is not None and completed[1:] == (session_generation, ticket))))

    def set_session_available(self, available):
        if type(available) is not bool:
            raise ValueError("La disponibilidad de sesión debe ser booleana")
        with self._lock:
            if available != self._session_available:
                self._session_generation += 1
            self._session_available = available
            if not available:
                self._persistent_confirmations.clear()
                self._persistent_cancel_permissions.clear()
                # The broker owns deferred alerts and will rebuild their text
                # on resume. Never replay an old local countdown after unlock.
                for delivery_id in self._pending_deliveries:
                    self._receipt_locked(delivery_id, "cancelled")
                self._pending_deliveries.clear()
                # Any unacknowledged accepted request must be rebuilt by the
                # broker after resume rather than deduplicated as already seen.
                self._completed_deliveries.clear()
                while True:
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except queue.Empty:
                        break
            backend = self._backend
        if backend is not None:
            backend.wake()

    def _set_sound_error(self, message):
        with self._lock:
            self._last_sound_error = message

    def _set_user_notification_state(self, diagnostic):
        with self._lock:
            self._user_notification_state = dict(diagnostic)

    def _record_display_event(self, event):
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_display_event = {"event": event, "timestamp": stamp}
            if event == "shown":
                self._last_shown_at = stamp
            elif event in ("hidden", "timeout", "clicked"):
                self._last_hidden_at = stamp

    def _set_popup_available(self, available):
        with self._lock:
            self._popup_available = available

    def _record_popup_event(self, event):
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if event == "shown":
                self._last_popup_shown_at = stamp
            elif event == "hidden":
                self._last_popup_hidden_at = stamp

    def _set_ready(self, ready):
        with self._lock:
            self._ready = ready
            if ready:
                self._last_error = None

    def _run(self):
        backend = None
        try:
            backend = _Win32Backend(self)
            with self._lock:
                self._backend = backend
            backend.initialize()
            self._set_ready(True)
            self._started.set()
            if not self._stop.is_set():
                backend.run()
        except Exception as exc:
            self._set_error(str(exc))
        finally:
            self._set_ready(False)
            self._started.set()
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    self._set_error(str(exc))
            with self._lock:
                self._backend = None

    def notify(self, title, message, severity="info", sound=True, *, persistent=False, key=None,
               delivery_id=None, priority=None):
        if severity not in SEVERITIES:
            raise ValueError("Nivel de aviso inválido")
        if not isinstance(title, str) or not isinstance(message, str) or not title or not message:
            raise ValueError("El aviso necesita título y mensaje")
        if type(persistent) is not bool or (persistent and (not isinstance(key, str) or not key)):
            raise ValueError("El aviso persistente necesita una clave válida")
        if delivery_id is not None and (not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 128):
            raise ValueError("El identificador de entrega debe tener entre 1 y 128 caracteres")
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 3):
            raise ValueError("Prioridad de aviso inválida")
        with self._lock:
            if self._stop.is_set() or not self._ready or not self._session_available:
                return False
            backend = self._backend
            generation = self._persistent_versions.get(key)
            item = (title, message, severity, bool(sound))
            item = (*item, True, key, generation) if persistent else item
            duplicate_pending = False
            if priority is not None:
                item = {"notification": item, "priority": priority}
            if delivery_id is not None:
                now = time.monotonic()
                while self._completed_deliveries and next(iter(self._completed_deliveries.values()))[0] < now - 300:
                    self._completed_deliveries.popitem(last=False)
                if delivery_id in self._completed_deliveries:
                    self._receipt_locked(delivery_id, "accepted")
                    return True
                duplicate_pending = delivery_id in self._pending_deliveries
                self._delivery_serial += 1
                item = {"notification": item["notification"] if isinstance(item, dict) else item,
                        "priority": priority or 0, "delivery_id": delivery_id,
                        "session_generation": self._session_generation, "ticket": self._delivery_serial}
            if not duplicate_pending:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    self._last_error = "La cola de avisos está llena"
                    return False
                if delivery_id is not None:
                    self._pending_deliveries[delivery_id] = (self._session_generation, item["ticket"])
        if backend is not None and not backend.wake():
            self._set_error("No se pudo despertar el hilo de avisos")
            return False
        return True

    def sync_persistent(self, messages):
        now = time.monotonic()
        normalized = {key: normalize_notice_metadata(value) for key, value in messages.items()}
        with self._lock:
            if self._stop.is_set():
                return
            while (self._confirmed_countdown_cancellations and
                   next(iter(self._confirmed_countdown_cancellations.values())) < now - 300):
                self._confirmed_countdown_cancellations.popitem(last=False)
            normalized = {key: value for key, value in normalized.items()
                          if (value.get("countdown") or {}).get("countdown_id")
                          not in self._confirmed_countdown_cancellations}
            for key in normalized:
                if key not in self._persistent_messages:
                    self._persistent_serial += 1
                    self._persistent_versions[key] = self._persistent_serial
            self._persistent_versions = {key: self._persistent_versions[key] for key in normalized}
            for key, value in normalized.items():
                countdown = value.get("countdown")
                if countdown is not None:
                    previous = self._persistent_countdowns.get(key)
                    deadline = now + countdown["remaining_seconds"]
                    if previous is not None and previous[0] == countdown["countdown_id"]:
                        deadline = min(previous[1], deadline)
                    self._persistent_countdowns[key] = (countdown["countdown_id"], deadline)
                    confirmation = self._persistent_confirmations.get(key)
                    if confirmation is not None and confirmation[0] != countdown["countdown_id"]:
                        self._persistent_confirmations.pop(key, None)
                        self._persistent_cancel_permissions.pop(key, None)
                    valid_for_ms = countdown["valid_for_ms"]
                    if "confirmed_for_ms" in value:
                        valid_for_ms = min(valid_for_ms, value["confirmed_for_ms"])
                    if (self._session_available and value.get("confirmed", True) is True
                            and countdown["remaining_seconds"] > 0 and valid_for_ms > 0):
                        self._persistent_confirmations[key] = (countdown["countdown_id"], now + valid_for_ms / 1000)
                        self._persistent_cancel_permissions[key] = (countdown["countdown_id"], countdown["cancelable"])
                else:
                    self._persistent_countdowns.pop(key, None)
                    self._persistent_confirmations.pop(key, None)
                    self._persistent_cancel_permissions.pop(key, None)
            self._persistent_countdowns = {key: value for key, value in self._persistent_countdowns.items() if key in normalized}
            self._persistent_confirmations = {key: value for key, value in self._persistent_confirmations.items() if key in normalized}
            self._persistent_cancel_permissions = {key: value for key, value in self._persistent_cancel_permissions.items() if key in normalized}
            self._persistent_messages = normalized
            self._persistent_received = {key: now for key in normalized}
            self._persistent_sound_at = {key: value for key, value in self._persistent_sound_at.items() if key in normalized}
            countdown_ids = {value["countdown"]["countdown_id"] for value in normalized.values() if "countdown" in value}
            self._countdown_cancel_feedback = {key: value for key, value in self._countdown_cancel_feedback.items()
                                              if key in countdown_ids}
            self._persistent_dismissed.intersection_update(normalized)
            backend = self._backend
        if backend is not None:
            backend.wake()

    def _persistent_active(self, key, generation=None):
        with self._lock:
            return (not self._stop.is_set() and key in self._persistent_messages and
                    (generation is None or self._persistent_versions.get(key) == generation))

    def _persistent_keys(self):
        with self._lock:
            return set(self._persistent_messages)

    def _persistent_message(self, key):
        with self._lock:
            return self._persistent_message_locked(key, time.monotonic())

    def _persistent_message_locked(self, key, now):
        value = self._persistent_messages.get(key)
        if value is None:
            return None
        result = age_notice_metadata(value, now - self._persistent_received.get(key, now))
        countdown = result.get("countdown")
        if countdown is not None:
            previous = self._persistent_countdowns.get(key)
            if previous is not None and previous[0] == countdown["countdown_id"]:
                countdown["remaining_seconds"] = min(countdown["remaining_seconds"], max(0, previous[1] - now))
                if countdown["remaining_seconds"] <= 0:
                    countdown["cancelable"] = False
                    result["confirmed"] = False
                    result["priority"] = min(result.get("priority", 0), 1)
            confirmation = self._persistent_confirmations.get(key)
            if (confirmation is not None and confirmation[0] == countdown["countdown_id"]
                    and (result.get("confirmed", True) is not True or countdown["valid_for_ms"] <= 0)
                    and countdown["remaining_seconds"] > 0
                    and now < confirmation[1] + COUNTDOWN_REFRESH_GRACE_SECONDS):
                # This local-only presentation hint never renews validity,
                # authorizes cancellation, or extends the episode deadline.
                countdown["refresh_pending"] = True
        return result

    def _persistent_acknowledged(self, key):
        with self._lock:
            return key in self._persistent_dismissed

    def _ack_persistent(self, key):
        if isinstance(key, str) and key.startswith("notice:"):
            callback = self.acknowledge_callback
            try:
                notice_id = int(key.partition(":")[2])
            except ValueError:
                return
            with self._lock:
                if key not in self._persistent_messages or key in self._ack_inflight:
                    return
                if not callable(callback):
                    self._last_error = "No se pudo guardar Entendido; abre Elipro para confirmar el aviso."
                    return
                self._ack_inflight.add(key)
                generation = self._persistent_versions.get(key)
            def acknowledge():
                try:
                    callback([notice_id])
                    with self._lock:
                        if self._persistent_versions.get(key) == generation:
                            self._persistent_dismissed.add(key)
                            self._last_error = None
                except Exception as exc:
                    with self._lock:
                        if self._persistent_versions.get(key) == generation:
                            self._last_error = "No se guardó Entendido: " + str(exc)[:300]
                finally:
                    with self._lock:
                        self._ack_inflight.discard(key)
                        backend = self._backend
                    if backend is not None:
                        backend.wake()
            threading.Thread(target=acknowledge, name="elipro-notice-ack", daemon=True).start()
            return
        with self._lock:
            if key in self._persistent_messages:
                self._persistent_dismissed.add(key)

    def _next_persistent(self, current_key=None):
        with self._lock:
            now = time.monotonic()
            candidates = [(key, self._persistent_message_locked(key, now)) for key in self._persistent_messages
                          if key not in self._persistent_dismissed]
            if not candidates:
                return None
            best = max(candidates, key=lambda item: item[1].get("priority", 0))
            current = next((item for item in candidates if item[0] == current_key), None)
            return current if current is not None and current[1].get("priority", 0) >= best[1].get("priority", 0) else best

    def _countdown_feedback(self, countdown_id):
        with self._lock:
            return self._countdown_cancel_feedback.get(countdown_id), countdown_id in self._countdown_cancel_inflight

    def _countdown_cancel_request_allowed(self, notice):
        """Permit a request, never infer cancellation from an old UI snapshot."""
        countdown = notice.get("countdown") if isinstance(notice, dict) else None
        if (not countdown or countdown["remaining_seconds"] <= 0
                or not callable(self.cancel_countdown_callback) or not self._session_currently_available()):
            return False
        identifier, now = countdown["countdown_id"], time.monotonic()
        with self._lock:
            if (self._stop.is_set() or identifier in self._countdown_cancel_inflight
                    or identifier in self._confirmed_countdown_cancellations):
                return False
            if (notice.get("confirmed", True) is True and countdown["valid_for_ms"] > 0
                    and countdown["cancelable"] is True):
                return True
            if countdown.get("refresh_pending") is not True:
                return False
            for key, (episode, valid_until) in self._persistent_confirmations.items():
                if (episode == identifier and now < valid_until + COUNTDOWN_REFRESH_GRACE_SECONDS
                        and self._persistent_cancel_permissions.get(key) == (identifier, True)):
                    deadline = self._persistent_countdowns.get(key)
                    if deadline is not None and deadline[0] == identifier and deadline[1] > now:
                        return True
        return False

    def _cancel_persistent_countdown(self, key):
        notice = self._persistent_message(key)
        if not self._countdown_cancel_request_allowed(notice):
            return
        countdown = notice["countdown"]
        countdown_id = countdown["countdown_id"]
        with self._lock:
            if self._stop.is_set() or countdown_id in self._countdown_cancel_inflight:
                return
            self._countdown_cancel_inflight.add(countdown_id)
            self._countdown_cancel_feedback.pop(countdown_id, None)
            generation = self._persistent_versions.get(key)
        def cancel():
            feedback = None
            confirmed = False
            try:
                result = self.cancel_countdown_callback(countdown_id)
                protection = result.get("protection") if isinstance(result, dict) else None
                if (not isinstance(result, dict) or result.get("cancelled_countdown_id") != countdown_id
                        or not isinstance(protection, dict) or protection.get("state") != "cancelled"):
                    raise ValueError("El monitor no confirmó esta cuenta.")
                confirmed = True
                feedback = "El monitor confirmó la cancelación."
            except Exception as exc:
                feedback = "No se confirmó la cancelación. " + str(exc)[:60]
            finally:
                with self._lock:
                    self._countdown_cancel_inflight.discard(countdown_id)
                    current = self._persistent_message_locked(key, time.monotonic())
                    if confirmed and not self._stop.is_set():
                        self._confirmed_countdown_cancellations[countdown_id] = time.monotonic()
                        self._confirmed_countdown_cancellations.move_to_end(countdown_id)
                        while len(self._confirmed_countdown_cancellations) > 256:
                            self._confirmed_countdown_cancellations.popitem(last=False)
                    if (not self._stop.is_set() and self._persistent_versions.get(key) == generation
                            and (current or {}).get("countdown", {}).get("countdown_id") == countdown_id):
                        self._countdown_cancel_feedback[countdown_id] = feedback
                        if confirmed:
                            # Close only this confirmed episode, without marking
                            # its durable incident as read or touching a new one.
                            for values in (self._persistent_messages, self._persistent_received,
                                           self._persistent_countdowns, self._persistent_confirmations,
                                           self._persistent_cancel_permissions, self._persistent_versions,
                                           self._persistent_sound_at):
                                values.pop(key, None)
                            self._persistent_dismissed.discard(key)
                            self._last_error = None
                    backend = self._backend
                if backend is not None:
                    backend.wake()
        threading.Thread(target=cancel, name="elipro-countdown-cancel", daemon=True).start()

    def _request_exit(self):
        callback = self.exit_callback
        if callback is None or self._stop.is_set():
            return
        def request():
            if self._stop.is_set():
                return
            try:
                callback()
            except Exception as exc:
                self._set_error("No se pudo cerrar Elipro: " + str(exc)[:300])
        # server.shutdown and notifier.stop can join threads. Neither is ever
        # called from the native message loop that those operations must join.
        threading.Thread(target=request, name="elipro-tray-exit", daemon=True).start()

    def _drain(self, backend):
        synchronized = False
        processed = False
        apply_theme = getattr(backend, "_apply_popup_theme", None)
        if callable(apply_theme):
            try:
                apply_theme()
            except Exception as exc:
                self._set_error(str(exc))
        # Select from the complete current state before a FIFO ticket can steal
        # the desktop or hide a newer, more urgent condition.
        if getattr(backend, "supports_notification_priority", False) is True and self._session_currently_available():
            try:
                backend.sync_persistent()
                synchronized = True
            except Exception as exc:
                self._set_error(str(exc))
        while not self._stop.is_set():
            if not self._session_currently_available():
                self.set_session_available(False)
                suspend = getattr(backend, "suspend_session", None)
                if callable(suspend):
                    suspend()
                return
            try:
                with self._lock:
                    item = self._queue.get_nowait()
            except queue.Empty:
                synchronize = getattr(backend, "sync_persistent", None)
                if callable(synchronize) and (not synchronized or processed):
                    try:
                        synchronize()
                    except Exception as exc:
                        self._set_error(str(exc))
                return
            processed = True
            delivery_id = item.get("delivery_id") if isinstance(item, dict) else None
            notification = item["notification"] if isinstance(item, dict) else item
            session_generation = item.get("session_generation") if isinstance(item, dict) else None
            ticket = item.get("ticket") if isinstance(item, dict) else None
            error = None
            try:
                validator = None
                if delivery_id is not None:
                    validator = lambda delivery_id=delivery_id, ticket=ticket, session_generation=session_generation: self._delivery_is_current(delivery_id, ticket, session_generation)
                    if not validator():
                        continue
                if validator is not None and getattr(backend, "supports_delivery_validation", False) is True:
                    result = backend.deliver(*notification, validity_callback=validator)
                else:
                    result = backend.deliver(*notification)
                available = self._session_currently_available()
                with self._lock:
                    if result is not False and available:
                        self._last_delivered_at = datetime.now(timezone.utc).isoformat()
                        self._last_error = None
                    if delivery_id is not None and self._pending_deliveries.get(delivery_id) == (session_generation, ticket):
                        self._pending_deliveries.pop(delivery_id, None)
                        if result is True and available and self._session_generation == session_generation:
                            self._completed_deliveries[delivery_id] = (time.monotonic(), session_generation, ticket)
                            while len(self._completed_deliveries) > 256:
                                self._completed_deliveries.popitem(last=False)
                            self._receipt_locked(delivery_id, "accepted")
                        else:
                            self._receipt_locked(delivery_id, "cancelled" if not available or result is False else "failed")
            except Exception as exc:
                error = str(exc)
                self._set_error(error)
                if delivery_id is not None:
                    with self._lock:
                        if self._pending_deliveries.get(delivery_id) == (session_generation, ticket):
                            self._pending_deliveries.pop(delivery_id, None)
                            self._receipt_locked(delivery_id, "failed", error)
            finally:
                self._queue.task_done()

    def _open_panel(self):
        if not webbrowser.open(self.url, new=2):
            self._set_error("Windows no pudo abrir el panel de Elipro")

    def status(self):
        with self._lock:
            return {"available": os.name == "nt", "ready": self._ready,
                    "last_error": self._last_error, "last_delivered_at": self._last_delivered_at,
                    "last_sound_error": self._last_sound_error,
                    "last_shown_at": self._last_shown_at, "last_hidden_at": self._last_hidden_at,
                    "last_display_event": dict(self._last_display_event) if self._last_display_event else None,
                    "user_notification_state": dict(self._user_notification_state) if self._user_notification_state else None,
                    "popup_available": self._popup_available,
                    "last_popup_shown_at": self._last_popup_shown_at,
                    "last_popup_hidden_at": self._last_popup_hidden_at,
                    "persistent_active": len(self._persistent_messages),
                    "persistent_dismissed": len(self._persistent_dismissed),
                    "dpi_awareness": dict(self._dpi_status) if self._dpi_status else None,
                    "notification_theme": self._notification_theme,
                    "session_available": self._session_available,
                    "pending_deliveries": len(self._pending_deliveries),
                    "delivery_receipts": len(self._delivery_receipts),
                    "platform": "windows" if os.name == "nt" else os.name,
                    "channel": "windows-tray"}

    def stop(self):
        self._stop.set()
        with self._lock:
            backend, thread = self._backend, self._thread
            self._persistent_messages.clear()
            self._persistent_received.clear()
            self._persistent_countdowns.clear()
            self._persistent_confirmations.clear()
            self._persistent_cancel_permissions.clear()
            self._confirmed_countdown_cancellations.clear()
            self._persistent_sound_at.clear()
            self._persistent_dismissed.clear()
            self._persistent_versions.clear()
            for delivery_id in self._pending_deliveries:
                self._receipt_locked(delivery_id, "cancelled")
            self._pending_deliveries.clear()
        if backend is not None:
            backend.request_stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if thread is None or not thread.is_alive():
            self._set_ready(False)
            # Never replay obsolete alerts if explicitly restarted.
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
