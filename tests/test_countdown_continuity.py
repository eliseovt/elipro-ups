"""Brief refresh gaps allow episode-bound requests, never local cancellation success."""
from copy import deepcopy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import PolicyEngine, Store, iso_utc
from ups_monitor.notification_priority import age_notice_metadata, normalize_notice_metadata
from ups_monitor.session_broker import SessionBroker
from ups_monitor.windows_notifications import COUNTDOWN_REFRESH_GRACE_SECONDS, WindowsNotifier
from ups_monitor import windows_notifications as wn
from tests.test_alerts import Clock


class CountdownContinuityTests(unittest.TestCase):
    key = "notice:1"

    def setUp(self):
        self.clock = Clock()
        timer = patch.object(wn.time, "monotonic", side_effect=self.clock.monotonic)
        timer.start()
        self.addCleanup(timer.stop)
        self.receiver = WindowsNotifier("http://127.0.0.1:8765")
        self.receiver.cancel_countdown_callback = Mock()
        self.addCleanup(self.receiver.stop)

    @staticmethod
    def notice(*, identifier="a" * 32, confirmed=True, confirmed_for_ms=3000,
               remaining_seconds=30, valid_for_ms=3000, **countdown_changes):
        return {"title": "Protección", "message": "Cuenta activa", "priority": 3,
                "severity": "critical", "sound": False, "confirmed": confirmed,
                "confirmed_for_ms": confirmed_for_ms,
                "countdown": {"countdown_id": identifier, "action": "hibernate",
                              "remaining_seconds": remaining_seconds, "valid_for_ms": valid_for_ms,
                              "cancelable": True, **countdown_changes}}

    def receive(self, notice):
        self.receiver.sync_persistent({self.key: notice})

    def current(self):
        return self.receiver._persistent_message(self.key)

    def request_and_wait(self):
        completed = threading.Event()
        def wake():
            if not self.receiver._countdown_cancel_inflight:
                completed.set()
        backend = SimpleNamespace(wake=wake)
        with patch.object(self.receiver, "_backend", backend):
            self.receiver._cancel_persistent_countdown(self.key)
            self.assertTrue(completed.wait(1), "Cancellation worker must finish without blocking the UI")

    def test_normal_pipeline_phase_gap_is_refresh_pending_until_new_snapshot_arrives(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        broker = SessionBroker(clock=self.clock.monotonic)
        alerts = AlertManager(store, broker, clock=self.clock.monotonic, utc_clock=self.clock.utc)
        alerts.start()
        self.addCleanup(alerts.stop)
        actions = Mock()
        engine = PolicyEngine({"enabled": True, "countdown_seconds": 60}, actions,
                              clock=self.clock.monotonic, utc_clock=self.clock.utc)

        def sample():
            return {"timestamp": iso_utc(self.clock.utc()), "on_battery": True,
                    "fault": False, "low_battery": False, "load_percent": 10, "battery_percent": 80}

        def poll():
            return broker.poll({"client_id": "a" * 32, "after": None,
                "delivery": {"ready": True}, "protocol": 2, "acks": [],
                "epoch": None, "session_state": "active"})["persistent"]

        reading = sample()
        engine.evaluate(reading)
        alerts.update(engine.status(), "connected", reading)
        self.clock.seconds = 2
        alerts.update(engine.status(), "connected", reading)
        self.clock.seconds = 2.4
        reading = sample()
        engine.evaluate(reading)
        self.clock.seconds = 2.9
        persistent = poll()
        self.key = next(iter(persistent))
        self.assertAlmostEqual(persistent[self.key]["countdown"]["valid_for_ms"], 100)
        self.receiver.sync_persistent(persistent)
        deadline = self.receiver._persistent_countdowns[self.key]
        self.clock.seconds = 3
        alerts.update(engine.status(), "connected", reading)
        self.clock.seconds = 3.2
        self.assertEqual(engine.status()["state"], "countdown")
        self.assertEqual(engine.status()["countdown_valid_for_ms"], 2200)
        pending = self.current()
        self.assertTrue(pending["countdown"]["refresh_pending"])
        self.assertGreater(pending["countdown"]["remaining_seconds"], 0)
        self.assertFalse(pending["confirmed"])
        self.assertEqual(pending["countdown"]["valid_for_ms"], 0)
        self.assertFalse(pending["countdown"]["cancelable"])
        self.assertTrue(self.receiver._countdown_cancel_request_allowed(pending))
        self.receiver.cancel_countdown_callback.side_effect = TimeoutError("monitor ocupado")
        self.request_and_wait()
        self.receiver.cancel_countdown_callback.assert_called_once_with(pending["countdown"]["countdown_id"])
        self.assertIn("No se confirmó", self.receiver._countdown_feedback(pending["countdown"]["countdown_id"])[0])
        self.clock.seconds = 3.9
        self.receiver.sync_persistent(poll())
        renewed = self.current()
        self.assertTrue(renewed["confirmed"])
        self.assertNotIn("refresh_pending", renewed["countdown"])
        self.assertEqual(self.receiver._persistent_countdowns[self.key], deadline)
        actions.assert_not_called()

    def test_refresh_grace_is_bounded_by_last_authoritative_validity_not_status_reads(self):
        self.receive(self.notice())
        confirmation = self.receiver._persistent_confirmations[self.key]
        self.assertEqual(confirmation, ("a" * 32, 3.0))
        for instant in (3.1, 4, 5, 6.99):
            self.clock.seconds = instant
            value = self.current()
            self.assertTrue(value["countdown"]["refresh_pending"])
            self.assertFalse(value["confirmed"])
            self.assertFalse(value["countdown"]["cancelable"])
            self.assertEqual(value["priority"], 1)
            self.assertEqual(self.receiver._persistent_confirmations[self.key], confirmation)
        self.clock.seconds = confirmation[1] + COUNTDOWN_REFRESH_GRACE_SECONDS
        self.assertNotIn("refresh_pending", self.current()["countdown"])
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))

    def test_stale_snapshots_never_renew_grace_or_deadline(self):
        self.receive(self.notice())
        confirmation = self.receiver._persistent_confirmations[self.key]
        deadline = self.receiver._persistent_countdowns[self.key]
        for instant in (3.2, 4, 5.9, 7.1):
            self.clock.seconds = instant
            self.receive(self.notice(confirmed=False, confirmed_for_ms=0, valid_for_ms=0))
            self.assertEqual(self.receiver._persistent_confirmations[self.key], confirmation)
            self.assertEqual(self.receiver._persistent_countdowns[self.key], deadline)
        self.assertNotIn("refresh_pending", self.current()["countdown"])

    def test_shorter_confirmed_lease_limits_grace_for_monitor_validated_requests(self):
        self.receive(self.notice(confirmed_for_ms=500))
        self.assertEqual(self.receiver._persistent_confirmations[self.key], ("a" * 32, .5))
        self.clock.seconds = .6
        value = self.current()
        self.assertTrue(value["countdown"]["refresh_pending"])
        self.assertFalse(value["confirmed"])
        self.assertGreater(value["countdown"]["valid_for_ms"], 0)
        self.assertTrue(self.receiver._countdown_cancel_request_allowed(value))
        self.receiver.cancel_countdown_callback.return_value = {
            "cancelled_countdown_id": "a" * 32, "protection": {"state": "cancelled"}}
        self.request_and_wait()
        self.receiver.cancel_countdown_callback.assert_called_once_with("a" * 32)
        self.assertIn("confirmó la cancelación", self.receiver._countdown_feedback("a" * 32)[0])
        self.assertIsNone(self.current(), "A confirmed cancellation must retire the matching local clock")
        self.clock.seconds = 4.5
        self.assertIsNone(self.current())

    def test_never_confirmed_or_slow_payload_cannot_seed_continuity(self):
        for notice in (self.notice(confirmed=False),
                       self.notice(confirmed_for_ms=0),
                       self.notice(valid_for_ms=0),
                       age_notice_metadata(self.notice(), 4)):
            with self.subTest(notice=notice):
                self.receiver.sync_persistent({})
                self.receive(notice)
                self.assertEqual(self.receiver._persistent_confirmations, {})
                self.assertNotIn("refresh_pending", self.current()["countdown"])
                self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))

    def test_incoming_refresh_hint_is_not_trusted_or_persisted(self):
        source = self.notice(confirmed=False, confirmed_for_ms=0, valid_for_ms=0, refresh_pending=True)
        original = deepcopy(source)
        self.assertNotIn("refresh_pending", normalize_notice_metadata(source)["countdown"])
        self.receive(source)
        self.assertNotIn("refresh_pending", self.current()["countdown"])
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.assertEqual(source, original)

    def test_new_episode_does_not_inherit_confirmation_and_fresh_episode_can_seed_its_own(self):
        self.receive(self.notice())
        self.clock.seconds = 3.2
        self.receive(self.notice(identifier="b" * 32, confirmed=False, valid_for_ms=0))
        self.assertEqual(self.receiver._persistent_confirmations, {})
        self.assertNotIn("refresh_pending", self.current()["countdown"])
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.receive(self.notice(identifier="b" * 32))
        self.clock.seconds = 6.3
        self.assertTrue(self.current()["countdown"]["refresh_pending"])
        self.assertEqual(self.receiver._persistent_confirmations[self.key][0], "b" * 32)

    def test_removal_non_countdown_session_loss_and_stop_clear_continuity(self):
        for reset in ("remove", "non_countdown", "session", "stop"):
            with self.subTest(reset=reset):
                self.clock.seconds = 0
                self.receive(self.notice())
                if reset == "remove":
                    self.receiver.sync_persistent({})
                elif reset == "non_countdown":
                    self.receive({"title": "Revisión", "message": "Abre el panel"})
                elif reset == "session":
                    self.receiver.set_session_available(False)
                    self.receiver.set_session_available(True)
                else:
                    self.receiver.stop()
                self.assertEqual(self.receiver._persistent_confirmations, {})
                if reset != "stop":
                    self.clock.seconds = 3.2
                    self.receive(self.notice(confirmed=False, valid_for_ms=0))
                    self.assertNotIn("refresh_pending", self.current()["countdown"])

    def test_zero_remaining_never_uses_refresh_grace(self):
        self.receive(self.notice(remaining_seconds=1))
        self.clock.seconds = 1.1
        value = self.current()
        self.assertEqual(value["countdown"]["remaining_seconds"], 0)
        self.assertFalse(value["confirmed"])
        self.assertNotIn("refresh_pending", value["countdown"])
        self.receiver._cancel_persistent_countdown(self.key)
        self.receiver.cancel_countdown_callback.assert_not_called()

    def test_fresh_legacy_countdown_without_confirmation_fields_can_seed_continuity(self):
        source = self.notice()
        source.pop("confirmed")
        source.pop("confirmed_for_ms")
        self.receive(source)
        self.clock.seconds = 3.1
        self.assertTrue(self.current()["countdown"]["refresh_pending"])

    def test_refresh_request_requires_last_genuine_permission_and_honors_revocation(self):
        self.receive(self.notice(cancelable=False))
        self.clock.seconds = 3.2
        self.assertTrue(self.current()["countdown"]["refresh_pending"])
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.receiver._cancel_persistent_countdown(self.key)
        self.receiver.cancel_countdown_callback.assert_not_called()

        self.receiver.sync_persistent({})
        self.clock.seconds = 10
        self.receive(self.notice())
        self.clock.seconds = 11
        self.receive(self.notice(remaining_seconds=29, cancelable=False))
        self.clock.seconds = 14.1
        self.assertTrue(self.current()["countdown"]["refresh_pending"])
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))

    def test_aged_snapshot_does_not_revoke_last_genuine_permission_during_bounded_gap(self):
        self.receive(self.notice())
        self.clock.seconds = 3.2
        self.receive(self.notice(confirmed=False, confirmed_for_ms=0, valid_for_ms=0,
                                 remaining_seconds=26.8, cancelable=False))
        value = self.current()
        self.assertFalse(value["countdown"]["cancelable"])
        self.assertTrue(self.receiver._countdown_cancel_request_allowed(value))
        self.clock.seconds = 7.01
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))

    def test_session_loss_prevents_even_an_otherwise_current_request(self):
        self.receive(self.notice())
        self.receiver.set_session_available(False)
        self.assertFalse(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.receiver._cancel_persistent_countdown(self.key)
        self.receiver.cancel_countdown_callback.assert_not_called()

    def test_only_explicit_matching_cancelled_result_reports_success(self):
        self.receive(self.notice())
        self.clock.seconds = 3.2
        invalid_results = (
            {"cancelled_countdown_id": "b" * 32, "protection": {"state": "cancelled"}},
            {"cancelled_countdown_id": "a" * 32},
            *({"cancelled_countdown_id": "a" * 32, "protection": {"state": state}}
              for state in (None, "idle", "blocked", "countdown", "executed")),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                self.receiver.cancel_countdown_callback.return_value = result
                self.request_and_wait()
                self.assertIn("No se confirmó", self.receiver._countdown_feedback("a" * 32)[0])
                self.assertTrue(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.receiver.cancel_countdown_callback.return_value = {
            "cancelled_countdown_id": "a" * 32, "protection": {"state": "cancelled"}}
        self.request_and_wait()
        self.assertIn("confirmó la cancelación", self.receiver._countdown_feedback("a" * 32)[0])

    def test_confirmed_cancelled_episode_cannot_reappear_from_delayed_snapshots(self):
        self.receive(self.notice())
        self.clock.seconds = 3.2
        self.receiver.cancel_countdown_callback.return_value = {
            "cancelled_countdown_id": "a" * 32, "protection": {"state": "cancelled"}}
        self.request_and_wait()
        self.assertIsNone(self.current())
        self.assertFalse(self.receiver._persistent_acknowledged(self.key), "Cancel is not a reading acknowledgement")
        for previous_snapshot in (
            self.notice(confirmed=False, confirmed_for_ms=0, valid_for_ms=0), self.notice()):
            with self.subTest(previous_snapshot=previous_snapshot):
                self.clock.seconds += .1
                self.receive(previous_snapshot)
                self.assertIsNone(self.current(), "An old poll must not restore a cancelled clock")
        self.receive(self.notice(identifier="b" * 32))
        self.assertEqual(self.current()["countdown"]["countdown_id"], "b" * 32)
        self.assertTrue(self.receiver._countdown_cancel_request_allowed(self.current()))

    def test_late_monitor_confirmation_does_not_retire_new_episode_using_same_notice_key(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def cancel(identifier):
            entered.set()
            release.wait(2)
            return {"cancelled_countdown_id": identifier, "protection": {"state": "cancelled"}}

        def wake():
            if not self.receiver._countdown_cancel_inflight:
                completed.set()

        self.receive(self.notice())
        self.clock.seconds = 3.2
        self.receiver.cancel_countdown_callback = Mock(side_effect=cancel)
        with patch.object(self.receiver, "_backend", SimpleNamespace(wake=wake)):
            self.receiver._cancel_persistent_countdown(self.key)
            self.assertTrue(entered.wait(1))
            self.clock.seconds = 3.3
            self.receive(self.notice(identifier="b" * 32))
            self.assertEqual(self.current()["countdown"]["countdown_id"], "b" * 32)
            release.set()
            self.assertTrue(completed.wait(1))
        self.assertEqual(self.current()["countdown"]["countdown_id"], "b" * 32)
        self.assertIsNone(self.receiver._countdown_feedback("b" * 32)[0])
        self.assertTrue(self.receiver._countdown_cancel_request_allowed(self.current()))
        self.receiver.cancel_countdown_callback.assert_called_once_with("a" * 32)


if __name__ == "__main__":
    unittest.main()
