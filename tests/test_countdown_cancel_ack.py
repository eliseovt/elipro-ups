"""Only an explicit monitor acknowledgement can confirm cancellation.

All requests, clocks and Windows APIs are fakes. No monitor or power executor
is started by these tests.
"""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ups_monitor import windows_notifications as wn
from ups_monitor.session_agent import Agent
from tests import test_windows_notifications as fixtures


REAL_MONOTONIC = time.monotonic


def acknowledgement(identifier, state="cancelled"):
    return {"cancelled_countdown_id": identifier, "protection": {"state": state}}


def notice(identifier="a" * 32):
    return {"title": "Protección", "message": "Cuenta activa", "priority": 3,
            "severity": "critical", "sound": False, "confirmed": True,
            "confirmed_for_ms": 3000,
            "countdown": {"countdown_id": identifier, "action": "hibernate",
                          "remaining_seconds": 30, "valid_for_ms": 3000,
                          "cancelable": True}}


def malformed_responses(identifier):
    yield None
    yield []
    yield {}
    yield {"cancelled_countdown_id": identifier}
    for protection in (None, [], "cancelled", {}, {"state": None}):
        yield {"cancelled_countdown_id": identifier, "protection": protection}
    for state in ("idle", "blocked", "unknown", "countdown", "executed", "action_started", "action_failed"):
        yield acknowledgement(identifier, state)
    yield acknowledgement("b" * 32)


def wait_for_cancel(test, notifier, identifier):
    deadline = REAL_MONOTONIC() + 2
    while notifier._countdown_feedback(identifier)[1] and REAL_MONOTONIC() < deadline:
        time.sleep(.001)
    test.assertFalse(notifier._countdown_feedback(identifier)[1], "Cancellation worker did not finish")


class AgentCancellationAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(cancel_countdown=Mock(), cancel=Mock())
        self.agent = Agent(self.client, Mock(), session_state=lambda: "active")

    def test_incomplete_or_non_cancelled_response_is_never_confirmation(self):
        identifier = "a" * 32
        for response in malformed_responses(identifier):
            with self.subTest(response=response):
                self.client.cancel_countdown.return_value = response
                with self.assertRaises(ValueError):
                    self.agent.cancel_countdown(identifier)
        self.client.cancel.assert_not_called()

    def test_matching_id_and_explicit_cancelled_state_are_required(self):
        identifier = "a" * 32
        response = acknowledgement(identifier)
        self.client.cancel_countdown.return_value = response
        self.assertIs(self.agent.cancel_countdown(identifier), response)
        self.client.cancel_countdown.assert_called_once_with(identifier)
        self.client.cancel.assert_not_called()

    def test_timeout_conflict_and_busy_propagate_without_legacy_fallback(self):
        for error in (OSError("timeout"), HTTPError("http://127.0.0.1", 409, "changed", {}, None),
                      HTTPError("http://127.0.0.1", 503, "busy", {}, None)):
            with self.subTest(error=error):
                self.client.cancel_countdown.side_effect = error
                with self.assertRaises(type(error)):
                    self.agent.cancel_countdown("a" * 32)
        self.client.cancel.assert_not_called()


class NativeCancellationAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.timer = patch.object(wn.time, "monotonic", return_value=100.0)
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.notifier = wn.WindowsNotifier("http://127.0.0.1:8765")
        self.notifier.cancel_countdown_callback = Mock()
        self.notifier.acknowledge_callback = Mock()
        self.notifier._backend = SimpleNamespace(wake=lambda: True, request_stop=lambda: None)
        self.addCleanup(self.notifier.stop)

    def seed(self, identifier="a" * 32, key="notice:1"):
        self.notifier.sync_persistent({key: notice(identifier)})

    def test_malformed_acknowledgements_keep_notice_and_reading_pending(self):
        identifier = "a" * 32
        for response in malformed_responses(identifier):
            with self.subTest(response=response):
                self.seed(identifier)
                self.notifier.cancel_countdown_callback.return_value = response
                attempts = self.notifier.cancel_countdown_callback.call_count
                self.notifier._cancel_persistent_countdown("notice:1")
                wait_for_cancel(self, self.notifier, identifier)
                self.assertEqual(self.notifier.cancel_countdown_callback.call_count, attempts + 1)
                feedback, inflight = self.notifier._countdown_feedback(identifier)
                self.assertFalse(inflight)
                self.assertIn("No se confirmó la cancelación", feedback)
                self.assertIn("notice:1", self.notifier._persistent_keys())
                self.assertNotIn(identifier, self.notifier._confirmed_countdown_cancellations)
        self.notifier.acknowledge_callback.assert_not_called()
        self.assertEqual(self.notifier._persistent_dismissed, set())

    def test_confirmed_cancellation_retires_local_notice_and_ignores_stale_snapshot(self):
        identifier = "a" * 32
        self.seed(identifier)
        self.notifier.cancel_countdown_callback.return_value = acknowledgement(identifier)
        self.notifier._cancel_persistent_countdown("notice:1")
        wait_for_cancel(self, self.notifier, identifier)
        self.assertNotIn("notice:1", self.notifier._persistent_keys())
        self.assertIn(identifier, self.notifier._confirmed_countdown_cancellations)
        self.seed(identifier)
        self.assertNotIn("notice:1", self.notifier._persistent_keys())
        self.seed("b" * 32)
        self.assertEqual(self.notifier._persistent_message("notice:1")["countdown"]["countdown_id"], "b" * 32)
        self.notifier.acknowledge_callback.assert_not_called()
        self.assertEqual(self.notifier._persistent_dismissed, set())

    def test_late_success_for_old_episode_does_not_retire_new_episode(self):
        old, current = "a" * 32, "b" * 32
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def cancel(identifier):
            entered.set()
            release.wait(1)
            return acknowledgement(identifier)

        self.notifier.cancel_countdown_callback.side_effect = cancel
        self.seed(old)
        self.notifier._cancel_persistent_countdown("notice:1")
        self.assertTrue(entered.wait(1))
        self.seed(current)
        release.set()
        wait_for_cancel(self, self.notifier, old)
        self.assertEqual(self.notifier._persistent_message("notice:1")["countdown"]["countdown_id"], current)
        self.assertIn(old, self.notifier._confirmed_countdown_cancellations)
        self.assertNotIn(current, self.notifier._confirmed_countdown_cancellations)
        self.assertNotIn(old, self.notifier._countdown_cancel_feedback)
        self.notifier.sync_persistent({"notice:old": notice(old), "notice:1": notice(current)})
        self.assertEqual(self.notifier._persistent_keys(), {"notice:1"})
        self.notifier.acknowledge_callback.assert_not_called()

    def test_timeout_never_retires_notice_and_matching_retry_can_confirm(self):
        identifier = "a" * 32
        self.seed(identifier)
        self.notifier.cancel_countdown_callback.side_effect = OSError("timeout")
        self.notifier._cancel_persistent_countdown("notice:1")
        wait_for_cancel(self, self.notifier, identifier)
        self.assertIn("notice:1", self.notifier._persistent_keys())
        self.assertNotIn(identifier, self.notifier._confirmed_countdown_cancellations)
        self.notifier.cancel_countdown_callback.side_effect = None
        self.notifier.cancel_countdown_callback.return_value = acknowledgement(identifier)
        self.notifier._cancel_persistent_countdown("notice:1")
        wait_for_cancel(self, self.notifier, identifier)
        self.assertNotIn("notice:1", self.notifier._persistent_keys())
        self.assertIn(identifier, self.notifier._confirmed_countdown_cancellations)
        self.assertEqual(self.notifier.cancel_countdown_callback.call_count, 2)


class ConfirmedCancellationReplayTests(unittest.TestCase):
    setUp = fixtures.NativeShellTests.setUp
    configure_popup = fixtures.NativeShellTests.configure_popup
    query_accepts_notifications = staticmethod(fixtures.NativeShellTests.query_accepts_notifications)

    def test_queued_and_late_tickets_for_confirmed_episode_do_not_reopen_or_sound(self):
        notifier = self.configure_popup()
        notifier._backend = self.backend
        notifier._ready = True
        notifier.acknowledge_callback = Mock()
        self.addCleanup(notifier.stop)
        identifier = "a" * 32
        with patch.object(wn.time, "monotonic", return_value=100.0):
            notifier.sync_persistent({"notice:1": notice(identifier)})
            notifier.notify("Protección", "Cuenta activa", severity="critical", sound=True,
                            persistent=True, key="notice:1", delivery_id="epoch:before")
            notifier.cancel_countdown_callback = Mock(return_value=acknowledgement(identifier))
            notifier._cancel_persistent_countdown("notice:1")
            wait_for_cancel(self, notifier, identifier)
            notifier.sync_persistent({"notice:1": notice(identifier)})
            notifier.notify("Protección", "Snapshot antiguo", severity="critical", sound=True,
                            persistent=True, key="notice:1", delivery_id="epoch:after")
            with patch.object(self.backend, "_play_sound") as sound:
                notifier._drain(self.backend)
                sound.assert_not_called()
            self.assertIsNone(self.backend.popup_hwnd)
            self.assertFalse(any(item["outcome"] == "accepted" for item in notifier.drain_receipts()))
            notifier.acknowledge_callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
