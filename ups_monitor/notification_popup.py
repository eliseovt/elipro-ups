"""Branded Win32 popup presentation, with no protection or delivery decisions.

All HWND/GDI work is performed by the owning notification message-loop thread.
Icons are borrowed from the backend; fonts, brushes and pens belong to this view.
"""
from __future__ import annotations

import ctypes
import math
import textwrap

from .runtime import resource_root


UINT = ctypes.c_uint32
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
BOOL = ctypes.c_int32
PTR = ctypes.c_void_p

WM_PAINT, WM_ERASEBKGND = 0x000F, 0x0014
WM_DRAWITEM, WM_CTLCOLORSTATIC = 0x002B, 0x0138
WM_SETFONT, STM_SETICON = 0x0030, 0x0170
DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x0001, 0x0004, 0x0020
DT_WORDBREAK, DT_CALCRECT, DT_NOPREFIX = 0x0010, 0x0400, 0x0800
DT_END_ELLIPSIS = 0x8000
ODS_SELECTED, ODS_DISABLED, ODS_FOCUS, ODS_NOFOCUSRECT = 1, 4, 16, 512


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", PTR), ("fErase", BOOL), ("rcPaint", RECT),
                ("fRestore", BOOL), ("fIncUpdate", BOOL), ("rgbReserved", ctypes.c_ubyte * 32)]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [("CtlType", UINT), ("CtlID", UINT), ("itemID", UINT),
                ("itemAction", UINT), ("itemState", UINT), ("hwndItem", PTR),
                ("hDC", PTR), ("rcItem", RECT), ("itemData", ctypes.c_size_t)]


class HIGHCONTRAST(ctypes.Structure):
    _fields_ = [("cbSize", UINT), ("dwFlags", DWORD), ("lpszDefaultScheme", ctypes.c_wchar_p)]


def _color(value):
    value = value.lstrip("#")
    red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    return red | (green << 8) | (blue << 16)


_PALETTES = {
    "light": dict(background="#ffffff", foreground="#193340", muted="#5c7283", line="#d7e2e9",
                  secondary="#f4f7f9", teal="#087e6c", teal_background="#e6f5ef",
                  warning="#9c5b08", warning_background="#fff7e8", critical="#b13046",
                  critical_background="#fff0f3", primary="#f4bc65", primary_text="#2b2113",
                  disabled="#e7edf1", disabled_text="#667985", track="#e0e7e9", focus="#087e6c"),
    "dark": dict(background="#111820", foreground="#e8eff3", muted="#97a9b8", line="#293641",
                 secondary="#17212b", teal="#52e0c2", teal_background="#10352f",
                 warning="#f4bc65", warning_background="#2a231a", critical="#fc8585",
                 critical_background="#322126", primary="#f4bc65", primary_text="#2b2113",
                 disabled="#28343e", disabled_text="#97a9b8", track="#35424e", focus="#52e0c2"),
}


def resolve_theme(preference, *, system_light=None):
    """Resolve an explicit theme or the current Windows application appearance."""
    if preference not in ("auto", "light", "dark"):
        raise ValueError("El tema de avisos debe ser auto, light o dark")
    if preference != "auto":
        return preference
    if system_light is None:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                system_light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0] != 0
        except (ImportError, OSError):
            system_light = True
    return "light" if system_light else "dark"


