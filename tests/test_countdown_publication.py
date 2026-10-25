"""Publish fresh countdown observations before storage or the next watchdog tick."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store, iso_utc
from ups_monitor.server import CountdownConflictError, Monitor
from ups_monitor.session_broker import SessionBroker


class Clock:
    def __init__(self):
        self.seconds = 100.0
        self.epoch = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def utc(self):
        return self.epoch + timedelta(seconds=self.seconds - 100)

    def advance(self, seconds):
        self.seconds += seconds


class CountdownPublicationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")
        self.broker = SessionBroker(clock=self.clock.monotonic)
        self.alerts = AlertManager(self.store, self.broker,
            clock=self.clock.monotonic, utc_clock=self.clock.utc)
        self.actions = Mock()
        # Never start a reader, monitor thread or operating-system executor.
        self.adapter = SimpleNamespace(source="test", read=Mock(side_effect=AssertionError("No USB read expected")))
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.actions,
                               alerts=self.alerts, health_clock=self.clock.monotonic)
        self.addCleanup(self.store.close)
        self.addCleanup(self.monitor.stop)
        self.monitor.engine._clock = self.clock.monotonic
        self.monitor.engine._utc_clock = self.clock.utc
        self.monitor._fresh_sample = self.fresh_sample
        self.monitor.engine.update_policy({"enabled": True, "countdown_seconds": 60, "action": "hibernate"})
        self.alerts.start()
        self.poll()

    def fresh_sample(self, sample):
        if not isinstance(sample, dict) or sample.get("connected", True) is not True:
            return False
        observed = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
        return -5 <= (self.clock.utc() - observed).total_seconds() <= 15

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": True,
                "low_battery": False, "fault": False, "battery_percent": 80,
                "battery_voltage": 27.4, "load_percent": 10,
                "input_voltage": 0, "output_voltage": 220, **changes}

    def poll(self):
        return self.broker.poll({"client_id": "a" * 32, "after": None,
                                 "delivery": {"ready": True, "last_error": None}})

    def countdown(self):
        return next((notice for notice in self.poll()["persistent"].values()
                     if notice.get("countdown") is not None), None)

    def begin(self):
        self.monitor._process_sample(self.sample())
        current = self.countdown()
        self.assertIsNotNone(current)
        self.assertEqual(current["priority"], 3)
        self.assertTrue(current["confirmed"])
        return current["countdown"]["countdown_id"]

    def test_fresh_sample_is_published_before_persistence_and_next_supervisor_tick(self):
        identifier = self.begin()
        self.clock.advance(2.4)
        persisted = []
        original = self.store.add_sample

        def save(sample):
            persisted.append(self.countdown())
            return original(sample)

        self.monitor.last_saved = 0  # Force the existing persistence branch.
        with patch.object(self.monitor, "_supervise_once") as supervisor, \
                patch.object(self.store, "add_sample", side_effect=save):
            self.monitor._process_sample(self.sample())
        supervisor.assert_not_called()
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["countdown"]["countdown_id"], identifier)
        self.assertAlmostEqual(persisted[0]["countdown"]["valid_for_ms"], 3000, delta=1)
        self.clock.advance(.5)
        received = self.countdown()
        self.assertEqual(received["countdown"]["countdown_id"], identifier)
        self.assertAlmostEqual(received["countdown"]["valid_for_ms"], 2500, delta=1)
        self.assertTrue(received["confirmed"])
        self.assertTrue(received["countdown"]["cancelable"])
        self.actions.assert_not_called()
        self.adapter.read.assert_not_called()

    def test_status_and_poll_reads_never_renew_the_original_freshness(self):
        identifier = self.begin()
        self.clock.advance(2)
        for _ in range(3):
            self.monitor.snapshot()
            self.assertAlmostEqual(self.countdown()["countdown"]["valid_for_ms"], 1000, delta=1)
        self.clock.advance(1.01)
        self.monitor.snapshot()
        expired = self.countdown()
        self.assertEqual(self.monitor.engine.status()["countdown_id"], identifier)
        self.assertEqual(expired["countdown"]["valid_for_ms"], 0)
        self.assertFalse(expired["countdown"]["cancelable"])
        self.assertFalse(expired["confirmed"])
        self.actions.assert_not_called()
        self.adapter.read.assert_not_called()

    def test_online_recovery_immediately_retires_countdown_and_preserves_incident_history(self):
        self.begin()
        self.clock.advance(.2)
        self.monitor._process_sample(self.sample(on_battery=False, input_voltage=220))
        self.assertIsNone(self.countdown())
        self.assertIsNone(self.monitor.engine.status()["countdown_id"])
        incident = next(notice for notice in self.store.pending_notifications()
                        if notice["kind"] == "policy_countdown")
        self.assertIsNotNone(incident["resolved_at"])
        self.assertIsNone(incident["acknowledged_at"])
        self.actions.assert_not_called()

    def test_real_communication_loss_immediately_retires_the_episode_and_rejects_cancellation(self):
        identifier = self.begin()
        self.clock.advance(.4)
        self.monitor._unavailable("Pérdida de comunicación simulada")
        self.assertIsNone(self.countdown())
        self.assertIsNone(self.monitor.engine.status()["countdown_id"])
        self.assertEqual(self.monitor.engine.status()["state"], "unknown")
        with self.assertRaises(CountdownConflictError):
            self.monitor.cancel_countdown(identifier)
        self.actions.assert_not_called()
        self.adapter.read.assert_not_called()

    def test_superseded_sample_publishes_invalidation_before_returning(self):
        self.begin()
        self.clock.advance(.2)
        self.monitor._processing_sequence = 12
        with patch.object(self.monitor.telemetry, "is_latest", return_value=False):
            self.monitor._process_sample(self.sample())
        self.monitor._processing_sequence = None
        self.assertIsNone(self.countdown())
        self.assertIsNone(self.monitor.engine.status()["countdown_id"])
        self.actions.assert_not_called()

    def test_publication_failure_does_not_interrupt_policy_evaluation_or_recording(self):
        with patch.object(self.alerts, "update", side_effect=RuntimeError("Error de presentación simulado")), \
                self.assertLogs("elipro.monitor", level="ERROR"):
            self.monitor._process_sample(self.sample())
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        self.assertIsNotNone(self.monitor.sample)
        self.assertIsNotNone(self.monitor.last_saved_timestamp)
        self.actions.assert_not_called()
        self.adapter.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
