"""Presentation contracts with fake HWND/GDI handles; never operate the UPS."""
import ctypes
from contextlib import nullcontext
from itertools import count
import math
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from ups_monitor import notification_popup as popup


class PresentationValueTests(unittest.TestCase):
    def test_explicit_and_automatic_theme_resolution(self):
        for selected in ("light", "dark"):
            for system_light in (True, False):
                self.assertEqual(popup.resolve_theme(selected, system_light=system_light), selected)
        self.assertEqual(popup.resolve_theme("auto", system_light=True), "light")
        self.assertEqual(popup.resolve_theme("auto", system_light=False), "dark")
        for value in (None, "system", "", True, {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                popup.resolve_theme(value)

    def test_countdown_rounds_up_and_has_unambiguous_units(self):
        for seconds, expected in ((0, "00:00"), (0.01, "00:01"), (19, "00:19"),
                                  (59.1, "01:00"), (125, "02:05"), (3601, "1:00:01"),
                                  (-10, "00:00"), (100000, "24:00:00")):
            with self.subTest(seconds=seconds):
                self.assertEqual(popup.format_countdown(seconds), expected)
        for value in (True, "19", None, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assertEqual(popup.format_countdown(value), "—:—")

    def test_automatic_appearance_reads_windows_application_theme_and_tolerates_missing_key(self):
        registry = SimpleNamespace(HKEY_CURRENT_USER=1,
            OpenKey=Mock(side_effect=lambda *_args: nullcontext("theme-key")),
            QueryValueEx=Mock(return_value=(0, 4)))
        with patch.dict(sys.modules, {"winreg": registry}):
            self.assertEqual(popup.resolve_theme("auto"), "dark")
            registry.QueryValueEx.assert_called_once_with("theme-key", "AppsUseLightTheme")
            registry.QueryValueEx.return_value = (1, 4)
            self.assertEqual(popup.resolve_theme("auto"), "light")
            registry.OpenKey.side_effect = OSError("No existe el ajuste")
            self.assertEqual(popup.resolve_theme("auto"), "light")


class PopupPresentationTests(unittest.TestCase):
    def setUp(self):
        self.user, self.gdi = Mock(), Mock()
        self.user.CreateWindowExW.side_effect = count(100)
        self.user.SetWindowPos.return_value = True
        self.user.GetDC.return_value = 0  # Deterministic text measurement fallback.
        self.gdi.CreateFontW.side_effect = count(1000)
        self.gdi.CreateSolidBrush.side_effect = count(2000)
        self.gdi.CreatePen.side_effect = count(3000)
        self.gdi.SaveDC.return_value = 1
        self.backend = SimpleNamespace(user=self.user, gdi=self.gdi, popup_hwnd=50,
            instance=51, popup_dpi=96, _load_icon=Mock(return_value=52))
        self.backend._scale = lambda value: max(1, round(value * self.backend.popup_dpi / 96))
        with patch.object(popup, "resolve_theme", return_value="light"):
            self.view = popup.PopupView(self.backend)
        self.view.create_controls()
        self.addCleanup(self.view.close)

    @staticmethod
    def countdown(**changes):
        return {"countdown_id": "a" * 32, "action": "hibernate", "remaining_seconds": 30,
                "valid_for_ms": 3000, "cancelable": True, **changes}

    def test_existing_logo_is_borrowed_and_never_destroyed_by_view(self):
        self.backend._load_icon.assert_called_once_with()
        self.user.SendMessageW.assert_any_call(self.view.controls["logo"], popup.STM_SETICON, 52, 0)
        self.view.close()
        self.user.DestroyIcon.assert_not_called()

    def test_owned_logo_is_loaded_at_each_dpi_and_released_once(self):
        self.user.LoadImageW.reset_mock()
        self.user.LoadImageW.side_effect = [9000, 9001]
        self.backend._load_icon.reset_mock()
        with patch.object(popup, "resolve_theme", return_value="light"):
            view = popup.PopupView(self.backend)
        self.addCleanup(view.close)
        view.create_controls()
        self.assertEqual(self.user.LoadImageW.call_args.args[3:5], (38, 38))
        self.user.SendMessageW.assert_any_call(view.controls["logo"], popup.STM_SETICON, 9000, 0)
        view.set_fonts()
        self.user.LoadImageW.assert_called_once()
        self.backend.popup_dpi = 144
        view.set_fonts()
        self.assertEqual(self.user.LoadImageW.call_count, 2)
        self.assertEqual(self.user.LoadImageW.call_args.args[3:5], (57, 57))
        self.user.DestroyIcon.assert_called_once_with(9000)
        self.backend._load_icon.assert_not_called()
        view.close()
        view.close()
        self.assertEqual([call.args[0] for call in self.user.DestroyIcon.call_args_list], [9000, 9001])

    def test_same_content_palette_fonts_and_layout_do_not_repeat_native_work(self):
        self.view.set_content("Batería baja", "Revisa el estado del UPS.", severity="critical")
        self.view.layout(False)
        self.user.SetWindowTextW.reset_mock()
        self.user.ShowWindow.reset_mock()
        self.user.SetWindowPos.reset_mock()
        self.user.InvalidateRect.reset_mock()
        self.gdi.CreateFontW.reset_mock()
        self.gdi.CreateSolidBrush.reset_mock()
        self.assertFalse(self.view.set_content("Batería baja", "Revisa el estado del UPS.", severity="critical"))
        self.assertFalse(self.view.apply_theme("light"))
        self.view.set_fonts()
        self.view.layout(False)
        for function in (self.user.SetWindowTextW, self.user.ShowWindow, self.user.SetWindowPos,
                         self.user.InvalidateRect, self.gdi.CreateFontW, self.gdi.CreateSolidBrush):
            function.assert_not_called()

    def test_dpi_replaces_fonts_and_close_releases_every_resource_once(self):
        old_fonts = set(self.view.fonts.values())
        self.backend.popup_dpi = 192
        self.view.set_fonts()
        new_fonts = set(self.view.fonts.values())
        self.assertTrue(old_fonts.isdisjoint(new_fonts))
        old_brushes = set(self.view.brushes.values())
        self.view.apply_theme("dark")
        new_brushes = set(self.view.brushes.values())
        self.assertTrue(old_brushes.isdisjoint(new_brushes))
        self.view.close()
        self.view.close()
        deleted = [call.args[0] for call in self.gdi.DeleteObject.call_args_list]
        for handle in old_fonts | new_fonts | old_brushes | new_brushes:
            self.assertEqual(deleted.count(handle), 1)

    def test_failed_font_allocation_keeps_previous_fonts_and_frees_partial_set(self):
        previous = dict(self.view.fonts)
        self.backend.popup_dpi = 144
        self.gdi.CreateFontW.side_effect = [9000, 0]
        self.view.set_fonts()
        self.assertEqual(self.view.fonts, previous)
        self.gdi.DeleteObject.assert_called_once_with(9000)

    def test_failed_palette_allocation_preserves_previous_palette_and_frees_partial_set(self):
        previous_palette, previous_brushes = dict(self.view.palette), dict(self.view.brushes)
        allocations = iter((9000, 9001))
        self.gdi.CreateSolidBrush.side_effect = lambda *_args: next(allocations, 0)
        self.assertFalse(self.view.apply_theme("dark"))
        self.assertEqual(self.view.palette, previous_palette)
        self.assertEqual(self.view.brushes, previous_brushes)
        deleted = [call.args[0] for call in self.gdi.DeleteObject.call_args_list]
        self.assertEqual(deleted.count(9000), 1)
        self.assertEqual(deleted.count(9001), 1)
        self.assertTrue(set(previous_brushes.values()).isdisjoint(deleted))

    def test_initial_palette_failure_releases_partial_resources_and_rejects_undrawable_view(self):
        failing_gdi = Mock()
        failing_gdi.CreateSolidBrush.side_effect = [9000, 0]
        backend = SimpleNamespace(**{**vars(self.backend), "gdi": failing_gdi})
        with patch.object(popup, "resolve_theme", return_value="light"), self.assertRaises(OSError):
            popup.PopupView(backend)
        failing_gdi.DeleteObject.assert_called_once_with(9000)

    def test_countdown_progress_keeps_original_episode_duration(self):
        self.view.set_content("Protección", "Texto", countdown=self.countdown())
        self.assertEqual(self.view.texts["countdown"], "00:30")
        self.assertEqual(self.view.texts["badge"], "URGENTE")
        self.assertEqual(self.view._countdown_total, 30)
        self.view.set_content("Protección", "Texto", countdown=self.countdown(remaining_seconds=19))
        self.assertEqual(self.view._countdown_total, 30)
        self.view.set_content("Protección", "Texto", countdown=self.countdown(countdown_id="b" * 32,
            remaining_seconds=10))
        self.assertEqual(self.view._countdown_total, 10)

    def capture_invalidations(self):
        invalidations = []

        def record(hwnd, rectangle, erase):
            copied = None
            if rectangle is not None:
                value = ctypes.cast(rectangle, ctypes.POINTER(popup.RECT)).contents
                copied = (value.left, value.top, value.right, value.bottom)
            invalidations.append((hwnd, copied, bool(erase)))
            return True

        self.user.InvalidateRect.reset_mock()
        self.user.InvalidateRect.side_effect = record
        return invalidations

    def prepare_countdown(self):
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown())
        self.view.layout(True)
        self.user.SetWindowTextW.reset_mock()
        self.user.ShowWindow.reset_mock()
        self.user.SetWindowPos.reset_mock()
        return self.capture_invalidations()

    def test_countdown_tick_repaints_only_digits_and_progress_without_background_erase(self):
        invalidations = self.prepare_countdown()
        self.assertTrue(self.view.set_content("Protección", "Guarda tu trabajo.",
            countdown=self.countdown(remaining_seconds=29, valid_for_ms=2000)))
        self.user.SetWindowTextW.assert_called_once_with(self.view.controls["countdown"], "00:29")
        self.user.ShowWindow.assert_not_called()
        self.user.SetWindowPos.assert_not_called()
        self.assertTrue(invalidations, "A visible countdown tick must request repainting")
        for hwnd, rectangle, erase in invalidations:
            self.assertFalse(erase, "Repeated countdown frames must not erase an intermediate background")
            self.assertIn(hwnd, (self.backend.popup_hwnd, self.view.controls["countdown"]))
            if hwnd == self.backend.popup_hwnd:
                self.assertIsNotNone(rectangle, "A tick must never invalidate the complete parent window")
                x, y, width, height = self.view.bounds["progress"]
                expected = (self.backend._scale(x), self.backend._scale(y),
                            self.backend._scale(x + width), self.backend._scale(y + height))
                self.assertEqual(rectangle, expected)

    def test_fractional_countdown_progress_does_not_redraw_unchanged_digit(self):
        invalidations = self.prepare_countdown()
        self.assertTrue(self.view.set_content("Protección", "Guarda tu trabajo.",
            countdown=self.countdown(remaining_seconds=29.5, valid_for_ms=2500)))
        self.assertEqual(self.view.texts["countdown"], "00:30")
        self.user.SetWindowTextW.assert_not_called()
        self.assertTrue(invalidations)
        self.assertTrue(all(hwnd == self.backend.popup_hwnd and rectangle is not None and not erase
                            for hwnd, rectangle, erase in invalidations))

    def test_metadata_and_subpixel_changes_do_not_request_another_frame(self):
        self.prepare_countdown()
        self.view.set_content("Protección", "Guarda tu trabajo.",
            countdown=self.countdown(remaining_seconds=19.5, valid_for_ms=2000))
        invalidations = self.capture_invalidations()
        self.user.SetWindowTextW.reset_mock()
        self.assertFalse(self.view.set_content("Protección", "Guarda tu trabajo.",
            countdown=self.countdown(remaining_seconds=19.4999, valid_for_ms=1999)))
        self.assertFalse(invalidations)
        self.user.SetWindowTextW.assert_not_called()
        self.assertAlmostEqual(self.view.countdown["remaining_seconds"], 19.4999,
                               msg="Latest source metadata is retained even when it does not affect a pixel")

    def test_expiry_still_updates_uncertainty_and_requests_a_complete_presentation(self):
        invalidations = self.prepare_countdown()
        self.assertTrue(self.view.set_content("Protección", "No se confirma el estado.",
            countdown=self.countdown(remaining_seconds=29, valid_for_ms=0)))
        self.assertEqual(self.view.texts["title"], "Actualización interrumpida")
        self.assertEqual(self.view.texts["countdown"], "—:—")
        self.assertEqual(self.view.texts["badge"], "SIN DATOS RECIENTES")
        self.assertEqual(self.view.texts["countdown_label"], "Último estado")
        self.assertTrue(any(hwnd == self.backend.popup_hwnd and rectangle is None
                            for hwnd, rectangle, _erase in invalidations))

    def test_clock_is_owner_drawn_while_retaining_native_accessible_text(self):
        creation = next(call.args for call in self.user.CreateWindowExW.call_args_list
                        if call.args[2] == "00:00")
        self.assertEqual(creation[1], "STATIC")
        self.assertEqual(creation[3] & 0x1F, 0x0D, "SS_OWNERDRAW avoids separate text erase/paint passes")
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown(remaining_seconds=19))
        self.user.SetWindowTextW.assert_any_call(self.view.controls["countdown"], "00:19")

    def test_clock_text_update_disables_intermediate_redraw_then_invalidates_without_erase(self):
        self.prepare_countdown()
        clock = self.view.controls["countdown"]
        self.user.reset_mock()
        self.view.set_content("Protección", "Guarda tu trabajo.",
            countdown=self.countdown(remaining_seconds=29))
        operations = [item for item in self.user.mock_calls if item[0] in ("SendMessageW", "SetWindowTextW")]
        self.assertEqual([(item[0], item.args) for item in operations], [
            ("SendMessageW", (clock, popup.WM_SETREDRAW, 0, 0)),
            ("SetWindowTextW", (clock, "00:29")),
            ("SendMessageW", (clock, popup.WM_SETREDRAW, 1, 0)),
        ])
        self.user.InvalidateRect.assert_any_call(clock, None, False)

    def test_unchanged_cancel_button_state_is_not_set_or_enabled_again(self):
        cancel = self.view.controls["cancel"]
        self.view.set_action("cancel", "Cancelar protección", True)
        self.user.SetWindowTextW.reset_mock()
        self.user.EnableWindow.reset_mock()
        self.view.set_action("cancel", "Cancelar protección", True)
        self.user.SetWindowTextW.assert_not_called()
        self.user.EnableWindow.assert_not_called()
        self.view.set_action("cancel", "Cancelando…", False)
        self.user.SetWindowTextW.assert_called_once_with(cancel, "Cancelando…")
        self.user.EnableWindow.assert_called_once_with(cancel, False)
        self.user.SetWindowTextW.reset_mock()
        self.user.EnableWindow.reset_mock()
        self.view.set_action("cancel", "Cancelando…", False)
        self.user.SetWindowTextW.assert_not_called()
        self.user.EnableWindow.assert_not_called()

    def test_countdown_frames_reuse_layout_without_measuring_or_moving_static_content(self):
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown())
        first_height = self.view.layout(True)
        with patch.object(self.view, "_measure", wraps=self.view._measure) as measure:
            self.user.GetDC.reset_mock()
            self.user.SetWindowPos.reset_mock()
            self.view.set_content("Protección", "Guarda tu trabajo.",
                countdown=self.countdown(remaining_seconds=29))
            self.assertFalse(self.view.layout_dirty)
            self.assertEqual(self.view.layout(True), first_height)
            measure.assert_not_called()
            self.user.GetDC.assert_not_called()
            self.user.SetWindowPos.assert_not_called()
            self.view.set_content("Protección", "No se confirmó la cancelación. Comprueba el estado.",
                countdown=self.countdown(remaining_seconds=29))
            self.assertTrue(self.view.layout_dirty)
            self.view.layout(True)
            self.assertTrue(measure.called, "Changed feedback must still reflow when needed")

    def prepare_paint_buffer(self):
        self.user.BeginPaint.return_value = 77

        def client_rectangle(_hwnd, output):
            rectangle = ctypes.cast(output, ctypes.POINTER(popup.RECT)).contents
            rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = 0, 0, 480, 400
            return True

        self.user.GetClientRect.side_effect = client_rectangle
        self.gdi.CreateCompatibleDC.return_value = 88
        self.gdi.CreateCompatibleBitmap.return_value = 99
        self.gdi.SelectObject.return_value = 66
        self.gdi.BitBlt.return_value = True
        self.gdi.DeleteObject.reset_mock()
        self.gdi.DeleteDC.reset_mock()

    def test_background_erase_is_acknowledged_without_drawing_an_intermediate_frame(self):
        with patch.object(self.view, "_paint") as paint:
            self.assertEqual(self.view.handle_message(popup.WM_ERASEBKGND, 77, 0), 1)
        paint.assert_not_called()
        self.user.FillRect.assert_not_called()

    def test_window_paint_presents_one_complete_buffer_and_releases_temporary_gdi_resources(self):
        self.prepare_paint_buffer()
        with patch.object(self.view, "_paint") as paint:
            self.assertEqual(self.view.handle_message(popup.WM_PAINT, 0, 0), 0)
        paint.assert_called_once_with(88)
        self.gdi.CreateCompatibleDC.assert_called_once_with(77)
        self.gdi.CreateCompatibleBitmap.assert_called_once_with(77, 480, 400)
        self.gdi.BitBlt.assert_called_once_with(77, 0, 0, 480, 400, 88, 0, 0, 0x00CC0020)
        self.gdi.SelectObject.assert_any_call(88, 66)
        self.gdi.DeleteObject.assert_called_once_with(99)
        self.gdi.DeleteDC.assert_called_once_with(88)
        self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_buffered_paint_failure_still_releases_bitmap_dc_and_ends_paint(self):
        self.prepare_paint_buffer()
        with patch.object(self.view, "_paint", side_effect=OSError("falló el dibujo")):
            with self.assertRaisesRegex(OSError, "falló el dibujo"):
                self.view.handle_message(popup.WM_PAINT, 0, 0)
        self.gdi.BitBlt.assert_not_called()
        self.gdi.SelectObject.assert_any_call(88, 66)
        self.gdi.DeleteObject.assert_called_once_with(99)
        self.gdi.DeleteDC.assert_called_once_with(88)
        self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_clock_presents_text_and_background_in_one_right_aligned_buffer(self):
        self.prepare_paint_buffer()
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown(remaining_seconds=19))
        item = popup.DRAWITEMSTRUCT()
        item.CtlType, item.CtlID = 5, 104  # ODT_STATIC and the native clock identifier.
        item.hwndItem, item.hDC = self.view.controls["countdown"], 77
        item.rcItem = popup.RECT(0, 0, 204, 49)
        self.user.DrawTextW.reset_mock()
        self.assertEqual(self.view.handle_message(popup.WM_DRAWITEM, 104, ctypes.addressof(item)), 1)
        self.gdi.CreateCompatibleBitmap.assert_called_once_with(77, 204, 49)
        self.user.DrawTextW.assert_called_once_with(88, "00:19", -1, ANY,
            popup.DT_RIGHT | popup.DT_VCENTER | popup.DT_SINGLELINE | popup.DT_NOPREFIX)
        self.gdi.SelectObject.assert_any_call(88, self.view.fonts["timer"])
        self.gdi.BitBlt.assert_called_once_with(77, 0, 0, 204, 49, 88, 0, 0, 0x00CC0020)
        self.gdi.DeleteObject.assert_called_once_with(99)
        self.gdi.DeleteDC.assert_called_once_with(88)
        unknown = popup.DRAWITEMSTRUCT()
        unknown.CtlType, unknown.CtlID = 5, 104
        unknown.hwndItem, unknown.hDC = 999, 77
        self.assertIsNone(self.view.handle_message(popup.WM_DRAWITEM, 104, ctypes.addressof(unknown)))

    def test_failed_memory_dc_uses_direct_paint_without_leaking_resources(self):
        self.prepare_paint_buffer()
        self.gdi.CreateCompatibleDC.return_value = 0
        with patch.object(self.view, "_paint") as paint:
            self.assertEqual(self.view.handle_message(popup.WM_PAINT, 0, 0), 0)
        paint.assert_called_once_with(77)
        self.gdi.CreateCompatibleBitmap.assert_not_called()
        self.gdi.BitBlt.assert_not_called()
        self.gdi.DeleteObject.assert_not_called()
        self.gdi.DeleteDC.assert_not_called()
        self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_failed_buffer_bitmap_uses_direct_paint_and_releases_memory_dc(self):
        self.prepare_paint_buffer()
        self.gdi.CreateCompatibleBitmap.return_value = 0
        with patch.object(self.view, "_paint") as paint:
            self.assertEqual(self.view.handle_message(popup.WM_PAINT, 0, 0), 0)
        paint.assert_called_once_with(77)
        self.gdi.SelectObject.assert_not_called()
        self.gdi.BitBlt.assert_not_called()
        self.gdi.DeleteObject.assert_not_called()
        self.gdi.DeleteDC.assert_called_once_with(88)
        self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_failed_bitmap_selection_never_restores_an_invalid_gdi_object(self):
        for failure in (0, -1, ctypes.c_void_p(-1).value):
            with self.subTest(failure=failure):
                self.prepare_paint_buffer()
                self.user.EndPaint.reset_mock()
                self.gdi.BitBlt.reset_mock()
                self.gdi.SelectObject.reset_mock()
                self.gdi.SelectObject.return_value = failure
                with patch.object(self.view, "_paint") as paint:
                    self.assertEqual(self.view.handle_message(popup.WM_PAINT, 0, 0), 0)
                paint.assert_called_once_with(77)
                self.gdi.SelectObject.assert_called_once_with(88, 99)
                self.gdi.BitBlt.assert_not_called()
                self.gdi.DeleteObject.assert_called_once_with(99)
                self.gdi.DeleteDC.assert_called_once_with(88)
                self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_failed_buffer_copy_falls_back_to_current_frame_and_releases_resources(self):
        self.prepare_paint_buffer()
        self.gdi.BitBlt.return_value = False
        with patch.object(self.view, "_paint") as paint:
            self.assertEqual(self.view.handle_message(popup.WM_PAINT, 0, 0), 0)
        self.assertEqual([item.args[0] for item in paint.call_args_list], [88, 77])
        self.gdi.SelectObject.assert_any_call(88, 66)
        self.gdi.DeleteObject.assert_called_once_with(99)
        self.gdi.DeleteDC.assert_called_once_with(88)
        self.user.EndPaint.assert_called_once_with(self.backend.popup_hwnd, ANY)

    def test_expired_countdown_shows_uncertainty_instead_of_claiming_completion(self):
        self.view.set_content("Protección", "No se confirma el estado.", countdown=self.countdown(valid_for_ms=0))
        self.assertEqual(self.view.texts["title"], "Actualización interrumpida")
        self.assertEqual(self.view.texts["badge"], "SIN DATOS RECIENTES")
        self.assertEqual(self.view.texts["countdown"], "—:—")
        self.assertEqual(self.view.texts["countdown_label"], "Último estado")
        self.assertIn("no cancela la protección", self.view.texts["footer"])

    def test_brief_refresh_gap_preserves_countdown_identity_and_layout(self):
        message = "Guarda tu trabajo antes de que termine la cuenta."
        footer = "Entendido no cancela la protección."
        self.view.set_content("Protección", message, countdown=self.countdown(), footer=footer)
        original_height = self.view.layout(True)
        original_bounds = dict(self.view.bounds)
        invalidations = self.capture_invalidations()
        self.user.SetWindowTextW.reset_mock()
        self.user.SetWindowPos.reset_mock()
        self.assertTrue(self.view.set_content("Protección", message,
            countdown=self.countdown(remaining_seconds=29.5, valid_for_ms=0, refresh_pending=True), footer=footer))
        self.assertEqual(self.view.texts["title"], "Hibernación programada")
        self.assertEqual(self.view.texts["badge"], "ACTUALIZANDO")
        self.assertEqual(self.view.texts["countdown_label"], "Tiempo estimado")
        self.assertEqual(self.view.texts["countdown"], "00:30")
        self.assertEqual(self.view.texts["body"], message)
        self.assertEqual(self.view.texts["footer"], footer)
        self.assertFalse(self.view.layout_dirty)
        self.assertEqual(self.view.layout(True), original_height)
        self.assertEqual(self.view.bounds, original_bounds)
        self.user.SetWindowPos.assert_not_called()
        self.assertTrue(invalidations)
        self.assertFalse(any(hwnd == self.backend.popup_hwnd and rectangle is None
                             for hwnd, rectangle, _erase in invalidations))
        self.assertGreater(self.view._progress_pixels(), 0)
        changed_handles = {call.args[0] for call in self.user.SetWindowTextW.call_args_list}
        self.assertEqual(changed_handles, {self.view.controls["badge"], self.view.controls["countdown_label"]})

    def test_fresh_confirmation_restores_labels_without_recreating_countdown(self):
        message = "Guarda tu trabajo antes de que termine la cuenta."
        footer = "Entendido no cancela la protección."
        self.view.set_content("Protección", message, countdown=self.countdown(), footer=footer)
        height = self.view.layout(True)
        controls = dict(self.view.controls)
        self.view.set_content("Protección", message,
            countdown=self.countdown(remaining_seconds=27, valid_for_ms=0, refresh_pending=True), footer=footer)
        self.user.SetWindowPos.reset_mock()
        self.view.set_content("Protección", message,
            countdown=self.countdown(remaining_seconds=26.9, valid_for_ms=3000), footer=footer)
        self.assertEqual(self.view.texts["badge"], "URGENTE")
        self.assertEqual(self.view.texts["countdown_label"], "Tiempo restante")
        self.assertEqual(self.view.texts["countdown"], "00:27")
        self.assertEqual(self.view.texts["title"], "Hibernación programada")
        self.assertEqual(self.view.controls, controls)
        self.assertEqual(self.view._countdown_total, 30)
        self.assertFalse(self.view.layout_dirty)
        self.assertEqual(self.view.layout(True), height)
        self.user.SetWindowPos.assert_not_called()

    def test_zero_is_waiting_for_confirmation_even_if_refresh_pending(self):
        self.view.set_content("Protección", "La cuenta terminó.",
            countdown=self.countdown(remaining_seconds=0, valid_for_ms=0, refresh_pending=True))
        self.assertEqual(self.view.texts["title"], "Esperando confirmación")
        self.assertEqual(self.view.texts["badge"], "CONFIRMANDO")
        self.assertEqual(self.view.texts["countdown"], "00:00")
        self.assertEqual(self.view.texts["countdown_label"], "Cuenta finalizada")
        self.assertNotIn("completada", self.view.texts["title"])
        self.assertNotIn("hibernada", self.view.texts["title"])

    def test_shutdown_refresh_gap_keeps_shutdown_heading(self):
        self.view.set_content("Protección", "Guarda tu trabajo antes de que termine la cuenta.",
            countdown=self.countdown(action="shutdown", remaining_seconds=125, valid_for_ms=0,
                                     refresh_pending=True), footer="Entendido no cancela la protección.")
        self.assertEqual(self.view.texts["title"], "Apagado programado")
        self.assertEqual(self.view.texts["countdown"], "02:05")
        self.assertEqual(self.view.texts["countdown_label"], "Tiempo estimado")
        self.assertEqual(self.view.texts["badge"], "ACTUALIZANDO")

    def test_long_message_is_constrained_without_hiding_actions(self):
        message = "El monitor no pudo confirmar la cancelación de la protección. " * 16
        self.view.set_content("Hibernación programada", message, countdown=self.countdown())
        height = self.view.layout(True, width=320, max_height=390)
        self.assertLessEqual(height, 390)
        self.assertEqual(self.view.message, message)
        self.assertTrue(self.view.texts["body"].endswith("…"))
        self.assertLess(len(self.view.texts["body"]), len(message))
        for key in ("cancel", "open", "ack", "footer"):
            x, y, width, child_height = self.view.bounds[key]
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 320)
            self.assertLessEqual(y + child_height, height)

    def test_short_notice_does_not_reserve_countdown_space(self):
        self.view.set_content("Corte de energía", "El UPS usa batería.")
        standard_height = self.view.layout(False)
        self.assertNotIn("countdown_box", self.view.bounds)
        self.assertNotIn("cancel", self.view.bounds)
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown())
        countdown_height = self.view.layout(True)
        self.assertGreater(countdown_height, standard_height)
        self.assertIn("countdown_box", self.view.bounds)

    def test_small_workarea_keeps_generic_notice_actions_inside_window(self):
        self.view.set_content("Revisa el estado del equipo y la comunicación con el monitor de protección",
                              "Abre el panel para consultar todos los detalles. " * 12)
        height = self.view.layout(False, width=240, max_height=280)
        self.assertLessEqual(height, 280)
        for key in ("open", "ack", "footer"):
            x, y, width, child_height = self.view.bounds[key]
            self.assertLessEqual(x + width, 240)
            self.assertLessEqual(y + child_height, height)

    def test_extreme_workarea_preserves_clock_and_actions_without_overlap(self):
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown())
        height = self.view.layout(True, width=240, max_height=280)
        self.assertLessEqual(height, 280)
        for key in ("logo", "brand", "title", "countdown", "cancel", "open", "ack", "footer"):
            x, y, width, child_height = self.view.bounds[key]
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + width, 240)
            self.assertLessEqual(y + child_height, height)
        label = self.view.bounds["countdown_label"]
        timer = self.view.bounds["countdown"]
        self.assertLessEqual(label[0] + label[2], timer[0])
        self.assertNotIn("body", self.view.bounds)
        self.assertFalse(self.view._visibility["badge"])
        self.assertIn("no cancela", self.view.texts["footer"])
        self.view.layout(True, width=480, max_height=600)
        self.assertTrue(self.view._visibility["body"])
        self.assertTrue(self.view._visibility["badge"])
        self.assertEqual(self.view.texts["footer"], self.view.footer)

    def test_native_measurement_releases_dc_and_restores_selected_font(self):
        self.user.GetDC.return_value = 77
        self.user.DrawTextW.return_value = 38
        self.assertEqual(self.view._measure("Dos líneas", 300, "body", 19), 38)
        self.gdi.RestoreDC.assert_called_with(77, 1)
        self.user.ReleaseDC.assert_called_once_with(50, 77)

    def test_owner_draw_buttons_preserve_labels_focus_and_disabled_state(self):
        self.view.set_content("Protección", "Guarda tu trabajo.", countdown=self.countdown())
        for identity, key in ((101, "open"), (102, "ack"), (103, "cancel")):
            with self.subTest(identity=identity):
                item = popup.DRAWITEMSTRUCT()
                item.CtlType, item.CtlID = 4, identity
                item.hwndItem, item.hDC = self.view.controls[key], 99
                item.rcItem = popup.RECT(0, 0, 300, 42)
                item.itemState = popup.ODS_FOCUS | popup.ODS_DISABLED
                self.assertEqual(self.view.handle_message(popup.WM_DRAWITEM, identity, ctypes.addressof(item)), 1)
                self.user.DrawTextW.assert_called_with(99, self.view.texts[key], -1,
                    ANY, popup.DT_CENTER | popup.DT_VCENTER | popup.DT_SINGLELINE | popup.DT_NOPREFIX)
                self.gdi.SetTextColor.assert_called_with(99, self.view.palette["disabled_text"])
                self.user.DrawFocusRect.assert_called()
        unknown = popup.DRAWITEMSTRUCT()
        unknown.CtlType, unknown.CtlID = 4, 999
        self.assertIsNone(self.view.handle_message(popup.WM_DRAWITEM, 999, ctypes.addressof(unknown)))


if __name__ == "__main__":
    unittest.main()
