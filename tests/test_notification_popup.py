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

    def test_expired_countdown_shows_uncertainty_instead_of_claiming_completion(self):
        self.view.set_content("Protección", "No se confirma el estado.", countdown=self.countdown(valid_for_ms=0))
        self.assertEqual(self.view.texts["title"], "Estado sin confirmar")
        self.assertEqual(self.view.texts["badge"], "SIN CONFIRMAR")
        self.assertEqual(self.view.texts["countdown"], "—:—")
        self.assertIn("no cancela la protección", self.view.texts["footer"])

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
