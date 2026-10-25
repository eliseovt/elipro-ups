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
        self.user.SetWindowTextW.assert_any_call(self.backend.popup_title,
                                                "Elipro UPS · URGENTE · Hibernación en 30 segundos")
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
            self.user.SetWindowTextW.assert_any_call(self.backend.popup_title,
                                                    "Elipro UPS · URGENTE · Hibernación en 29 segundos")
            self.user.EnableWindow.assert_called_with(self.backend.popup_cancel, True)
            clock.return_value = 103.01
            self.backend._window_proc(self.backend.popup_hwnd, wn.WM_TIMER, 3, 0)
            self.user.SetWindowTextW.assert_any_call(self.backend.popup_title, "Elipro UPS · Estado sin confirmar")
            self.user.EnableWindow.assert_called_with(self.backend.popup_cancel, False)
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
            self.user.SetWindowTextW.assert_any_call(self.backend.popup_title, "Elipro UPS · Estado sin confirmar")
            body = self.user.SetWindowTextW.call_args.args[1]
            self.assertIn("No se confirma", body)
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
        self.assertIn("No se confirmó la cancelación", self.user.SetWindowTextW.call_args.args[1])
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

    def test_button_positions_and_window_size_remain_fixed_across_priorities_at_supported_dpi(self):
        notifier = self.configure_popup()
        for dpi in (96, 144, 192):
            with self.subTest(dpi=dpi):
                self.user.GetDpiForWindow.return_value = dpi
                notifier.sync_persistent({"notice:1": self.notice()})
                self.backend.sync_persistent()
                self.backend._layout_popup(dpi)
                controls = {hwnd: call.args[2:6] for call in self.user.SetWindowPos.call_args_list
                            if (hwnd := call.args[0]) in self.backend.popup_buttons}
                notifier.sync_persistent({"notice:1": self.notice(), "notice:2": self.notice("Cuenta", 3, countdown="a" * 32)})
                self.backend.sync_persistent()
                self.assertEqual(self.user.SetWindowPos.call_args.args[4:6],
                                 (round(420 * dpi / 96), round(230 * dpi / 96)))
                self.backend._layout_popup(dpi)
                for call in self.user.SetWindowPos.call_args_list:
                    if call.args[0] in controls:
                        self.assertEqual(call.args[2:6], controls[call.args[0]])
                notifier.sync_persistent({})
                self.backend.sync_persistent()
                self.user.SetWindowPos.reset_mock()


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
