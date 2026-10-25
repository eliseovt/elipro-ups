import ctypes
from itertools import count
from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor import windows_notifications as wn
from ups_monitor import notification_popup as popup_presentation
from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store


def latest_control_text(user, hwnd):
    """Read one control's last update without depending on sibling draw order."""
    return next(call.args[1] for call in reversed(user.SetWindowTextW.call_args_list)
                if call.args[0] == hwnd)


def latest_window_position(user, hwnd):
    return next(call.args[2:6] for call in reversed(user.SetWindowPos.call_args_list)
                if call.args[0] == hwnd)


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

    def test_theme_is_validated_stored_and_only_wakes_the_native_worker(self):
        notifier = self.make_notifier()
        self.assertEqual(notifier.notification_theme, "auto")
        self.assertEqual(notifier.status()["notification_theme"], "auto")
        notifier.set_theme("light")
        self.assertEqual(notifier.notification_theme, "light")
        self.assertIsNone(notifier._thread)
        backend = Mock()
        notifier._backend = backend
        notifier.set_theme("dark")
        self.assertEqual(notifier.status()["notification_theme"], "dark")
        backend.wake.assert_called_once_with()
        backend.deliver.assert_not_called()
        backend.initialize.assert_not_called()
        for value in (None, True, 1, "system", "Light", "", {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notifier.set_theme(value)
        self.assertEqual(notifier.notification_theme, "dark")
        notifier._backend = None

    def test_delivery_receipt_requires_native_true_and_duplicate_does_not_replay(self):
        notifier = self.make_notifier()
        notifier._ready = True
        backend = Mock()
        backend.deliver.return_value = True
        self.assertTrue(notifier.notify("Corte", "En batería", delivery_id="epoch:1"))
        self.assertEqual(notifier.drain_receipts(), [], "Queue admission is not an ACK")
        notifier._drain(backend)
        receipt = notifier.drain_receipts()
        self.assertEqual([(item["delivery_id"], item["outcome"]) for item in receipt], [("epoch:1", "accepted")])
        self.assertTrue(notifier.notify("Corte", "En batería", delivery_id="epoch:1"))
        notifier._drain(backend)
        backend.deliver.assert_called_once()
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "accepted")

    def test_none_rejection_and_exception_never_create_success_ack_and_allow_retry(self):
        notifier = self.make_notifier()
        notifier._ready = True
        backend = Mock()
        for result in (None, False, OSError("Shell rejected")):
            backend.deliver.side_effect = result if isinstance(result, Exception) else None
            backend.deliver.return_value = result
            self.assertTrue(notifier.notify("Corte", "Pendiente", delivery_id="epoch:1"))
            notifier._drain(backend)
            receipts = notifier.drain_receipts()
            self.assertEqual(len(receipts), 1)
            self.assertNotEqual(receipts[0]["outcome"], "accepted")
            self.assertEqual(notifier.status()["pending_deliveries"], 0)
        backend.deliver.side_effect = None
        backend.deliver.return_value = True
        notifier.notify("Corte", "Reintento", delivery_id="epoch:1")
        notifier._drain(backend)
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "accepted")

    def test_lock_cancels_queued_payload_and_fresh_resume_does_not_replay_old_text(self):
        notifier = self.make_notifier()
        notifier._ready = True
        backend = Mock()
        backend.deliver.return_value = True
        notifier.notify("Apagado", "Quedan 60 segundos", delivery_id="epoch:1")
        notifier.set_session_available(False)
        notifier._drain(backend)
        backend.deliver.assert_not_called()
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "cancelled")
        notifier.set_session_available(True)
        notifier.notify("Recuperación", "La red ya volvió", delivery_id="epoch:1")
        notifier._drain(backend)
        backend.deliver.assert_called_once_with("Recuperación", "La red ya volvió", "info", True)
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "accepted")

    def test_live_session_probe_closes_queue_and_ack_races(self):
        notifier = self.make_notifier()
        notifier._ready = True
        active = Mock(return_value=True)
        notifier.set_session_probe(active)
        notifier.notify("Corte", "Batería", delivery_id="epoch:1")
        active.return_value = False
        backend = Mock()
        backend.deliver.return_value = True
        notifier._drain(backend)
        backend.deliver.assert_not_called()
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "cancelled")
        active.return_value = True
        notifier.set_session_available(True)
        notifier.notify("Corte", "Nueva entrega", delivery_id="epoch:2")
        def lock_during_native(*_args):
            notifier.set_session_available(False)
            return True
        backend.deliver.side_effect = lock_during_native
        notifier._drain(backend)
        self.assertEqual([item["outcome"] for item in notifier.drain_receipts()], ["cancelled"])

    def test_cancelled_old_ticket_cannot_deliver_after_same_id_has_new_payload(self):
        notifier = self.make_notifier()
        notifier._ready = True
        backend = Mock()
        backend.deliver.return_value = True
        notifier.notify("Viejo", "Cuenta obsoleta", delivery_id="epoch:1")
        notifier.cancel_delivery("epoch:1")
        notifier.notify("Actual", "Ya recuperado", delivery_id="epoch:1")
        notifier._drain(backend)
        backend.deliver.assert_called_once_with("Actual", "Ya recuperado", "info", True)
        self.assertEqual([item["outcome"] for item in notifier.drain_receipts()], ["cancelled", "accepted"])
        notifier.cancel_delivery("epoch:1")
        notifier.notify("Actualizado", "Contexto posterior", delivery_id="epoch:1")
        notifier._drain(backend)
        self.assertEqual(backend.deliver.call_count, 2, "Explicit replacement also invalidates completed deduplication")

    def test_receipts_and_completed_dedup_are_bounded(self):
        notifier = self.make_notifier()
        notifier._ready = True
        backend = Mock()
        backend.deliver.return_value = True
        for index in range(300):
            notifier.notify("Aviso", "Contenido", delivery_id="epoch:" + str(index))
            notifier._drain(backend)
        self.assertEqual(len(notifier._completed_deliveries), 256)
        self.assertEqual(len(notifier.drain_receipts()), 256)
        self.assertEqual(notifier.status()["pending_deliveries"], 0)

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
        font_patch = patch.object(wn, "_load_font_apis", return_value=None)
        font_patch.start()
        self.addCleanup(font_patch.stop)
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
        self.shell.SHQueryUserNotificationState.side_effect = NativeShellTests.query_busy_notifications
        with patch.object(wn, "_load_apis", return_value=(self.user, self.shell, self.kernel)):
            self.backend = wn._Win32Backend(self.owner)
        self.backend.initialize()
        self.addCleanup(self.backend.close)

    @staticmethod
    def query_accepts_notifications(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 5
        return 0

    @staticmethod
    def query_busy_notifications(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 2
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

    def test_per_monitor_v2_is_requested_before_any_window_class_is_registered(self):
        calls = [item[0] for item in self.user.mock_calls]
        self.assertLess(calls.index("SetProcessDpiAwarenessContext"), calls.index("RegisterClassW"))
        self.assertEqual(self.user.SetProcessDpiAwarenessContext.call_args.args[0].value,
                         ctypes.c_void_p(-4).value)
        self.owner._set_dpi_status.assert_called_with({"mode": "per_monitor_v2", "error": None})

    def test_already_configured_and_older_windows_dpi_modes_are_nonfatal(self):
        user = Mock()
        user.SetProcessDpiAwarenessContext.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertEqual(wn.enable_per_monitor_dpi(user)["mode"], "already_configured")
        user.SetProcessDPIAware.assert_not_called()
        old = SimpleNamespace(SetProcessDPIAware=Mock(return_value=True))
        self.assertEqual(wn.enable_per_monitor_dpi(old)["mode"], "system_aware_fallback")

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
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
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
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        self.user.CreateWindowExW.side_effect = count(0xE0000000001)
        self.user.SetWindowPos.return_value = 1
        self.user.SetTimer.return_value = 1

        def workarea(action, param, output, flags):
            rectangle = ctypes.cast(output, ctypes.POINTER(wn.RECT)).contents
            rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = 0, 0, 1920, 1040
            return 1

        self.user.SystemParametersInfoW.side_effect = workarea
        return notifier

    def test_popup_scales_geometry_and_releases_every_font_after_dpi_changes(self):
        self.configure_popup()
        self.user.GetDpiForWindow.return_value = 192
        self.backend.gdi = Mock()
        self.backend.gdi.CreateSolidBrush.side_effect = count(20000)
        created_fonts = []
        handles = count(1001)

        def create_font(*_args):
            handle = next(handles)
            created_fonts.append(handle)
            return handle

        self.backend.gdi.CreateFontW.side_effect = create_font
        self.backend._show_popup("DPI", "Escala 200 %")
        create_popup = self.user.CreateWindowExW.call_args_list[1].args
        self.assertEqual(create_popup[6], 960)
        self.assertGreater(create_popup[7], 0)
        self.assertEqual(latest_window_position(self.user, self.backend.popup_hwnd)[2], 960)
        old_fonts = set(self.backend.popup_view.fonts.values())
        self.assertGreater(len(old_fonts), 1, "Title, body and countdown need a typographic hierarchy")
        old_heights = {call.args[0] for call in self.backend.gdi.CreateFontW.call_args_list}
        self.assertGreater(len(old_heights), 1)
        proposed = wn.RECT(20, 40, 740, 640)
        self.backend._window_proc(self.backend.popup_hwnd, wn.WM_DPICHANGED,
                                  144 | (144 << 16), ctypes.addressof(proposed))
        self.assertEqual(self.backend.popup_dpi, 144)
        position = latest_window_position(self.user, self.backend.popup_hwnd)
        self.assertEqual(position[2], 720)
        self.assertGreaterEqual(position[0], 0)
        self.assertGreaterEqual(position[1], 0)
        self.assertLessEqual(position[0] + position[2], 1920)
        self.assertLessEqual(position[1] + position[3], 1040)
        self.assertGreater(position[3], 0)
        new_fonts = set(self.backend.popup_view.fonts.values())
        self.assertTrue(old_fonts.isdisjoint(new_fonts))
        deleted = [call.args[0] for call in self.backend.gdi.DeleteObject.call_args_list]
        for font in old_fonts:
            self.assertEqual(deleted.count(font), 1)
        assigned = {call.args[2] for call in self.user.SendMessageW.call_args_list
                    if call.args[1] == wn.WM_SETFONT}
        self.assertTrue(new_fonts <= assigned)
        self.backend._destroy_popup()
        self.backend._destroy_popup()
        deleted = [call.args[0] for call in self.backend.gdi.DeleteObject.call_args_list]
        for font in created_fonts:
            self.assertEqual(deleted.count(font), 1)

    def test_theme_change_repaints_same_popup_without_acknowledgment_or_sound(self):
        notifier = self.configure_popup()
        notifier.acknowledge_callback = Mock()
        self.backend.gdi = Mock()
        self.backend.gdi.CreateFontW.side_effect = count(10000)
        self.backend.gdi.CreateSolidBrush.side_effect = count(20000)
        notifier.set_theme("light")
        self.backend._show_popup("Prueba", "Diseño de Elipro")
        popup, view = self.backend.popup_hwnd, self.backend.popup_view
        created = self.user.CreateWindowExW.call_count
        notifier._backend = self.backend
        self.user.InvalidateRect.reset_mock()
        self.user.RedrawWindow.reset_mock()
        with patch.object(self.backend, "_play_sound") as sound:
            notifier.set_theme("dark")
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_NOTIFY, 0, 0)
            self.assertTrue(self.user.InvalidateRect.called or self.user.RedrawWindow.called)
            self.assertEqual(self.backend.popup_hwnd, popup)
            self.assertIs(self.backend.popup_view, view)
            self.assertEqual(self.user.CreateWindowExW.call_count, created)
            notifier.acknowledge_callback.assert_not_called()
            notifier.cancel_callback.assert_not_called()
            notifier._open_panel.assert_not_called()
            sound.assert_not_called()
        notifier._backend = None

    def test_windows_appearance_messages_repaint_without_recreating_popup(self):
        notifier = self.configure_popup()
        self.backend.gdi = Mock()
        self.backend.gdi.CreateFontW.side_effect = count(10000)
        self.backend.gdi.CreateSolidBrush.side_effect = count(20000)
        with patch.object(popup_presentation, "resolve_theme", return_value="light"):
            self.backend._show_popup("Prueba", "Sigue el aspecto de Windows")
        popup = self.backend.popup_hwnd
        created = self.user.CreateWindowExW.call_count
        with patch.object(self.backend, "_play_sound") as sound, \
                patch.object(popup_presentation, "resolve_theme", side_effect=("dark", "light")):
            for message in (wn.WM_SETTINGCHANGE, wn.WM_THEMECHANGED):
                with self.subTest(message=message):
                    self.user.InvalidateRect.reset_mock()
                    self.user.RedrawWindow.reset_mock()
                    self.backend._window_proc(self.backend.hwnd, message, 0, 0)
                    self.assertTrue(self.user.InvalidateRect.called or self.user.RedrawWindow.called)
                    self.assertEqual(self.backend.popup_hwnd, popup)
                    self.assertEqual(self.user.CreateWindowExW.call_count, created)
            sound.assert_not_called()
        self.assertEqual(notifier.notification_theme, "auto")

    def test_buttons_keep_native_commands_and_keyboard_tab_stops(self):
        self.configure_popup()
        self.backend._show_popup("Prueba", "Acciones accesibles")
        buttons = [call.args for call in self.user.CreateWindowExW.call_args_list
                   if call.args[1] == "BUTTON"]
        self.assertEqual({args[9].value for args in buttons}, {101, 102, 103})
        for args in buttons:
            self.assertTrue(args[3] & 0x00010000, "Native buttons must remain keyboard-reachable")
            self.assertEqual(args[3] & 0xF, 0xB, "Branded actions use BS_OWNERDRAW")

    def test_empty_wakeup_synchronizes_native_notice_only_once(self):
        notifier = self.configure_popup()
        with patch.object(self.backend, "sync_persistent") as synchronize:
            notifier._drain(self.backend)
            synchronize.assert_called_once_with()

    def test_wakeup_with_delivery_preserves_final_synchronization(self):
        notifier = self.configure_popup()
        notifier._queue.put(("Prueba", "Texto", "info", False))
        with patch.object(self.backend, "sync_persistent") as synchronize, \
                patch.object(self.backend, "deliver", return_value=True) as deliver:
            notifier._drain(self.backend)
            self.assertEqual(synchronize.call_count, 2)
            deliver.assert_called_once_with("Prueba", "Texto", "info", False)

    def test_same_visible_popup_is_not_repositioned_but_hidden_popup_can_show_again(self):
        self.configure_popup()
        self.backend._show_popup("Prueba", "Aviso estable")
        popup = self.backend.popup_hwnd
        self.user.SetWindowPos.reset_mock()
        self.backend._position_popup()
        self.backend._position_popup(show=True)
        self.user.SetWindowPos.assert_not_called()
        self.backend._hide_popup()
        self.backend._show_popup("Prueba", "Aviso estable")
        shown = [call for call in self.user.SetWindowPos.call_args_list
                 if call.args[0] == popup and call.args[-1] & 0x0040]
        self.assertEqual(len(shown), 1)
        self.assertTrue(self.backend._popup_visible)

    def test_workarea_change_reflows_and_moves_popup_once(self):
        self.configure_popup()
        self.backend._show_popup("Prueba", "Aviso estable")
        self.user.SetWindowPos.reset_mock()
        work = wn.RECT(0, 0, 1280, 900)
        self.assertTrue(self.backend._layout_popup(96, work))
        self.assertTrue(self.backend._position_popup(work))
        self.assertFalse(self.backend._layout_popup(96, work))
        self.assertFalse(self.backend._position_popup(work))
        positions = [call for call in self.user.SetWindowPos.call_args_list if call.args[0] == self.backend.popup_hwnd]
        self.assertEqual(len(positions), 1)
        left, top, width, height = positions[0].args[2:6]
        self.assertLessEqual(left + width, work.right)
        self.assertLessEqual(top + height, work.bottom)

    def test_static_text_uses_its_surface_background_in_both_themes(self):
        self.configure_popup()
        self.backend.gdi = Mock()
        self.backend.gdi.CreateFontW.side_effect = count(10000)
        self.backend.gdi.CreateSolidBrush.side_effect = count(20000)
        self.backend._show_popup("Prueba", "Sin bloques grises")
        view = self.backend.popup_view
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                view.apply_theme(theme)
                brush = view.handle_message(popup_presentation.WM_CTLCOLORSTATIC, 99, self.backend.popup_body)
                self.assertEqual(brush, view.brushes[view.palette["background"]])
                self.backend.gdi.SetBkColor.assert_called_with(99, view.palette["background"])
                self.backend.gdi.SetTextColor.assert_called_with(99, view.palette["muted"])
                brush = view.handle_message(popup_presentation.WM_CTLCOLORSTATIC, 99, self.backend.popup_countdown)
                self.assertEqual(brush, view.brushes[view.palette["warning_background"]])
                self.backend.gdi.SetBkColor.assert_called_with(99, view.palette["warning_background"])

    def test_high_contrast_uses_windows_surfaces_and_highlight_colors(self):
        self.configure_popup()
        self.backend.gdi = Mock()
        self.backend.gdi.CreateFontW.side_effect = count(10000)
        self.backend.gdi.CreateSolidBrush.side_effect = count(20000)
        self.backend._show_popup("Prueba", "Contraste del sistema")
        view = self.backend.popup_view
        self.user.GetSysColor.side_effect = lambda index: 0x100000 + index
        with patch.object(view, "_high_contrast", return_value=True):
            view.apply_theme("dark")
        self.assertEqual(view.palette["background"], 0x100005)
        self.assertEqual(view.palette["foreground"], 0x100008)
        self.assertEqual(view.palette["primary"], 0x10000D)
        self.assertEqual(view.palette["primary_text"], 0x10000E)
        self.assertEqual(view.palette["disabled_text"], 0x100011)
        brush = view.handle_message(popup_presentation.WM_CTLCOLORSTATIC, 99, self.backend.popup_body)
        self.assertEqual(brush, view.brushes[0x100005])

    def test_cancelled_ticket_during_popup_creation_is_not_shown_or_acknowledged(self):
        notifier = self.configure_popup()
        notifier._ready = True
        notifier.sync_persistent({"policy_countdown": {"title": "Cuenta", "message": "Quedan 30 segundos"}})
        notifier.notify("Cuenta", "Quedan 30 segundos", persistent=True,
                        key="policy_countdown", delivery_id="epoch:1")
        handles = count(500)
        def create(*_args):
            handle = next(handles)
            if handle == 504:
                notifier.cancel_delivery("epoch:1")
            return handle
        self.user.CreateWindowExW.side_effect = create
        notifier._drain(self.backend)
        self.assertFalse(any(call.args[0] == self.backend.popup_hwnd and call.args[-1] & 0x40
                             for call in self.user.SetWindowPos.call_args_list))
        self.assertIsNone(notifier.status()["last_popup_shown_at"])
        self.assertEqual([item["outcome"] for item in notifier.drain_receipts()], ["cancelled"])

    def test_real_shell_acceptance_produces_receipt_without_claiming_user_saw_it(self):
        notifier = self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        notifier._ready = True
        notifier.notify("Prueba", "Solicitud nativa", sound=False, delivery_id="epoch:1")
        notifier._drain(self.backend)
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "accepted")
        self.assertIsNone(notifier.status()["last_shown_at"])

    def test_test_notice_uses_selected_theme_immediately_without_second_visual_or_sound(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                notifier = self.configure_popup()
                notifier._ready = True
                store = Store(":memory:")
                self.addCleanup(store.close)
                alerts = AlertManager(store, notifier)
                alerts.update_settings({"theme": theme, "sound_enabled": True})
                shell_before = len(self.native_calls)
                self.user.SetTimer.reset_mock()
                sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
                with patch.dict(sys.modules, {"winsound": sound}):
                    alerts.test()
                    notifier._drain(self.backend)
                    popup = self.backend.popup_hwnd
                    self.assertIsNotNone(popup)
                    self.assertEqual(self.backend.popup_view.theme, theme)
                    self.assertIn("Prueba", latest_control_text(self.user, self.backend.popup_title))
                    self.assertIn("No se apagará ni hibernará", latest_control_text(self.user, self.backend.popup_body))
                    self.assertIsNotNone(notifier.status()["last_popup_shown_at"])
                    self.assertIsNone(notifier.status()["last_shown_at"])
                    self.assertTrue(self.backend.last_notice["popup_handled"])
                    self.user.SetTimer.assert_called_once_with(popup, 1, 12000, None)
                    self.assertEqual(len(self.native_calls), shell_before, "Own popup must not add a Shell balloon")
                    created = self.user.CreateWindowExW.call_count
                    positioned = self.user.SetWindowPos.call_count
                    self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONTIMEOUT)
                    self.backend._window_proc(self.backend.hwnd, wn.WM_TIMER, 2, 0)
                    self.backend._maybe_fallback_popup()
                    self.assertEqual(self.user.CreateWindowExW.call_count, created)
                    self.assertEqual(self.user.SetWindowPos.call_count, positioned)
                    sound.PlaySound.assert_called_once()
                self.backend._window_proc(popup, wn.WM_TIMER, 1, 0)
                self.assertIsNotNone(notifier.status()["last_popup_hidden_at"])
                notifier.cancel_callback.assert_not_called()

    def test_restricted_desktop_uses_only_shell_visual_and_preserves_explicit_sound(self):
        notifier = self.configure_popup()
        for state in (2, 3, 4, 6):
            with self.subTest(state=state):
                def query(pointer):
                    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = state
                    return 0

                self.shell.SHQueryUserNotificationState.side_effect = query
                before = len(self.native_calls)
                sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
                with patch.dict(sys.modules, {"winsound": sound}):
                    self.assertTrue(self.backend.deliver("Prueba", "Respeta el estado de Windows", "info", True))
                    self.backend._maybe_fallback_popup()
                    self.backend._check_unconfirmed_popup()
                    sound.PlaySound.assert_called_once()
                self.assertEqual(len(self.native_calls), before + 1)
                self.assertEqual(self.native_calls[-1][0], wn.NIM_MODIFY)
                self.assertIsNone(self.backend.popup_hwnd)
        self.assertIsNone(notifier.status()["last_popup_shown_at"])

    def test_accepted_delivery_keeps_valid_fallback_until_explicitly_cancelled(self):
        notifier = self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        notifier._ready = True
        with patch.object(wn.time, "monotonic", return_value=100):
            notifier.notify("Corte", "En batería", sound=False, delivery_id="epoch:1")
            notifier._drain(self.backend)
        self.assertEqual(notifier.drain_receipts()[0]["outcome"], "accepted")
        self.assertEqual(notifier.status()["pending_deliveries"], 0)
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        with patch.object(wn.time, "monotonic", return_value=101.01):
            self.backend._check_unconfirmed_popup()
        self.assertIsNotNone(notifier.status()["last_popup_shown_at"],
                             "Removing an accepted pending entry must not suppress a valid fallback")
        notifier.cancel_delivery("epoch:1")
        self.backend.sync_persistent()
        self.assertIsNone(self.backend.last_notice)
        self.assertIsNotNone(notifier.status()["last_popup_hidden_at"])

    def test_cancelled_accepted_delivery_cannot_reappear_from_timer_or_shell_timeout(self):
        notifier = self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        notifier._ready = True
        with patch.object(wn.time, "monotonic", return_value=100):
            notifier.notify("Corte", "Texto anterior", sound=False, delivery_id="epoch:1")
            notifier._drain(self.backend)
        notifier.cancel_delivery("epoch:1")
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        self.backend._query_user_notification_state()
        with patch.object(wn.time, "monotonic", return_value=100.05):
            self.backend._maybe_fallback_popup()
        with patch.object(wn.time, "monotonic", return_value=101.01):
            self.backend._check_unconfirmed_popup()
        self.user.SetWindowPos.assert_not_called()
        self.assertIsNone(notifier.status()["last_popup_shown_at"])
        self.assertEqual([item["outcome"] for item in notifier.drain_receipts()], ["cancelled"])

    def test_fast_shell_timeout_uses_one_nonactivating_popup_and_timer(self):
        notifier = self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Aviso de escritorio", "warning", False)
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        self.backend._query_user_notification_state()
        with patch.object(wn.time, "monotonic", return_value=100.04):
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONTIMEOUT)
        self.assertIsNotNone(notifier.status()["last_popup_shown_at"])
        self.user.SetTimer.assert_any_call(self.backend.popup_hwnd, 1, 12000, None)
        create_args = self.user.CreateWindowExW.call_args_list[1].args
        self.assertTrue(create_args[0] & 0x08000000)  # WS_EX_NOACTIVATE
        self.assertTrue(create_args[0] & 0x00000080)  # WS_EX_TOOLWINDOW
        self.assertTrue(create_args[0] & 0x00000008)  # WS_EX_TOPMOST
        self.assertEqual(create_args[6], 480)
        self.assertGreater(create_args[7], 0)
        self.assertTrue(self.user.SetWindowPos.call_args.args[-1] & 0x10)  # SWP_NOACTIVATE
        with patch.object(wn.time, "monotonic", return_value=100.05):
            self.backend._maybe_fallback_popup()
        self.assertEqual(self.user.SetTimer.call_count, 1)  # Popup expiry; no fallback timer beside Shell.

    def test_normal_timeout_quiet_mode_click_or_stop_never_shows_fallback(self):
        self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
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
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Shell no emitió callback", "info", False)
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
        with patch.object(wn.time, "monotonic", return_value=101.01):
            self.backend._window_proc(self.backend.hwnd, wn.WM_TIMER, 2, 0)
        self.assertIsNotNone(notifier.status()["last_popup_shown_at"])
        self.assertIsNone(notifier.status()["last_shown_at"])

    def test_confirmed_shell_display_has_no_second_popup(self):
        notifier = self.configure_popup()
        self.shell.SHQueryUserNotificationState.side_effect = self.query_busy_notifications
        with patch.object(wn.time, "monotonic", return_value=100):
            self.backend.deliver("Prueba", "Shell mostró este aviso", "info", False)
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_TRAY, 0, wn.NIN_BALLOONSHOW)
        self.shell.SHQueryUserNotificationState.side_effect = self.query_accepts_notifications
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
        self.user.KillTimer.assert_any_call(popup_hwnd, 1)
        self.user.KillTimer.assert_any_call(popup_hwnd, 3)
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

    def test_outage_recovery_closes_native_notice_without_ack_and_recovery_popup_expires(self):
        notifier = self.configure_popup()
        notifier._set_ready(True)
        store = Store(":memory:")
        self.addCleanup(store.close)
        alerts = AlertManager(store, notifier)
        alerts.update_settings({"sound_enabled": False})
        with patch.object(store, "acknowledge_notifications", wraps=store.acknowledge_notifications) as acknowledge:
            alerts.on_event("power_lost", "El UPS usa batería")
            notifier._drain(self.backend)
            outage = alerts.status()["pending"][0]
            self.assertEqual(self.backend.popup_persistent_key, f"notice:{outage['id']}")
            popup = self.backend.popup_hwnd

            alerts.on_event("power_restored", "Volvió la red")
            notifier._drain(self.backend)
            self.assertIsNone(self.backend.popup_persistent_key)
            self.user.ShowWindow.assert_any_call(popup, 0)
            pending = alerts.status()["pending"]
            self.assertEqual({item["kind"] for item in pending}, {"power_lost", "power_restored"})
            self.assertIsNotNone(next(item for item in pending if item["kind"] == "power_lost")["resolved_at"])
            self.assertTrue(all(item["acknowledged_at"] is None for item in pending))
            self.assertTrue(all(not item["requires_attention"] for item in pending))

            # Recovery uses the same branded surface immediately, with a bounded lifetime.
            self.assertEqual(latest_control_text(self.user, self.backend.popup_title), "Suministro eléctrico recuperado")
            self.user.SetTimer.assert_any_call(popup, 1, 12000, None)
            self.assertIsNone(self.backend.popup_persistent_key)
            self.backend._window_proc(popup, wn.WM_TIMER, 1, 0)
            acknowledge.assert_not_called()
            notifier.cancel_callback.assert_not_called()
            self.assertEqual(len(store.pending_notifications()), 2)

    def test_recovery_before_native_queue_drains_discards_obsolete_outage_window(self):
        notifier = self.configure_popup()
        notifier._set_ready(True)
        store = Store(":memory:")
        self.addCleanup(store.close)
        alerts = AlertManager(store, notifier)
        alerts.update_settings({"sound_enabled": False})
        alerts.on_event("power_lost", "El UPS usa batería")
        alerts.on_event("power_restored", "Volvió la red")
        notifier._drain(self.backend)
        self.assertIsNotNone(self.backend.popup_hwnd)
        self.assertEqual(latest_control_text(self.user, self.backend.popup_title), "Suministro eléctrico recuperado")
        self.assertEqual(self.backend.last_notice["title"], "Suministro eléctrico recuperado")
        self.assertEqual(notifier._persistent_messages, {})
        self.assertEqual(len(store.pending_notifications()), 2)
        notifier.cancel_callback.assert_not_called()

    def test_durable_notice_close_does_not_ack_and_understood_waits_for_disk(self):
        notifier = self.configure_popup()
        entered, release = threading.Event(), threading.Event()
        def acknowledge(ids):
            self.assertEqual(ids, [123])
            entered.set()
            release.wait(1)
        notifier.acknowledge_callback = acknowledge
        notifier.sync_persistent({"notice:123": {"title": "Corte", "message": "Red recuperada; aviso pendiente"}})
        self.backend.sync_persistent()
        hwnd = self.backend.popup_hwnd
        self.backend._window_proc(hwnd, wn.WM_CLOSE, 0, 0)
        self.assertFalse(entered.is_set())
        self.assertEqual(self.backend.popup_persistent_key, "notice:123")
        self.backend._window_proc(hwnd, wn.WM_COMMAND, 102, 0)
        self.assertTrue(entered.wait(1))
        self.assertFalse(notifier._persistent_acknowledged("notice:123"))
        self.assertEqual(self.backend.popup_persistent_key, "notice:123")
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not notifier._persistent_acknowledged("notice:123"):
            time.sleep(.005)
        self.assertTrue(notifier._persistent_acknowledged("notice:123"))
        self.backend.sync_persistent()
        self.assertIsNone(self.backend.popup_persistent_key)

    def test_failed_durable_ack_keeps_popup_and_next_notice_waits_its_turn(self):
        notifier = self.configure_popup()
        notifier.acknowledge_callback = Mock(side_effect=OSError("No se pudo guardar"))
        notifier.sync_persistent({"notice:1": {"title": "Corte", "message": "Corte registrado"},
                                  "notice:2": {"title": "Red", "message": "Red recuperada"}})
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:1")
        self.assertFalse(self.backend._show_popup("Red", "Red recuperada", persistent=True, key="notice:2"))
        self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 102, 0)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and "No se guardó" not in (notifier.status()["last_error"] or ""):
            time.sleep(.005)
        self.assertIn("No se guardó", notifier.status()["last_error"])
        self.assertFalse(notifier._persistent_acknowledged("notice:1"))
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:1")

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
