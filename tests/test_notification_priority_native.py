"""Priority, live countdown and cancellation with fake Windows APIs only."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ups_monitor import windows_notifications as wn
from ups_monitor.session_agent import Agent, LocalClient
from tests import test_windows_notifications as fixtures


class NativePriorityTests(unittest.TestCase):
    setUp = fixtures.NativeShellTests.setUp
    configure_popup = fixtures.NativeShellTests.configure_popup
    query_accepts_notifications = staticmethod(fixtures.NativeShellTests.query_accepts_notifications)

    def notice(self, title="Corte", priority=1, *, sound=False, countdown=None):
        result = {"title": title, "message": "Revisa el estado del UPS.", "priority": priority,
                  "severity": "critical" if priority >= 2 else "warning", "sound": sound,
                  "confirmed": True, "confirmed_for_ms": 3000, "repeat_seconds": 30}
        if countdown is not None:
            result["countdown"] = {"countdown_id": countdown, "action": "hibernate",
                                   "remaining_seconds": 30, "valid_for_ms": 3000, "cancelable": True}
        return result

    def test_more_urgent_notice_preempts_without_ack_and_equal_priority_is_stable(self):
        notifier = self.configure_popup()
        notifier.acknowledge_callback = Mock()
        warning = self.notice()
        critical = self.notice("Batería baja", 2)
        urgent = self.notice("Protección", 3, countdown="a" * 32)
        notifier.sync_persistent({"notice:1": warning})
        self.backend.sync_persistent()
        popup = self.backend.popup_hwnd
        notifier.sync_persistent({"notice:1": warning, "notice:2": critical})
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:2")
        notifier.sync_persistent({"notice:3": self.notice("Falla", 2), "notice:2": critical, "notice:1": warning})
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:2")
        notifier.sync_persistent({"notice:1": warning, "notice:2": critical, "notice:4": urgent})
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:4")
        self.assertEqual(self.backend.popup_hwnd, popup)
        self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Hibernación programada")
        self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:30")
        self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "URGENTE")
        notifier.acknowledge_callback.assert_not_called()
        self.assertEqual(set(notifier._persistent_messages), {"notice:1", "notice:2", "notice:4"})

    def test_restoring_previous_notice_is_silent_and_updates_do_not_sound(self):
        notifier = self.configure_popup()
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        warning = self.notice(sound=True)
        critical = self.notice("Baja", 2, sound=True)
        with patch.dict("sys.modules", {"winsound": sound}):
            notifier.sync_persistent({"notice:1": warning})
            self.backend.sync_persistent()
            notifier.sync_persistent({"notice:1": warning, "notice:2": critical})
            self.backend.sync_persistent()
            self.backend.sync_persistent()
            self.assertEqual(sound.PlaySound.call_count, 2)
            notifier.sync_persistent({"notice:1": warning})
            self.backend.sync_persistent()
            self.assertEqual(self.backend.popup_persistent_key, "notice:1")
            self.assertEqual(sound.PlaySound.call_count, 2)

    def test_informational_delivery_does_not_sound_over_active_priority_notice(self):
        notifier = self.configure_popup()
        notifier.sync_persistent({"notice:1": self.notice("Falla", 2)})
        self.backend.sync_persistent()
        sound = Mock()
        with patch.dict("sys.modules", {"winsound": sound}):
            self.assertFalse(self.backend.deliver("Información", "Otro mensaje", "info", True))
        sound.PlaySound.assert_not_called()
        self.assertEqual(self.backend.popup_persistent_key, "notice:1")

    def test_sound_repetitions_require_visible_confirmed_notice_and_interval(self):
        notifier = self.configure_popup()
        sound = Mock(SND_ALIAS=0x10000, SND_ASYNC=1, SND_NODEFAULT=2)
        notice = self.notice("Baja", 2, sound=True)
        with patch.dict("sys.modules", {"winsound": sound}), patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            self.backend.deliver("Baja", "Baja", "critical", True, True, "notice:1")
            self.assertEqual(sound.PlaySound.call_count, 1)
            clock.return_value = 129
            notifier.sync_persistent({"notice:1": notice})
            self.backend.deliver("Baja", "Baja", "critical", True, True, "notice:1")
            self.assertEqual(sound.PlaySound.call_count, 1)
            clock.return_value = 130
            notifier.sync_persistent({"notice:1": notice})
            self.backend.deliver("Baja", "Baja", "critical", True, True, "notice:1")
            self.assertEqual(sound.PlaySound.call_count, 2)
            clock.return_value = 164
            self.backend.deliver("Baja", "Baja", "critical", True, True, "notice:1")
            self.assertEqual(sound.PlaySound.call_count, 2, "stale confirmation cannot repeat its sound")

    def test_priority_is_selected_before_fifo_and_deferred_ticket_is_not_a_receipt(self):
        notifier = self.configure_popup()
        notifier._set_ready(True)
        notifier.sync_persistent({"notice:1": self.notice()})
        notifier.notify("Corte", "Batería", "warning", False, persistent=True, key="notice:1", delivery_id="epoch:1")
        notifier.sync_persistent({"notice:1": self.notice(), "notice:2": self.notice("Protección", 3, countdown="a" * 32)})
        notifier.notify("Protección", "Cuenta", "critical", False, persistent=True, key="notice:2", delivery_id="epoch:2")
        notifier._drain(self.backend)
        self.assertEqual(self.backend.popup_persistent_key, "notice:2")
        receipts = {item["delivery_id"]: item["outcome"] for item in notifier.drain_receipts()}
        self.assertNotEqual(receipts["epoch:1"], "accepted")
        self.assertEqual(receipts["epoch:2"], "accepted")

    def test_live_countdown_does_not_extend_and_expired_metadata_disables_cancel(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        with patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            clock.return_value = 101
            # Even a rounded replacement cannot add time to this episode.
            notice["countdown"]["valid_for_ms"] = 2000
            notifier.sync_persistent({"notice:1": notice})
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Hibernación programada")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:29")
            self.user.EnableWindow.assert_called_with(self.backend.popup_cancel, True)
            clock.return_value = 103.01
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Hibernación programada")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:27")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "ACTUALIZANDO")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_view.controls["countdown_label"]), "Tiempo estimado")
            pending = notifier._persistent_message("notice:1")
            self.assertTrue(pending["countdown"]["refresh_pending"])
            self.assertFalse(pending["confirmed"])
            self.assertEqual(pending["countdown"]["valid_for_ms"], 0)
            self.assertFalse(pending["countdown"]["cancelable"])
            self.user.EnableWindow.assert_called_with(self.backend.popup_cancel, False)
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 103, 0)
            notifier.cancel_countdown_callback.assert_not_called()
            clock.return_value = 108.01
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Actualización interrumpida")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "—:—")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "SIN DATOS RECIENTES")

    def test_brief_refresh_gap_recovers_same_episode_without_extending_or_reflowing(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        with patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            popup = self.backend.popup_hwnd
            initial_body = fixtures.latest_control_text(self.user, self.backend.popup_body)
            initial_footer = fixtures.latest_control_text(self.user, self.backend.popup_view.controls["footer"])
            self.assertEqual(initial_body, "Guarda tu trabajo antes de que termine la cuenta.")
            self.assertEqual(initial_footer, "Entendido no cancela la protección.")
            self.user.SetWindowPos.reset_mock()
            created = self.user.CreateWindowExW.call_count
            with patch.object(self.backend.popup_view, "layout", wraps=self.backend.popup_view.layout) as layout:
                clock.return_value = 103.1
                self.backend._window_proc(popup, wn.WM_TIMER, 3, 0)
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Hibernación programada")
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:27")
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "ACTUALIZANDO")
                self.assertFalse(self.backend.popup_view._enabled["cancel"])
                self.backend._window_proc(popup, wn.WM_COMMAND, 103, 0)
                notifier.cancel_countdown_callback.assert_not_called()
                clock.return_value = 103.5
                # A newly confirmed but rounded payload cannot add time to the episode.
                notifier.sync_persistent({"notice:1": self.notice("Protección", 3, countdown="a" * 32)})
                self.backend.sync_persistent()
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:27")
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "URGENTE")
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_view.controls["countdown_label"]), "Tiempo restante")
                self.assertTrue(self.backend.popup_view._enabled["cancel"])
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_body), initial_body)
                self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_view.controls["footer"]), initial_footer)
                layout.assert_not_called()
            self.assertEqual(self.backend.popup_hwnd, popup)
            self.assertEqual(self.backend.popup_persistent_key, "notice:1")
            self.assertEqual(self.user.CreateWindowExW.call_count, created)
            self.user.SetWindowPos.assert_not_called()

    def test_visual_ticks_update_clock_without_relayout_reposition_or_button_churn(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        with patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            popup, view = self.backend.popup_hwnd, self.backend.popup_view
            self.user.SetTimer.assert_any_call(popup, 3, 100, None)
            self.user.SetWindowPos.reset_mock()
            self.user.SetWindowTextW.reset_mock()
            self.user.EnableWindow.reset_mock()
            self.user.ShowWindow.reset_mock()
            self.user.SetTimer.reset_mock()
            self.user.KillTimer.reset_mock()
            with patch.object(view, "layout", wraps=view.layout) as layout, \
                    patch.object(self.backend, "_play_sound") as sound:
                for instant in (100.1, 100.2, 100.9, 101.01, 101.1, 101.9, 102.01, 102.9):
                    clock.return_value = instant
                    self.backend._window_proc(popup, wn.WM_TIMER, 3, 0)
                layout.assert_not_called()
                sound.assert_not_called()
            self.user.SetWindowPos.assert_not_called()
            self.user.EnableWindow.assert_not_called()
            self.user.ShowWindow.assert_not_called()
            self.user.SetTimer.assert_not_called()
            self.user.KillTimer.assert_not_called()
            updates = [(call.args[0], call.args[1]) for call in self.user.SetWindowTextW.call_args_list]
            self.assertEqual(updates, [(self.backend.popup_countdown, "00:29"),
                                       (self.backend.popup_countdown, "00:28")])
            self.assertEqual(self.backend.popup_persistent_key, "notice:1")
            self.assertEqual(self.backend._popup_refresh_interval, 100)

    def test_refresh_interval_changes_only_when_visible_notice_switches_countdown_mode(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        with patch.object(wn.time, "monotonic", return_value=100):
            notifier.sync_persistent({"notice:1": self.notice("Corte", 1)})
            self.backend.sync_persistent()
            popup = self.backend.popup_hwnd
            self.user.SetTimer.assert_any_call(popup, 3, 1000, None)
            self.user.SetTimer.reset_mock()
            notifier.sync_persistent({"notice:1": self.notice("Cuenta", 3, countdown="a" * 32)})
            self.backend.sync_persistent()
            self.user.SetTimer.assert_called_once_with(popup, 3, 100, None)
            self.user.SetTimer.reset_mock()
            self.backend.sync_persistent()
            self.user.SetTimer.assert_not_called()
            notifier.sync_persistent({"notice:1": self.notice("Revisión", 2)})
            self.backend.sync_persistent()
            self.user.SetTimer.assert_called_once_with(popup, 3, 1000, None)
            self.user.SetTimer.reset_mock()
            self.backend.sync_persistent()
            self.user.SetTimer.assert_not_called()

    def test_high_frequency_ticks_preserve_expiry_and_never_enable_stale_cancellation(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        notice["countdown"]["valid_for_ms"] = 250
        with patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            self.user.EnableWindow.reset_mock()
            clock.return_value = 100.26
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:30")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "ACTUALIZANDO")
            self.user.EnableWindow.assert_called_once_with(self.backend.popup_cancel, False)
            clock.return_value = 100.4
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.user.EnableWindow.assert_called_once_with(self.backend.popup_cancel, False)
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 103, 0)
            notifier.cancel_countdown_callback.assert_not_called()

    def test_zero_never_claims_windows_action_completed(self):
        notifier = self.configure_popup()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        notice["countdown"]["remaining_seconds"] = 1
        with patch.object(wn.time, "monotonic", return_value=100) as clock:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            clock.return_value = 101.1
            self.backend.sync_persistent()
            self.assertIsNotNone(self.backend.popup_persistent_key)
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Esperando confirmación")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:00")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_badge), "CONFIRMANDO")
            body = fixtures.latest_control_text(self.user, self.backend.popup_body)
            self.assertRegex(body.lower(), "esper|confirm")
            self.assertNotIn("completada", body)

    def test_failed_live_timer_does_not_leave_an_actionable_stale_window(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock()
        notifier.sync_persistent({"notice:1": self.notice("Protección", 3, countdown="a" * 32)})
        self.user.SetTimer.return_value = 0
        with self.assertRaises(OSError):
            self.backend.sync_persistent()
        self.assertIsNone(self.backend.popup_persistent_key)
        notifier.cancel_countdown_callback.assert_not_called()

    def test_cancel_worker_uses_captured_episode_and_ignores_late_reply_for_next_episode(self):
        notifier = self.configure_popup()
        entered, release = threading.Event(), threading.Event()
        observed = []
        def callback(countdown_id):
            observed.append((countdown_id, threading.current_thread()))
            entered.set()
            release.wait(1)
            return {"cancelled_countdown_id": countdown_id, "protection": {"state": "cancelled"}}
        notifier.cancel_countdown_callback = callback
        notifier.sync_persistent({"notice:1": self.notice("Protección", 3, countdown="a" * 32)})
        self.backend.sync_persistent()
        caller = threading.current_thread()
        self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 103, 0)
        self.assertTrue(entered.wait(1))
        self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 103, 0)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0][1], caller)
        notifier.sync_persistent({"notice:2": self.notice("Otra cuenta", 3, countdown="b" * 32)})
        self.backend.sync_persistent()
        release.set()
        deadline = time.monotonic() + 1
        while notifier._countdown_cancel_inflight and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(observed[0][0], "a" * 32)
        self.assertEqual(self.backend.popup_persistent_key, "notice:2")
        self.assertEqual(notifier._countdown_cancel_feedback, {})

    def test_cancel_failure_is_visible_without_acknowledging_or_dismissing(self):
        notifier = self.configure_popup()
        notifier.cancel_countdown_callback = Mock(side_effect=OSError("timeout"))
        notifier.acknowledge_callback = Mock()
        notifier.sync_persistent({"notice:1": self.notice("Protección", 3, countdown="a" * 32)})
        self.backend.sync_persistent()
        self.backend._window_proc(self.backend.popup_hwnd, wn.WM_COMMAND, 103, 0)
        deadline = time.monotonic() + 1
        while notifier._countdown_cancel_inflight and time.monotonic() < deadline:
            time.sleep(.005)
        self.backend.sync_persistent()
        self.assertIn("No se confirmó la cancelación", fixtures.latest_control_text(self.user, self.backend.popup_body))
        self.assertEqual(self.backend.popup_persistent_key, "notice:1")
        notifier.acknowledge_callback.assert_not_called()

    def test_delayed_ack_does_not_dismiss_rearmed_generation(self):
        notifier = self.configure_popup()
        entered, release = threading.Event(), threading.Event()
        def acknowledge(_ids):
            entered.set()
            release.wait(1)
        notifier.acknowledge_callback = acknowledge
        notifier.sync_persistent({"notice:1": self.notice()})
        notifier._ack_persistent("notice:1")
        self.assertTrue(entered.wait(1))
        notifier.sync_persistent({})
        notifier.sync_persistent({"notice:1": self.notice("Nueva incidencia", 2)})
        release.set()
        deadline = time.monotonic() + 1
        while notifier._ack_inflight and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(notifier._persistent_acknowledged("notice:1"))
        self.backend.sync_persistent()
        self.assertEqual(self.backend.popup_persistent_key, "notice:1")

    def test_visible_buttons_fit_without_overlap_across_priorities_at_supported_dpi(self):
        notifier = self.configure_popup()
        for dpi in (96, 120, 144, 192):
            with self.subTest(dpi=dpi):
                self.user.GetDpiForWindow.return_value = dpi
                for countdown in (False, True):
                    with self.subTest(countdown=countdown):
                        notices = {"notice:1": self.notice()}
                        if countdown:
                            notices["notice:2"] = self.notice("Cuenta", 3, countdown="a" * 32)
                        notifier.sync_persistent(notices)
                        self.backend.sync_persistent()
                        self.backend._layout_popup(dpi)
                        _, _, width, height = fixtures.latest_window_position(self.user, self.backend.popup_hwnd)
                        self.assertEqual(width, round(480 * dpi / 96))
                        buttons = self.backend.popup_buttons if countdown else self.backend.popup_buttons[:2]
                        positions = [fixtures.latest_window_position(self.user, hwnd) for hwnd in buttons]
                        for x, y, button_width, button_height in positions:
                            self.assertGreaterEqual(x, 0)
                            self.assertGreaterEqual(y, 0)
                            self.assertGreater(button_width, 0)
                            self.assertGreater(button_height, 0)
                            self.assertLessEqual(x + button_width, width)
                            self.assertLessEqual(y + button_height, height)
                        for index, (x, y, button_width, button_height) in enumerate(positions):
                            for other_x, other_y, other_width, other_height in positions[index + 1:]:
                                overlap_width = min(x + button_width, other_x + other_width) - max(x, other_x)
                                overlap_height = min(y + button_height, other_y + other_height) - max(y, other_y)
                                self.assertFalse(overlap_width > 0 and overlap_height > 0,
                                                 "Acknowledgment and cancellation targets must not overlap")
                notifier.sync_persistent({})
                self.backend.sync_persistent()
                self.user.SetWindowPos.reset_mock()

    def test_shutdown_countdown_uses_its_own_heading_and_separate_timer(self):
        notifier = self.configure_popup()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        notice["countdown"]["action"] = "shutdown"
        notice["countdown"]["remaining_seconds"] = 125
        with patch.object(wn.time, "monotonic", return_value=100):
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
        self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_title), "Apagado programado")
        self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "02:05")

    def test_changing_theme_preserves_live_episode_and_does_not_restart_its_timer(self):
        notifier = self.configure_popup()
        notifier.acknowledge_callback = Mock()
        notifier.cancel_countdown_callback = Mock()
        notice = self.notice("Protección", 3, countdown="a" * 32)
        with patch.object(wn.time, "monotonic", return_value=100) as clock, \
                patch.object(self.backend, "_play_sound") as sound:
            notifier.sync_persistent({"notice:1": notice})
            self.backend.sync_persistent()
            popup = self.backend.popup_hwnd
            notifier._backend = self.backend
            clock.return_value = 101
            notifier.set_theme("light")
            self.backend._window_proc(self.backend.hwnd, wn.WM_APP_NOTIFY, 0, 0)
            self.assertEqual(self.backend.popup_hwnd, popup)
            self.assertEqual(self.backend.popup_persistent_key, "notice:1")
            self.assertEqual(fixtures.latest_control_text(self.user, self.backend.popup_countdown), "00:29")
            self.assertFalse(notifier._persistent_acknowledged("notice:1"))
            notifier.acknowledge_callback.assert_not_called()
            notifier.cancel_countdown_callback.assert_not_called()
            sound.assert_not_called()
        notifier._backend = None


class CountdownClientTests(unittest.TestCase):
    def test_new_endpoint_is_used_without_legacy_fallback(self):
        client = LocalClient()
        client.token = "x" * 32
        client._request = Mock(side_effect=HTTPError(client.url, 404, "old service", {}, None))
        with self.assertRaises(HTTPError):
            client.cancel_countdown("a" * 32)
        client._request.assert_called_once_with("/api/protection/countdown/cancel", {"countdown_id": "a" * 32})

    def test_agent_requires_matching_cancel_confirmation(self):
        client = SimpleNamespace(cancel_countdown=Mock(return_value={"cancelled_countdown_id": "b" * 32,
                                                                    "protection": {"state": "cancelled"}}))
        agent = Agent(client, Mock(), session_state=lambda: "active")
        with self.assertRaises(ValueError):
            agent.cancel_countdown("a" * 32)
        client.cancel_countdown.return_value = {"cancelled_countdown_id": "a" * 32,
                                               "protection": {"state": "cancelled"}}
        self.assertEqual(agent.cancel_countdown("a" * 32)["cancelled_countdown_id"], "a" * 32)

    def test_slow_poll_cannot_rejuvenate_countdown_freshness(self):
        now = [100]
        metadata = {"title": "Cuenta", "message": "Activa", "priority": 3, "confirmed": True,
                    "countdown": {"countdown_id": "a" * 32, "action": "hibernate", "remaining_seconds": 30,
                                  "valid_for_ms": 3000, "cancelable": True}}
        def poll(*_args, **_kwargs):
            now[0] += 4
            return {"protocol": 2, "epoch": "a" * 32, "cursor": 1, "running": True,
                    "persistent": {"notice:1": metadata}, "messages": []}
        notifier = Mock()
        notifier.status.return_value = {"ready": True}
        agent = Agent(SimpleNamespace(connect=lambda: None, poll=poll), notifier,
                      clock=lambda: now[0], session_state=lambda: "active")
        agent.step()
        value = notifier.sync_persistent.call_args.args[0]["notice:1"]
        self.assertEqual(value["countdown"]["valid_for_ms"], 0)
        self.assertEqual(value["countdown"]["remaining_seconds"], 26)
        self.assertFalse(value["countdown"]["cancelable"])
        self.assertEqual(value["priority"], 1)

    def test_urgent_ticket_gets_capacity_when_lower_priority_inflight_slots_are_full(self):
        response = {"protocol": 2, "epoch": "a" * 32, "cursor": 32, "running": True,
                    "persistent": {}, "messages": [{"id": index, "title": "Aviso", "message": "Pendiente", "priority": 1}
                                                      for index in range(1, 33)]}
        notifier = Mock()
        notifier.status.return_value = {"ready": True}
        notifier.notify.return_value = True
        # Real class methods advertise receipt cancellation capability.
        class Receiver:
            def __init__(self):
                self.cancelled = []
                self.options = []
            def status(self):
                return {"ready": True}
            def sync_persistent(self, value):
                pass
            def notify(self, *args, **kwargs):
                self.options.append(kwargs)
                return True
            def cancel_delivery(self, delivery_id):
                self.cancelled.append(delivery_id)
        receiver = Receiver()
        client = SimpleNamespace(connect=lambda: None, poll=lambda *a, **k: response)
        agent = Agent(client, receiver, clock=lambda: 100, session_state=lambda: "active")
        agent.step()
        self.assertEqual(len(agent._inflight), 32)
        response["messages"].append({"id": 33, "title": "Urgente", "message": "Cuenta", "priority": 3})
        agent.step()
        self.assertIn("a" * 32 + ":33", agent._inflight)
        self.assertEqual(len(agent._inflight), 32)
        self.assertEqual(len(receiver.cancelled), 1)
        self.assertEqual(receiver.options[-1]["priority"], 3)


if __name__ == "__main__":
    unittest.main()