def format_countdown(seconds):
    """Round up display time; this formatting never establishes action validity."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds):
        return "—:—"
    total = max(0, min(86400, math.ceil(seconds)))
    minutes, second = divmod(total, 60)
    if minutes >= 60:
        hour, minute = divmod(minutes, 60)
        return f"{hour}:{minute:02d}:{second:02d}"
    return f"{minutes:02d}:{second:02d}"


def bind_popup_apis(user, gdi):
    """Bind pointer-sized drawing APIs separately from the delivery backend."""
    if gdi is None:
        return
    for dll, name, args, result in (
        (user, "GetClientRect", [PTR, ctypes.POINTER(RECT)], BOOL),
        (user, "GetDC", [PTR], PTR), (user, "ReleaseDC", [PTR, PTR], ctypes.c_int),
        (user, "BeginPaint", [PTR, ctypes.POINTER(PAINTSTRUCT)], PTR),
        (user, "EndPaint", [PTR, ctypes.POINTER(PAINTSTRUCT)], BOOL),
        (user, "FillRect", [PTR, ctypes.POINTER(RECT), PTR], ctypes.c_int),
        (user, "DrawFocusRect", [PTR, ctypes.POINTER(RECT)], BOOL),
        (user, "DrawTextW", [PTR, ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(RECT), UINT], ctypes.c_int),
        (user, "GetWindowTextW", [PTR, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
        (user, "InvalidateRect", [PTR, ctypes.POINTER(RECT), BOOL], BOOL),
        (user, "GetSysColor", [ctypes.c_int], DWORD),
        (gdi, "CreateSolidBrush", [DWORD], PTR),
        (gdi, "CreatePen", [ctypes.c_int, ctypes.c_int, DWORD], PTR),
        (gdi, "SelectObject", [PTR, PTR], PTR),
        (gdi, "SaveDC", [PTR], ctypes.c_int), (gdi, "RestoreDC", [PTR, ctypes.c_int], BOOL),
        (gdi, "SetBkMode", [PTR, ctypes.c_int], ctypes.c_int),
        (gdi, "SetBkColor", [PTR, DWORD], DWORD), (gdi, "SetTextColor", [PTR, DWORD], DWORD),
        (gdi, "RoundRect", [PTR] + [ctypes.c_int] * 6, BOOL),
    ):
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result


def _handle(value):
    return isinstance(value, int) and value != 0


class PopupView:
    """Native controls and painting; the backend retains every action callback."""

    def __init__(self, backend):
        self.backend, self.user, self.gdi = backend, backend.user, backend.gdi
        bind_popup_apis(self.user, self.gdi)
        self.controls, self.fonts, self.brushes, self.pens = {}, {}, {}, {}
        self.bounds, self.texts = {}, {}
        self._visibility = {}
        self.width, self.height = 480, 400
        self.title, self.message, self.footer = "", "", ""
        self.countdown, self._countdown_total = None, None
        self.severity, self.theme, self.palette = "warning", None, {}
        self._font_dpi = None
        self._layout_dpi = None
        self._logo_dpi = None
        self._logo_icon = None
        self._owns_logo_icon = False
        self._closed = False
        self.apply_theme("auto")

    def create_controls(self):
        common = 0x40000000 | 0x10000000  # WS_CHILD | WS_VISIBLE
        for key, kind, text, extra, identity in (
            ("logo", "STATIC", "", 0x0003 | 0x0040 | 0x0200, None),
            ("brand", "STATIC", "elipro UPS", 0x0080, None),
            ("badge", "STATIC", "AVISO", 0x0080 | 0x0001, None),
            ("title", "STATIC", "Elipro UPS", 0x0080, None),
            ("body", "STATIC", "", 0x0080, None),
            ("countdown_label", "STATIC", "Tiempo restante", 0x0080, None),
            ("countdown", "STATIC", "00:00", 0x0080 | 0x0002, None),
            ("footer", "STATIC", "", 0x0080, None),
            ("open", "BUTTON", "Abrir Elipro", 0x000B | 0x10000, 101),
            ("ack", "BUTTON", "Entendido", 0x000B | 0x10000, 102),
            ("cancel", "BUTTON", "Cancelar protección", 0x000B | 0x10000, 103),
        ):
            handle = self.user.CreateWindowExW(0, kind, text, common | extra,
                0, 0, 1, 1, self.backend.popup_hwnd,
                ctypes.c_void_p(identity) if identity else None, self.backend.instance, None)
            if not handle:
                raise OSError("No se pudieron crear los controles del aviso de Elipro")
            self.controls[key] = handle
            self.texts[key] = text
        self.set_fonts()
        return dict(self.controls)

    def _set_logo(self):
        if not self.controls.get("logo") or self._logo_dpi == self.backend.popup_dpi:
            return
        size = self.backend._scale(38)
        try:
            icon = self.user.LoadImageW(None, str(resource_root() / "static" / "assets" / "elipro.ico"),
                                        1, size, size, 0x0010)
        except (OSError, AttributeError):
            icon = None
        owned = _handle(icon)
        if not owned:
            icon = self.backend._load_icon()
        previous, previous_owned = self._logo_icon, self._owns_logo_icon
        self._logo_icon, self._owns_logo_icon = icon, owned
        self._logo_dpi = self.backend.popup_dpi
        if icon:
            self.user.SendMessageW(self.controls["logo"], STM_SETICON, icon, 0)
        if previous_owned and previous:
            self.user.DestroyIcon(previous)

    def set_fonts(self):
        self._set_logo()
        if self.gdi is None or self._font_dpi == self.backend.popup_dpi:
            return
        fresh = {}
        for role, size, weight in (("brand", 20, 600), ("title", 21, 600), ("body", 14, 400),
                                   ("timer", 38, 600), ("small", 12, 400), ("button", 14, 600)):
            font = self.gdi.CreateFontW(-self.backend._scale(size), 0, 0, 0, weight,
                                      0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
            if not _handle(font):
                for created in fresh.values():
                    self.gdi.DeleteObject(created)
                return
            fresh[role] = font
        old, self.fonts = self.fonts, fresh
        for key, handle in self.controls.items():
            if key == "logo":
                continue
            role = {"brand": "brand", "title": "title", "body": "body", "countdown": "timer",
                    "open": "button", "ack": "button", "cancel": "button"}.get(key, "small")
            self.user.SendMessageW(handle, WM_SETFONT, fresh[role], 1)
        self._font_dpi = self.backend.popup_dpi
        for font in set(old.values()):
            self.gdi.DeleteObject(font)

    def _high_contrast(self):
        function = getattr(self.user, "SystemParametersInfoW", None)
        # Only call a real native function here. Old fake delivery fixtures may
        # implement SPI_GETWORKAREA only; presentation tests inject colors directly.
        if not isinstance(function, getattr(ctypes, "_CFuncPtr", ())):
            return False
        value = HIGHCONTRAST()
        value.cbSize = ctypes.sizeof(value)
        return bool(function(0x0042, value.cbSize, ctypes.byref(value), 0) and value.dwFlags & 1)

    def apply_theme(self, preference):
        resolved = resolve_theme(preference)
        palette = {key: _color(value) for key, value in _PALETTES[resolved].items()}
        if self.gdi is not None and self._high_contrast():
            window, text, button, button_text, highlight, highlight_text, gray = (
                self.user.GetSysColor(index) for index in (5, 8, 15, 18, 13, 14, 17))
            palette.update(background=window, foreground=text, muted=text, line=text,
                secondary=button, teal=text, teal_background=window, warning=text,
                warning_background=window, critical=text, critical_background=window,
                primary=highlight, primary_text=highlight_text, disabled=button,
                disabled_text=gray, track=button, focus=text)
        if palette == self.palette:
            return False
        if self.gdi is not None:
            fresh = {}
            for color in set(palette.values()):
                brush = self.gdi.CreateSolidBrush(color)
                if not _handle(brush):
                    for allocated in fresh.values():
                        self.gdi.DeleteObject(allocated)
                    if not self.palette:
                        raise OSError("Windows no pudo crear los colores del aviso de Elipro")
                    return False
                fresh[color] = brush
            old_brushes, old_pens = self.brushes, self.pens
            self.brushes = fresh
            self.pens = {}
            for handle in set(old_brushes.values()) | set(old_pens.values()):
                if _handle(handle):
                    self.gdi.DeleteObject(handle)
        self.theme, self.palette = resolved, palette
        self._set_frame_theme()
        self._invalidate()
        return True

    def _set_frame_theme(self):
        # Optional Windows 11 polish. Windows 10 retains the fully painted
        # client area even when these DWM attributes are unsupported.
        if not isinstance(getattr(self.user, "CreateWindowExW", None), getattr(ctypes, "_CFuncPtr", ())):
            return
        try:
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            function = dwm.DwmSetWindowAttribute
            function.argtypes, function.restype = [PTR, DWORD, PTR, DWORD], LONG
            for attribute, value in ((20, int(self.theme == "dark")), (33, 2), (34, self.palette["line"])):
                data = DWORD(value)
                function(self.backend.popup_hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
        except (AttributeError, OSError):
            pass

    def _invalidate(self):
        if self.backend.popup_hwnd and self.gdi is not None:
            self.user.InvalidateRect(self.backend.popup_hwnd, None, True)
            for handle in self.controls.values():
                self.user.InvalidateRect(handle, None, True)

    def _set_text(self, key, value):
        value = str(value)
        if self.texts.get(key) != value:
            self.texts[key] = value
            if self.controls.get(key):
                self.user.SetWindowTextW(self.controls[key], value)

    def set_content(self, title, message, severity="warning", countdown=None, footer=""):
        previous = (self.title, self.message, self.severity, self.countdown, self.footer)
        self.severity = severity if severity in ("warning", "critical", "error", "info") else "warning"
        self.countdown = dict(countdown) if isinstance(countdown, dict) else None
        active = self.countdown is not None
        valid = active and self.countdown.get("valid_for_ms", 0) > 0 and self.countdown.get("remaining_seconds", 0) > 0
        if active:
            title = ("Hibernación programada" if self.countdown.get("action") == "hibernate" else "Apagado programado") if valid else "Estado sin confirmar"
            badge = "URGENTE" if valid else "SIN CONFIRMAR"
            clock = format_countdown(self.countdown.get("remaining_seconds")) if valid else "—:—"
            self._set_text("countdown", clock)
            identifier = self.countdown.get("countdown_id")
            if valid and (getattr(self, "_countdown_id", None) != identifier or self._countdown_total is None):
                self._countdown_total = max(1, self.countdown.get("remaining_seconds", 1))
                self._countdown_id = identifier
        else:
            badge = {"critical": "CRÍTICO", "error": "CRÍTICO", "info": "INFORMACIÓN"}.get(self.severity, "AVISO")
            self._countdown_total = None
        self.title, self.message = str(title), str(message)
        default_footer = ("Entendido reconoce el aviso; la protección continúa." if valid
                          else "Entendido no cancela la protección.") if active else "Entendido confirma la lectura de este aviso."
        self.footer = str(footer or default_footer)
        for key, value in (("title", self.title), ("body", self.message), ("badge", badge), ("footer", self.footer)):
            self._set_text(key, value)
        for key in ("countdown_label", "countdown", "cancel"):
            if self.controls.get(key) and self._visibility.get(key) != active:
                self.user.ShowWindow(self.controls[key], 5 if active else 0)
                self._visibility[key] = active
        changed = previous != (self.title, self.message, self.severity, self.countdown, self.footer)
        if changed:
            self._invalidate()
        return changed

    def _measure(self, text, width, role, line_height):
        """DrawText uses the same selected font as its STATIC control."""
        if self.gdi is not None and self.fonts.get(role):
            dc = self.user.GetDC(self.backend.popup_hwnd)
            if _handle(dc):
                saved = self.gdi.SaveDC(dc)
                try:
                    self.gdi.SelectObject(dc, self.fonts[role])
                    rectangle = RECT(0, 0, self.backend._scale(width), 0)
                    measured = self.user.DrawTextW(dc, text, -1, ctypes.byref(rectangle), DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX)
                    if type(measured) is int and measured > 0:
                        return max(line_height, math.ceil(measured * 96 / self.backend.popup_dpi))
                finally:
                    if type(saved) is int and saved:
                        self.gdi.RestoreDC(dc, saved)
                    self.user.ReleaseDC(self.backend.popup_hwnd, dc)
        characters = max(8, int(width / (line_height * .47)))
        lines = sum(max(1, len(textwrap.wrap(line, characters))) for line in str(text).splitlines()) or 1
        return max(line_height, lines * line_height)

    def layout(self, countdown, width=480, max_height=None):
        self.width = max(240, int(width))
        self.set_fonts()
        padding = 22 if self.width >= 400 else 16
        content_width = self.width - padding * 2
        title_height = self._measure(self.title or "Elipro UPS", content_width, "title", 27)
        body_height = self._measure(self.message, content_width, "body", 19)
        footer_height = self._measure(self.footer, content_width, "small", 16)

        def compute(tight, body_size):
            edge, gap = (12, 8) if tight else (18, 14)
            logo_size = 32 if tight else 38
            header_bottom = edge + logo_size + (12 if tight else 16)
            top = header_bottom + (10 if tight else 16)
            narrow = self.width < 360
            if narrow:
                header_bottom += 28
                top += 28
            bounds = {"logo": (padding, edge, logo_size, logo_size),
                      "brand": (padding + logo_size + 8, edge + (4 if tight else 7), max(70, content_width - logo_size - 134), 28),
                      "badge": (self.width - padding - 122, edge + (8 if tight else 11), 122, 20),
                      "divider": (padding, header_bottom, content_width, 1),
                      "title": (padding, top, content_width, title_height)}
            if narrow:
                bounds["brand"] = (padding + logo_size + 8, edge + 4, content_width - logo_size - 8, 28)
                bounds["badge"] = (padding, edge + logo_size + 6, content_width, 20)
            top += title_height + (6 if tight else 8)
            bounds["body"] = (padding, top, content_width, body_size)
            top += body_size + gap
            if countdown:
                box_height = 64 if tight else 80
                bounds["countdown_box"] = (padding, top, content_width, box_height)
                timer_width = min(204, max(110, content_width * .5))
                bounds["countdown_label"] = (padding + 14, top + (14 if tight else 22), max(55, content_width - timer_width - 32), 30)
                bounds["countdown"] = (self.width - padding - timer_width - 14, top + (3 if tight else 9), timer_width, 49)
                bounds["progress"] = (padding + 14, top + box_height - 10, content_width - 28, 3)
                top += box_height + gap
                bounds["cancel"] = (padding, top, content_width, 36 if tight else 42)
                top += (36 if tight else 42) + gap
            button_height = 36 if tight else 40
            half = (content_width - 10) // 2
            bounds["open"] = (padding, top, half, button_height)
            bounds["ack"] = (padding + half + 10, top, content_width - half - 10, button_height)
            top += button_height + gap
            bounds["footer"] = (padding, top, content_width, footer_height)
            return bounds, top + footer_height + edge

        bounds, height = compute(False, body_height)
        if max_height is not None and height > max_height:
            bounds, height = compute(True, body_height)
            if height > max_height:
                body_height = max(19, body_height - (height - int(max_height)))
                bounds, height = compute(True, body_height)
        emergency = max_height is not None and height > max_height
        if emergency:
            # Extremely small work areas retain the title, clock and all three
            # actions. The full explanation remains available through the panel.
            padding = 10
            content_width = self.width - 20
            top = 48
            bounds = {"logo": (padding, 8, 28, 28),
                      "brand": (46, 9, self.width - 56, 28),
                      "divider": (padding, 40, content_width, 1),
                      "title": (padding, top, content_width, title_height)}
            top += title_height + 6
            if countdown:
                bounds["countdown_box"] = (padding, top, content_width, 58)
                timer_width = min(204, max(110, content_width * .5))
                bounds["countdown_label"] = (padding + 10, top + 10, content_width - timer_width - 24, 32)
                bounds["countdown"] = (self.width - padding - timer_width - 10, top, timer_width, 49)
                bounds["progress"] = (padding + 10, top + 51, content_width - 20, 3)
                top += 64
                bounds["cancel"] = (padding, top, content_width, 32)
                top += 38
            half = (content_width - 8) // 2
            bounds["open"] = (padding, top, half, 32)
            bounds["ack"] = (padding + half + 8, top, content_width - half - 8, 32)
            top += 38
            bounds["footer"] = (padding, top, content_width, 16)
            height = top + 26
        for key in ("body", "badge"):
            visible = not emergency
            if self.controls.get(key) and self._visibility.get(key) != visible:
                self.user.ShowWindow(self.controls[key], 5 if visible else 0)
                self._visibility[key] = visible
        short_footer = "Entendido no cancela la protección." if countdown else "Entendido confirma la lectura."
        self._set_text("footer", short_footer if emergency else self.footer)
        # Preserve the full source for future wider layouts. Only the
        # constrained display copy receives an ellipsis; the panel retains details.
        full_height = self._measure(self.message, content_width, "body", 19)
        display_message = self.message
        if body_height < full_height:
            line_count = max(1, body_height // 19)
            character_limit = max(12, int(content_width / 8) * line_count - 1)
            display_message = self.message[:character_limit].rstrip() + "…"
        self._set_text("body", display_message)
        for key, rectangle in bounds.items():
            if key not in self.controls or (self.bounds.get(key) == rectangle and self._layout_dpi == self.backend.popup_dpi):
                continue
            x, y, child_width, child_height = rectangle
            if not self.user.SetWindowPos(self.controls[key], None, self.backend._scale(x), self.backend._scale(y),
                    self.backend._scale(child_width), self.backend._scale(child_height), 0x0010 | 0x0004):
                raise OSError("Windows no pudo distribuir el aviso de Elipro")
        if bounds != self.bounds:
            self.bounds = bounds
            self._invalidate()
        self._layout_dpi = self.backend.popup_dpi
        self.height = height
        return height

    def _brush(self, key):
        return self.brushes.get(self.palette[key])

    def _rect(self, bounds):
        x, y, width, height = bounds
        scale = self.backend._scale
        return RECT(scale(x), scale(y), scale(x + width), scale(y + height))

    def _fill(self, dc, rectangle, key):
        brush = self._brush(key)
        if _handle(brush):
            self.user.FillRect(dc, ctypes.byref(rectangle), brush)

    def _rounded(self, dc, rectangle, fill, line, radius=8):
        brush = self._brush(fill)
        if not _handle(brush):
            return
        color = self.palette[line]
        if color not in self.pens:
            self.pens[color] = self.gdi.CreatePen(0, max(1, self.backend._scale(1)), color)
        self.gdi.SelectObject(dc, brush)
        pen = self.pens[color]
        if _handle(pen):
            self.gdi.SelectObject(dc, pen)
        self.gdi.RoundRect(dc, rectangle.left, rectangle.top, rectangle.right, rectangle.bottom,
                           self.backend._scale(radius * 2), self.backend._scale(radius * 2))

    def _status_colors(self):
        if self.countdown is not None or self.severity == "warning":
            return "warning", "warning_background"
        if self.severity in ("critical", "error"):
            return "critical", "critical_background"
        return "teal", "teal_background"

    def _paint(self, dc):
        saved = self.gdi.SaveDC(dc)
        try:
            rectangle = RECT()
            self.user.GetClientRect(self.backend.popup_hwnd, ctypes.byref(rectangle))
            self._fill(dc, rectangle, "background")
            self._rounded(dc, rectangle, "background", "line", 14)
            if self.bounds.get("divider"):
                self._fill(dc, self._rect(self.bounds["divider"]), "line")
            if self.bounds.get("badge"):
                foreground, background = self._status_colors()
                x, y, width, height = self.bounds["badge"]
                self._rounded(dc, self._rect((x - 5, y - 4, width + 10, height + 8)), background, background, 6)
            if self.countdown is not None and self.bounds.get("countdown_box"):
                self._rounded(dc, self._rect(self.bounds["countdown_box"]), "warning_background", "warning_background", 8)
                self._fill(dc, self._rect(self.bounds["progress"]), "track")
                remaining = max(0, self.countdown.get("remaining_seconds", 0))
                valid = self.countdown.get("valid_for_ms", 0) > 0
                ratio = min(1, remaining / self._countdown_total) if valid and self._countdown_total else 0
                if ratio:
                    x, y, width, height = self.bounds["progress"]
                    self._fill(dc, self._rect((x, y, max(1, width * ratio), height)), "primary")
        finally:
            if type(saved) is int and saved:
                self.gdi.RestoreDC(dc, saved)

    def _button(self, item):
        if item.CtlType != 4 or item.CtlID not in (101, 102, 103):  # ODT_BUTTON
            return None
        dc, rectangle = item.hDC, item.rcItem
        saved = self.gdi.SaveDC(dc)
        try:
            self._fill(dc, rectangle, "background")
            disabled = item.itemState & ODS_DISABLED
            main = item.CtlID == 103
            acknowledged = item.CtlID == 102 and self.countdown is None
            fill = "disabled" if disabled else "primary" if main else "teal" if acknowledged else "secondary"
            text = "disabled_text" if disabled else "primary_text" if main else "background" if acknowledged else "foreground"
            self._rounded(dc, rectangle, fill, fill if (main or acknowledged) else "line")
            self.gdi.SetBkMode(dc, 1)
            self.gdi.SetTextColor(dc, self.palette[text])
            if self.fonts.get("button"):
                self.gdi.SelectObject(dc, self.fonts["button"])
            key = {101: "open", 102: "ack", 103: "cancel"}[item.CtlID]
            buffer = ctypes.create_unicode_buffer(256)
            length = self.user.GetWindowTextW(item.hwndItem, buffer, len(buffer))
            label = buffer.value if type(length) is int and length > 0 else self.texts.get(key, "")
            inset = 1 if item.itemState & ODS_SELECTED else 0
            label_rect = RECT(rectangle.left + inset, rectangle.top + inset,
                              rectangle.right + inset, rectangle.bottom + inset)
            self.user.DrawTextW(dc, label, -1, ctypes.byref(label_rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
            if item.itemState & ODS_FOCUS and not item.itemState & ODS_NOFOCUSRECT:
                edge = self.backend._scale(5)
                focus = RECT(rectangle.left + edge, rectangle.top + edge,
                             rectangle.right - edge, rectangle.bottom - edge)
                self.user.DrawFocusRect(dc, ctypes.byref(focus))
        finally:
            if type(saved) is int and saved:
                self.gdi.RestoreDC(dc, saved)
        return 1

    def handle_message(self, message, wparam, lparam):
        if self.gdi is None or not _handle(self._brush("background")):
            return None
        if message == WM_ERASEBKGND:
            self._paint(wparam)
            return 1
        if message == WM_PAINT:
            paint = PAINTSTRUCT()
            dc = self.user.BeginPaint(self.backend.popup_hwnd, ctypes.byref(paint))
            try:
                if dc:
                    self._paint(dc)
            finally:
                self.user.EndPaint(self.backend.popup_hwnd, ctypes.byref(paint))
            return 0
        if message == WM_CTLCOLORSTATIC:
            key = next((name for name, handle in self.controls.items() if handle == lparam), None)
            foreground, background = "foreground", "background"
            if key in ("body", "footer"):
                foreground = "muted"
            elif key == "badge":
                foreground, background = self._status_colors()
            elif key in ("countdown_label", "countdown"):
                foreground, background = "warning", "warning_background"
            self.gdi.SetTextColor(wparam, self.palette[foreground])
            self.gdi.SetBkColor(wparam, self.palette[background])
            self.gdi.SetBkMode(wparam, 1)
            return self._brush(background)
        if message == WM_DRAWITEM and lparam:
            return self._button(ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents)
        return None

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._owns_logo_icon and self._logo_icon:
            self.user.DestroyIcon(self._logo_icon)
        self._logo_icon = None
        self._owns_logo_icon = False
        if self.gdi is not None:
            for handle in set(self.fonts.values()) | set(self.brushes.values()) | set(self.pens.values()):
                if _handle(handle):
                    self.gdi.DeleteObject(handle)
        self.fonts.clear()
        self.brushes.clear()
        self.pens.clear()
