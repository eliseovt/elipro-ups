import ctypes
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ups_monitor import windows_notifications as wn


class LoopBackend:
    instances = []

    def __init__(self, owner):
        self.owner = owner
        self.wakeup = threading.Event()
        self.stopped = threading.Event()
        self.delivered = []
        self.closed = False
        self.instances.append(self)

    def initialize(self):
        pass

    def run(self):
        while not self.stopped.is_set():
            self.wakeup.wait(.1)
            self.wakeup.clear()
            self.owner._drain(self)

    def wake(self):
        self.wakeup.set()
        return True

    def deliver(self, *item):
        self.delivered.append(item)

    def request_stop(self):
        self.stopped.set()
        self.wakeup.set()

    def close(self):
        self.closed = True


class WindowsNotifierTests(unittest.TestCase):
    def make_notifier(self):
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765/")
        self.addCleanup(notifier.stop)
        return notifier

    def wait_until(self, condition):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail("Notification test worker did not finish")

    def test_no_windows_is_available_without_loading_native_libraries(self):
        notifier = self.make_notifier()
        with patch.object(wn.os, "name", "posix"), patch.object(wn, "_Win32Backend") as factory:
            notifier.start()
            self.assertFalse(notifier.status()["available"])
            self.assertFalse(notifier.notify("Prueba", "Mensaje"))
            self.assertIn("Windows", notifier.status()["last_error"])
            factory.assert_not_called()

    def test_start_notify_stop_and_restart_use_worker_not_caller_thread(self):
        notifier = self.make_notifier()
        with patch.object(wn.os, "name", "nt"), patch.object(wn, "_Win32Backend", LoopBackend):
            notifier.start()
            backend = LoopBackend.instances[-1]
            notifier.start()
            self.assertIs(notifier._backend, backend)
            self.assertTrue(notifier.status()["ready"])
            self.assertTrue(notifier.notify("Corte", "UPS en batería", "warning", False))
            self.wait_until(lambda: bool(backend.delivered))
            self.assertEqual(backend.delivered, [("Corte", "UPS en batería", "warning", False)])
            self.assertIsNotNone(notifier.status()["last_delivered_at"])
            notifier.stop()
            self.assertTrue(backend.closed)
            self.assertFalse(notifier.status()["ready"])
            self.assertFalse(notifier.notify("Corte", "Después de detener"))
            notifier.start()
            self.assertIsNot(notifier._backend, backend)
            notifier.stop()

    def test_startup_failure_is_observable_and_does_not_break_caller(self):
        notifier = self.make_notifier()
        with patch.object(wn.os, "name", "nt"), patch.object(wn, "_Win32Backend", side_effect=OSError("sin Explorer")):
            notifier.start()
            notifier.stop()
            self.assertFalse(notifier.status()["ready"])
            self.assertEqual(notifier.status()["last_error"], "sin Explorer")

    def test_queue_is_bounded_and_does_not_call_native_delivery_inline(self):
        notifier = self.make_notifier()
        notifier._ready = True
        notifier._backend = Mock()
        for _ in range(32):
            self.assertTrue(notifier.notify("Título", "Mensaje"))
        self.assertFalse(notifier.notify("Título", "Cola llena"))
        self.assertIn("llena", notifier.status()["last_error"])
        notifier._backend.deliver.assert_not_called()
        notifier._backend = None

    def test_delivery_failure_is_reported_without_retry_or_fake_success(self):
        notifier = self.make_notifier()
        notifier._queue.put(("Corte", "Batería", "warning", True))
        backend = Mock()
        backend.deliver.side_effect = OSError("Shell rechazó aviso")
        notifier._drain(backend)
        self.assertEqual(backend.deliver.call_count, 1)
        self.assertIsNone(notifier.status()["last_delivered_at"])
        self.assertEqual(notifier.status()["last_error"], "Shell rechazó aviso")
        self.assertTrue(notifier._queue.empty())

    def test_panel_link_only_allows_local_http_and_is_opened_explicitly(self):
        for url in ("https://127.0.0.1:8765", "http://example.com", "file:///etc/hosts",
                    "http://user:pass@localhost:8765", "http://127.0.0.1:0"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                wn.WindowsNotifier(url)
        notifier = self.make_notifier()
        with patch.object(wn.webbrowser, "open", return_value=True) as browser:
            notifier._open_panel()
        browser.assert_called_once_with("http://127.0.0.1:8765/", new=2)

    def test_invalid_notification_input_is_rejected(self):
        notifier = self.make_notifier()
        for args in (("", "body"), ("title", ""), ("title", "body", "bad")):
            with self.assertRaises(ValueError):
                notifier.notify(*args)

    def test_session_tray_labels_are_explicit_and_default_labels_stay_compatible(self):
        monitor = self.make_notifier()
        self.assertEqual(monitor.exit_label, wn.DEFAULT_EXIT_LABEL)
        self.assertEqual(monitor.exit_message, wn.DEFAULT_EXIT_MESSAGE)
        self.assertEqual(monitor.tray_tip, wn.DEFAULT_TRAY_TIP)
        session = wn.WindowsNotifier("http://127.0.0.1:8765/",
            exit_label="Cerrar avisos de esta sesión…",
            exit_message="El servicio seguirá vigilando el UPS. ¿Cerrar avisos?",
            tray_tip="Elipro UPS · avisos de esta sesión")
        self.assertIn("servicio seguirá", session.exit_message)
        with self.assertRaises(ValueError):
            wn.WindowsNotifier("http://127.0.0.1:8765/", exit_label="")

    def test_persistent_queue_generation_rejects_resolved_previous_incident(self):
        notifier = self.make_notifier()
        notifier._ready = True
        notifier.sync_persistent({"battery_low": {"title": "Baja", "message": "Antes"}})
        self.assertTrue(notifier.notify("Baja", "Antes", persistent=True, key="battery_low"))
        old = notifier._queue.get_nowait()
        notifier._queue.task_done()
        self.assertTrue(notifier._persistent_active(old[5], old[6]))
        notifier.sync_persistent({})
        notifier.sync_persistent({"battery_low": {"title": "Baja", "message": "Nuevo"}})
        self.assertFalse(notifier._persistent_active(old[5], old[6]))
        notifier._ack_persistent("battery_low")
        self.assertTrue(notifier._persistent_acknowledged("battery_low"))
        notifier.sync_persistent({})
        notifier.sync_persistent({"battery_low": {"title": "Baja", "message": "Otro"}})
        self.assertFalse(notifier._persistent_acknowledged("battery_low"))

    def test_exit_callback_runs_off_native_loop_and_failure_is_reported(self):
        notifier = self.make_notifier()
        completed = threading.Event()
        called_threads = []
        def callback():
            called_threads.append(threading.current_thread())
            completed.set()
            raise RuntimeError("No cerró")
        notifier.exit_callback = callback
        notifier._request_exit()
        self.assertTrue(completed.wait(1))
        self.wait_until(lambda: notifier.status()["last_error"] is not None)
        self.assertNotEqual(called_threads[0], threading.current_thread())
        self.assertIn("No cerró", notifier.status()["last_error"])
        notifier._stop.set()
        notifier._request_exit()
        self.assertEqual(len(called_threads), 1)


class NativeShellTests(unittest.TestCase):
    def setUp(self):
        self.owner = Mock()
        self.owner.url = "http://127.0.0.1:8765"
        self.owner.cancel_callback = Mock()
        self.owner._stop = threading.Event()
        self.user, self.shell, self.kernel = Mock(), Mock(), Mock()
        self.user.RegisterWindowMessageW.return_value = 0xC001
        self.user.LoadImageW.return_value = 0
        self.user.LoadIconW.return_value = 0x123456789
        self.user.RegisterClassW.return_value = 1
        self.user.CreateWindowExW.return_value = 0x123456789ABC
        self.kernel.GetModuleHandleW.return_value = 0x100000000
        self.shell.Shell_NotifyIconW.return_value = 1
        self.native_calls = []

        def record(operation, pointer):
            source = ctypes.cast(pointer, ctypes.POINTER(wn.NOTIFYICONDATAW)).contents
            self.native_calls.append((operation, wn.NOTIFYICONDATAW.from_buffer_copy(source)))
            return 1

        self.shell.Shell_NotifyIconW.side_effect = record
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        with patch.object(wn, "_load_apis", return_value=(self.user, self.shell, self.kernel)):
            self.backend = wn._Win32Backend(self.owner)
        self.backend.initialize()
        self.addCleanup(self.backend.close)

    @staticmethod
    def query_accepts_notifications(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 5
        return 0

    def test_complete_unicode_structure_and_pointer_sized_window(self):
        self.assertEqual(ctypes.sizeof(wn.LRESULT), ctypes.sizeof(ctypes.c_void_p))
        data = self.native_calls[0][1]
        self.assertEqual(data.cbSize, ctypes.sizeof(wn.NOTIFYICONDATAW))
        self.assertEqual(data.hWnd, 0x123456789ABC)
        self.assertTrue(data.uFlags & wn.NIF_SHOWTIP)
        self.assertEqual(self.native_calls[1][0], wn.NIM_SETVERSION)
        self.assertEqual(self.native_calls[1][1].uVersion, 4)
        self.assertEqual(self.backend._data().hBalloonIcon, None)

    def test_file_icon_is_owned_reused_after_explorer_restart_and_released_once(self):
        # The initial fixture uses a shared fallback. A fresh backend exercises
        # the independent file handle without opening any real native window.
        self.user.LoadImageW.reset_mock()
        self.user.LoadImageW.return_value = 0xABCDEF123
        with patch.object(wn, "_load_apis", return_value=(self.user, self.shell, self.kernel)), \
                patch.object(wn, "resource_root", return_value=Path("packaged-resources")):
            backend = wn._Win32Backend(self.owner)
            backend.initialize()
        self.assertTrue(backend.owns_icon)
        self.assertEqual(backend.icon_handle, 0xABCDEF123)
        self.user.LoadImageW.assert_called_once_with(None,
            str(Path("packaged-resources") / "static" / "assets" / "elipro.ico"), 1, 0, 0, 0x50)
        backend._window_proc(backend.hwnd, backend.taskbar_created, 0, 0)
        self.assertEqual(self.user.LoadImageW.call_count, 1)
        self.assertEqual(self.native_calls[-2][1].hIcon, 0xABCDEF123)
        backend.close()
        self.user.DestroyIcon.assert_called_once_with(0xABCDEF123)
        backend.close()
        self.user.DestroyIcon.assert_called_once_with(0xABCDEF123)

    def test_missing_file_icon_uses_shared_system_icon_without_destroying_it(self):
        self.assertFalse(self.backend.owns_icon)
        self.assertEqual(self.native_calls[0][1].hIcon, 0x123456789)
        self.backend.close()
        self.user.DestroyIcon.assert_not_called()

    def test_file_icon_load_error_falls_back_without_preventing_notifications(self):
        self.user.LoadImageW.side_effect = OSError("file unavailable")
        with patch.object(wn, "_load_apis", return_value=(self.user, self.shell, self.kernel)):
            backend = wn._Win32Backend(self.owner)
            backend.initialize()
        self.assertFalse(backend.owns_icon)
        self.assertTrue(backend.icon_added)
        backend.close()
        self.user.DestroyIcon.assert_not_called()

    def test_initialization_failure_still_releases_the_owned_file_icon(self):
        self.user.LoadImageW.return_value = 0xABCDEF124
        self.shell.Shell_NotifyIconW.side_effect = None
        self.shell.Shell_NotifyIconW.return_value = 0
        with patch.object(wn, "_load_apis", return_value=(self.user, self.shell, self.kernel)):
            backend = wn._Win32Backend(self.owner)
        with self.assertRaises(OSError):
            backend.initialize()
        backend.close()
        self.user.DestroyIcon.assert_called_once_with(0xABCDEF124)

    def test_session_menu_and_tip_do_not_claim_exiting_stops_the_service(self):
        self.owner.tray_tip = "Elipro UPS · avisos de esta sesión"
        self.owner.exit_label = "Cerrar avisos de esta sesión…"
        self.owner.exit_message = "El servicio seguirá vigilando el UPS. ¿Cerrar avisos?"
        self.backend._add_icon()
        self.assertEqual(self.native_calls[-2][1].szTip, self.owner.tray_tip)
        self.user.TrackPopupMenu.return_value = 3
        self.user.MessageBoxW.return_value = 7
        self.backend._menu()
        self.user.AppendMenuW.assert_any_call(self.user.CreatePopupMenu.return_value, 0, 3, self.owner.exit_label)
        self.assertEqual(self.user.MessageBoxW.call_args.args[1], self.owner.exit_message)
        self.assertEqual(self.user.MessageBoxW.call_args.args[2], "Cerrar avisos de esta sesión")
        self.owner._request_exit.assert_not_called()

    def test_balloon_is_unicode_clipped_silent_and_sound_is_async_alias(self):
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        with patch.dict(sys.modules, {"winsound": sound}):
            self.backend.deliver("Título " + "x" * 100, "⚡" * 400, "critical", True)
        operation, data = self.native_calls[-1]
        self.assertEqual(operation, wn.NIM_MODIFY)
        self.assertEqual(len(data.szInfoTitle.encode("utf-16-le")), 126)
        self.assertLessEqual(len(data.szInfo.encode("utf-16-le")), 510)
        self.assertEqual(data.dwInfoFlags, 3 | wn.NIIF_NOSOUND)
        self.assertEqual(data.uFlags, wn.NIF_INFO | wn.NIF_REALTIME)
        sound.PlaySound.assert_called_once_with("SystemHand", 0x10003)
        self.owner._set_sound_error.assert_called_once_with(None)

    def test_sound_failure_preserves_accepted_visual_delivery(self):
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        sound.PlaySound.side_effect = RuntimeError("No se puede reproducir el sonido")
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765")
        self.backend.owner = notifier
        notifier._queue.put(("Prueba", "Aviso visible, sonido no disponible", "info", True))
        with patch.dict(sys.modules, {"winsound": sound}):
            notifier._drain(self.backend)
        status = notifier.status()
        self.assertIsNotNone(status["last_delivered_at"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["last_sound_error"], "No se puede reproducir el sonido")

    def test_disabled_sound_never_calls_winsound(self):
        sound = Mock()
        with patch.dict(sys.modules, {"winsound": sound}):
            self.backend.deliver("Prueba", "Sin sonido", "info", False)
        sound.PlaySound.assert_not_called()

    def test_shell_rejection_does_not_play_success_sound(self):
        self.shell.Shell_NotifyIconW.side_effect = None
        self.shell.Shell_NotifyIconW.return_value = 0
        sound = Mock()
        with patch.dict(sys.modules, {"winsound": sound}), self.assertRaises(OSError):
            self.backend.deliver("Corte", "Batería", "warning", True)
        sound.PlaySound.assert_not_called()

    def test_explorer_restart_readds_icon_and_version(self):
        before = len(self.native_calls)
        self.backend._window_proc(self.backend.hwnd, self.backend.taskbar_created, 0, 0)
        self.assertEqual([item[0] for item in self.native_calls[before:]], [wn.NIM_ADD, wn.NIM_SETVERSION])
        self.owner._set_ready.assert_called_with(True)

    def test_click_opens_panel_but_only_explicit_menu_command_cancels(self):
        self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONUSERCLICK)
        self.owner._open_panel.assert_called_once_with()
        self.owner.cancel_callback.assert_not_called()
        self.user.TrackPopupMenu.return_value = 0
        self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.WM_CONTEXTMENU)
        self.owner.cancel_callback.assert_not_called()
        self.user.TrackPopupMenu.return_value = 2
        self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.WM_CONTEXTMENU)
        self.owner.cancel_callback.assert_called_once_with()

    def test_shutdown_does_not_start_cancel_callback_from_open_menu(self):
        self.owner._stop.set()
        self.user.TrackPopupMenu.return_value = 2
        self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.WM_CONTEXTMENU)
        self.owner.cancel_callback.assert_not_called()

    def test_shell_callbacks_record_shown_hidden_and_timeout_without_user_claims(self):
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765")
        notifier._open_panel = Mock()
        self.backend.owner = notifier
        self.assertIsNone(notifier.status()["last_shown_at"])
        for native_event, name in ((wn.NIN_BALLOONSHOW, "shown"),
                                   (wn.NIN_BALLOONHIDE, "hidden"),
                                   (wn.NIN_BALLOONTIMEOUT, "timeout"),
                                   (wn.NIN_BALLOONUSERCLICK, "clicked")):
            with self.subTest(event=name):
                self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0,
                                          (1 << 16) | native_event)
                status = notifier.status()
                self.assertEqual(status["last_display_event"]["event"], name)
                key = "last_shown_at" if name == "shown" else "last_hidden_at"
                self.assertEqual(status[key], status["last_display_event"]["timestamp"])
        notifier._open_panel.assert_called_once_with()

    def test_read_only_user_notification_diagnostic_preserves_code_and_timestamp(self):
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765")
        self.backend.owner = notifier
        self.backend._query_user_notification_state()
        state = notifier.status()["user_notification_state"]
        self.assertEqual(state["state"], "accepts_notifications")
        self.assertEqual(state["code"], 5)
        self.assertIsNotNone(state["checked_at"])
        self.assertIsNone(state["error"])
        # Returned snapshots cannot mutate the recorded diagnostic.
        state["state"] = "tampered"
        self.assertEqual(notifier.status()["user_notification_state"]["state"], "accepts_notifications")

    def test_diagnostic_failure_does_not_fail_visual_delivery(self):
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765")
        self.backend.owner = notifier
        self.shell.SHQueryUserNotificationState.side_effect = None
        self.shell.SHQueryUserNotificationState.return_value = -2147467259
        notifier._queue.put(("Prueba", "Consulta opcional no disponible", "info", False))
        notifier._drain(self.backend)
        status = notifier.status()
        self.assertIsNotNone(status["last_delivered_at"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["user_notification_state"]["state"], "unknown")
        self.assertIn("80004005", status["user_notification_state"]["error"])

    def configure_popup(self):
        notifier = wn.WindowsNotifier("http://127.0.0.1:8765", cancel_callback=Mock())
        notifier._open_panel = Mock()
        self.backend.owner = notifier
        self.user.CreateWindowExW.side_effect = [0xE0000000001 + n for n in range(5)]
        self.user.SetWindowPos.return_value = 1
        self.user.SetTimer.return_value = 1

        def workarea(action, param, output, flags):
            rectangle = ctypes.cast(output, ctypes.POINTER(wn.RECT)).contents
            rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = 0, 0, 1920, 1040
            return 1

        self.user.SystemParametersInfoW.side_effect = workarea
        return notifier

    def test_fast_shell_timeout_uses_one_nonactivating_popup_and_timer(self):
        notifier = self.configure_popup()
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Aviso de escritorio", "warning", False)
        with patch.object(wn.time, "monotonic", return_value=100.04):
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONTIMEOUT)
        self.assertIsNotNone(notifier.status()["last_popup_shown_at"])
        self.user.SetTimer.assert_any_call(self.backend.popup_hwnd, 1, 12000, None)
        create_args = self.user.CreateWindowExW.call_args_list[1].args
        self.assertTrue(create_args[0] & 0x08000000)  # WS_EX_NOACTIVATE
        self.assertTrue(create_args[0] & 0x00000080)  # WS_EX_TOOLWINDOW
        self.assertTrue(create_args[0] & 0x00000008)  # WS_EX_TOPMOST
        self.assertEqual(create_args[6:8], (420, 190))
        self.assertTrue(self.user.SetWindowPos.call_args.args[-1] & 0x10)  # SWP_NOACTIVATE
        with patch.object(wn.time, "monotonic", return_value=100.05):
            self.backend._maybe_fallback_popup()
        self.assertEqual(self.user.SetTimer.call_count, 2)  # Shell check + popup expiry

    def test_normal_timeout_quiet_mode_click_or_stop_never_shows_fallback(self):
        self.configure_popup()
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Aviso", "info", False)
        for age, code, clicked, stopped in ((1.5, 5, False, False), (.05, 6, False, False),
                                          (.05, 5, True, False), (.05, 5, False, True)):
            with self.subTest(age=age, code=code, clicked=clicked, stopped=stopped):
                self.backend.last_notice["clicked"] = clicked
                self.backend.user_notification_code = code
                self.backend.owner._stop.clear()
                if stopped:
                    self.backend.owner._stop.set()
                with patch.object(wn.time, "monotonic", return_value=100 + age):
                    self.backend._maybe_fallback_popup()
        self.user.CreateWindowExW.assert_called_once()  # Only the original hidden window.

    def test_silent_shell_discard_falls_back_after_one_second(self):
        notifier = self.configure_popup()
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Shell no emitió callback", "info", False)
        with patch.object(wn.time, "monotonic", return_value=101.01):
            self.backend._window_proc(self.backend.hwnd, wn.WM_TIMER, 2, 0)
        self.assertIsNotNone(notifier.status()["last_popup_shown_at"])
        self.assertIsNone(notifier.status()["last_shown_at"])

    def test_confirmed_shell_display_has_no_second_popup(self):
        notifier = self.configure_popup()
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Shell mostró este aviso", "info", False)
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONSHOW)
        with patch.object(wn.time, "monotonic", return_value=101.01):
            self.backend._window_proc(self.backend.hwnd, wn.WM_TIMER, 2, 0)
        self.assertIsNone(notifier.status()["last_popup_shown_at"])
        self.assertIsNotNone(notifier.status()["last_shown_at"])

    def test_popup_close_or_timeout_never_cancels_protection_or_quits_main_loop(self):
        notifier = self.configure_popup()
        self.backend._show_popup("Prueba", "Cerrar solo oculta este aviso")
        hwnd = self.backend.popup_hwnd
        for message, wparam in ((wn.WM_COMMAND, 102), (wn.WM_TIMER, 1), (wn.WM_CLOSE, 0)):
            self.backend._window_proc(hwnd, message, wparam, 0)
        notifier.cancel_callback.assert_not_called()
        notifier._open_panel.assert_not_called()
        self.assertIsNotNone(notifier.status()["last_popup_hidden_at"])
        self.user.PostQuitMessage.assert_not_called()
        self.backend._window_proc(hwnd, wn.WM_COMMAND, 101, 0)
        notifier._open_panel.assert_called_once_with()
        notifier.cancel_callback.assert_not_called()

    def test_destroying_popup_dispatches_no_main_window_destroy(self):
        self.configure_popup()
        self.backend._show_popup("Prueba", "Aviso")
        main_hwnd = self.backend.hwnd
        popup_hwnd = self.backend.popup_hwnd
        self.user.DestroyWindow.side_effect = lambda hwnd: self.backend._window_proc(hwnd, wn.WM_DESTROY, 0, 0)
        self.backend._destroy_popup()
        self.assertIsNone(self.backend.popup_hwnd)
        self.assertEqual(self.backend.hwnd, main_hwnd)
        self.user.KillTimer.assert_called_with(popup_hwnd, 1)
        self.user.PostQuitMessage.assert_not_called()

    def test_destroy_removes_icon_with_original_window_handle(self):
        hwnd = self.backend.hwnd
        self.backend._window_proc(hwnd, wn.WM_DESTROY, 0, 0)
        operation, data = self.native_calls[-1]
        self.assertEqual(operation, wn.NIM_DELETE)
        self.assertEqual(data.hWnd, hwnd)
        self.assertIsNone(self.backend.hwnd)

    def test_persistent_popup_has_no_expiry_and_close_only_acknowledges_notice(self):
        notifier = self.configure_popup()
        notifier.sync_persistent({"policy_countdown": {"title": "Protección", "message": "Quedan 60 segundos"}})
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        before_shell = len(self.native_calls)
        with patch.dict(sys.modules, {"winsound": sound}):
            self.backend.deliver("Protección", "Quedan 60 segundos", "critical", True, True, "policy_countdown")
        self.assertEqual(len(self.native_calls), before_shell)  # No second balloon.
        self.user.SetTimer.assert_not_called()
        hwnd = self.backend.popup_hwnd
        self.backend._window_proc(hwnd, wn.WM_TIMER, 1, 0)
        self.assertEqual(self.backend.popup_persistent_key, "policy_countdown")
        self.backend._window_proc(hwnd, wn.WM_COMMAND, 101, 0)
        notifier._open_panel.assert_called_once_with()
        self.assertFalse(notifier._persistent_acknowledged("policy_countdown"))
        self.assertEqual(self.backend.popup_persistent_key, "policy_countdown")
        notifier.sync_persistent({"policy_countdown": {"title": "Protección", "message": "Quedan 58 segundos"}})
        self.backend.sync_persistent()
        self.user.SetWindowTextW.assert_any_call(self.backend.popup_body, "Quedan 58 segundos")
        sound.PlaySound.assert_called_once()
        self.backend._window_proc(hwnd, wn.WM_COMMAND, 102, 0)
        notifier.cancel_callback.assert_not_called()
        self.assertTrue(notifier._persistent_acknowledged("policy_countdown"))
        self.backend.deliver("Protección", "Quedan 30 segundos", "critical", False, True, "policy_countdown")
        self.assertIsNone(self.backend.popup_persistent_key)

    def test_resolved_or_stale_persistent_queue_never_displays_or_plays_sound(self):
        notifier = self.configure_popup()
        notifier.sync_persistent({"battery_low": {"title": "Baja", "message": "Baja"}})
        version = notifier._persistent_versions["battery_low"]
        notifier.sync_persistent({})
        sound = Mock()
        with patch.dict(sys.modules, {"winsound": sound}):
            result = self.backend.deliver("Baja", "Baja", "critical", True, True, "battery_low", version)
        self.assertFalse(result)
        self.assertIsNone(notifier.status()["last_popup_shown_at"])
        sound.PlaySound.assert_not_called()

    def test_persistent_recovery_hides_window_and_other_info_cannot_replace_it(self):
        notifier = self.configure_popup()
        notifier.sync_persistent({"ups_fault": {"title": "Falla", "message": "Falla activa"}})
        self.backend.deliver("Falla", "Falla activa", "critical", False, True, "ups_fault")
        shown = self.user.SetWindowPos.call_count
        self.backend._show_popup("Información", "Otro mensaje")
        self.assertEqual(self.user.SetWindowPos.call_count, shown)
        notifier.sync_persistent({})
        self.backend.sync_persistent()
        self.assertIsNone(self.backend.popup_persistent_key)
        self.assertIsNotNone(notifier.status()["last_popup_hidden_at"])
        self.assertIsNone(self.backend.last_notice)

    def test_persistent_warning_respects_windows_quiet_state(self):
        notifier = self.configure_popup()
        notifier.sync_persistent({"overload": {"title": "Carga", "message": "Sobrecarga"}})
        def quiet(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 6
            return 0
        self.shell.SHQueryUserNotificationState.side_effect = quiet
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        with patch.dict(sys.modules, {"winsound": sound}):
            self.backend.deliver("Carga", "Sobrecarga", "critical", True, True, "overload")
        # Persistence changes the visual lifetime only. It preserves the
        # existing explicit sound preference; Windows' state gates our popup.
        sound.PlaySound.assert_called_once()
        self.assertIsNone(self.backend.popup_persistent_key)
        self.assertIsNone(notifier.status()["last_popup_shown_at"])
        self.assertEqual(self.native_calls[-1][0], wn.NIM_MODIFY)

    def test_exit_menu_requires_explicit_yes_and_never_calls_cancel(self):
        notifier = self.configure_popup()
        notifier._request_exit = Mock()
        notifier.exit_callback = Mock()
        self.user.TrackPopupMenu.return_value = 3
        self.user.MessageBoxW.return_value = 7
        self.backend._menu()
        notifier._request_exit.assert_not_called()
        self.user.MessageBoxW.assert_called_once()
        self.assertEqual(self.user.MessageBoxW.call_args.args[-1], 0x134)
        self.assertIn("Windows seguirá encendido", self.user.MessageBoxW.call_args.args[1])
        self.user.MessageBoxW.return_value = 6
        self.backend._menu()
        notifier._request_exit.assert_called_once_with()
        notifier.cancel_callback.assert_not_called()
        notifier._stop.set()
        self.backend._menu()
        self.assertEqual(notifier._request_exit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
