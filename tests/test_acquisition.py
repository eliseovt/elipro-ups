"""Bounded acquisition, delayed delivery, and shutdown barriers without hardware."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.core import Store, TelemetryRingBuffer, iso_utc
from ups_monitor.server import Monitor


def sample(on_battery=False, low=False):
    return {"timestamp": iso_utc(), "on_battery": on_battery, "low_battery": low,
            "raw_flags": {"status_hex": "80" if on_battery else "00"}}


class RingBufferTests(unittest.TestCase):
    def test_owned_snapshots_bounded_samples_pending_and_single_wake(self):
        ring = TelemetryRingBuffer(3)
        original = sample()
        ring.publish(original, captured_at=100)
        original["raw_flags"]["status_hex"] = "mutated"
        self.assertEqual(ring.samples()[0]["raw_flags"]["status_hex"], "00")
        for _ in range(1000):
            ring.publish(sample(), captured_at=100)
        status = ring.status()
        self.assertEqual((status["samples"], status["pending"], status["dropped"]), (3, 3, 998))
        self.assertTrue(ring.wait(timeout=0))
        self.assertFalse(ring.wait(timeout=0), "The SimpleQueue must contain at most one wake token")
        entries, overflow = ring.drain()
        self.assertEqual([entry["sequence"] for entry in entries], [999, 1000, 1001])
        self.assertEqual(overflow["count"], 998)
        snapshots = ring.samples()
        snapshots[-1]["raw_flags"]["status_hex"] = "changed"
        entries[-1]["sample"]["raw_flags"]["status_hex"] = "also changed"
        self.assertEqual(ring.samples()[-1]["raw_flags"]["status_hex"], "00")
        self.assertEqual(ring.status()["pending"], 0)
        ring.publish(sample())
        self.assertTrue(ring.wait(timeout=0))

    def test_overflow_retains_alarm_and_transition_counts_without_current_latch(self):
        ring = TelemetryRingBuffer(2)
        ring.publish(sample(True, True))
        ring.publish(sample(False))
        ring.publish(sample(False))
        entries, overflow = ring.drain()
        self.assertEqual(overflow["transitions"]["battery_low"]["count"], 1)
        self.assertEqual(overflow["transitions"]["power_lost"]["count"], 1)
        self.assertFalse(entries[-1]["sample"]["low_battery"])
        overflow["transitions"].clear()
        self.assertIn("battery_low", ring.status()["last_overflow"]["transitions"])


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.action = Mock()
        self.adapter = SimpleNamespace(source="fake", read=lambda: sample())
        self.clock = Mock(return_value=100)
        self.alerts = Mock()
        self.alerts.status.return_value = {}
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action,
                               alerts=self.alerts, health_clock=self.clock,
                               telemetry_capacity=2, poll_interval=0.01)
        self.monitor.engine._clock = self.clock

    def tearDown(self):
        self.monitor.stop()
        self.store.close()

    def test_hardware_continues_during_blocked_consumer_with_bounded_memory(self):
        acquired = threading.Event()
        read_threads = set()
        count = 0

        def read():
            nonlocal count
            count += 1
            read_threads.add(threading.current_thread().name)
            if count >= 20:
                acquired.set()
            return sample(bool(count % 2))

        self.adapter.read = read
        with self.monitor.lock:
            self.monitor.start()
            self.assertTrue(acquired.wait(2), "USB polling must not wait for a stalled consumer")
            status = self.monitor.telemetry.status()
            self.assertEqual(status["samples"], 2)
            self.assertLessEqual(status["pending"], 2)
            self.assertGreater(status["dropped"], 0)
            self.assertEqual(self.store.events(), [])
            self.assertEqual(read_threads, {"ups-hardware-polling"})
            with self.assertRaisesRegex(RuntimeError, "dedicado"):
                self.monitor.tick()
        self.monitor.stop()
        self.assertFalse(self.monitor.processor_thread.is_alive())
        self.assertFalse(self.monitor.thread.is_alive())
        self.action.assert_not_called()

    def test_old_low_alarm_is_recorded_historically_and_never_fires_after_recovery(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.telemetry.publish(sample(True, True), captured_at=100)
        self.monitor.telemetry.publish(sample(False), captured_at=100)
        self.monitor.telemetry.publish(sample(False), captured_at=100)
        self.clock.return_value = 111
        self.monitor._consume_once()
        self.assertEqual(self.monitor.engine.status()["state"], "online")
        delayed = [event for event in self.store.events() if event["kind"] == "telemetry_delayed_transition"]
        self.assertTrue(any(event["data"]["transition"] == "battery_low" for event in delayed))
        self.assertNotIn("battery_low", [call.args[0] for call in self.alerts.on_event.call_args_list])
        self.action.assert_not_called()

    def test_overflow_restarts_countdown_from_current_sample_not_unobserved_time(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: sample(True)
        self.monitor.tick()
        self.clock.return_value = 111
        for _ in range(20):
            self.monitor.telemetry.publish(sample(True), captured_at=111)
        self.monitor._consume_once()
        self.assertEqual(self.monitor.engine.status()["countdown_remaining"], 10)
        self.action.assert_not_called()

    def test_monotonic_delivery_age_blocks_fresh_looking_delayed_timestamp(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.monitor.telemetry.publish(sample(True, True), captured_at=100)
        self.clock.return_value = 116
        self.monitor._consume_once()
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.monitor.engine.status()["state"], "unknown")
        self.action.assert_not_called()

    def test_superseded_delivery_cannot_reset_cancellation_or_invoke_action(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: sample(True)
        self.monitor.tick()
        self.monitor.cancel()
        self.monitor._processing_sequence = self.monitor.telemetry.publish(sample(False), captured_at=100)
        self.monitor.telemetry.publish(sample(True, True), captured_at=100)
        try:
            self.monitor._process_sample(sample(False))
        finally:
            self.monitor._processing_sequence = None
        self.assertTrue(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_online_sample_arriving_during_evaluation_blocks_old_battery_action(self):
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 10})
        self.adapter.read = lambda: sample(True)
        self.monitor.tick()
        self.clock.return_value = 111
        self.monitor.telemetry.publish(sample(True), captured_at=111)
        original_act = self.monitor.engine._action_callback

        def raced_action(action):
            self.monitor.telemetry.publish(sample(False), captured_at=111)
            original_act(action)

        self.monitor.engine._action_callback = raced_action
        self.monitor._consume_once()
        self.assertEqual(self.monitor.engine.status()["state"], "action_failed")
        self.assertIn("más reciente", self.monitor.engine.status()["reason"])
        self.action.assert_not_called()
        self.monitor._consume_once()
        self.assertEqual(self.monitor.engine.status()["state"], "online")

    def test_intentional_backoff_does_not_report_a_hung_worker(self):
        self.monitor.thread = Mock(is_alive=Mock(return_value=True))
        self.monitor.processor_thread = Mock(is_alive=Mock(return_value=True))
        try:
            self.monitor.health.finish_read(False)
            self.monitor._next_poll_at = 130
            self.monitor._last_processed_at = 100
            self.clock.return_value = 120
            self.monitor._consume_once()
            self.assertIsNone(self.monitor._health_problem())
        finally:
            self.monitor.thread = self.monitor.processor_thread = None

    def test_dead_consumer_is_degraded_and_actions_are_blocked(self):
        self.monitor.tick()
        self.monitor.processor_thread = Mock(is_alive=Mock(return_value=False))
        try:
            self.assertEqual(self.monitor.health_status()["state"], "degraded")
            with self.assertRaisesRegex(RuntimeError, "procesador"):
                self.monitor._act("shutdown")
            self.action.assert_not_called()
        finally:
            self.monitor.processor_thread = None


if __name__ == "__main__":
    unittest.main()
