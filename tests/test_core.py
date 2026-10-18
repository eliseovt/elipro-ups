import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ups_monitor.core import DEFAULT_POLICY, PolicyEngine, Store, iso_utc, validate_policy


class Clock:
    def __init__(self):
        self.seconds = 0
        self.epoch = datetime(2026, 10, 17, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def utc(self):
        return self.epoch + timedelta(seconds=self.seconds)

    def step(self, seconds):
        self.seconds += seconds


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.actions = []
        self.events = []
        self.engine = PolicyEngine(
            {"enabled": True, "on_battery_seconds": 20, "countdown_seconds": 10},
            self.actions.append, clock=self.clock.monotonic, utc_clock=self.clock.utc,
            event_callback=lambda *event: self.events.append(event))

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": True,
                "low_battery": False, "battery_percent": 80, "runtime_seconds": None, **changes}

    def advance(self, seconds, **changes):
        for _ in range(seconds):
            self.clock.step(1)
            self.engine.evaluate(self.sample(**changes))

    def test_time_threshold_and_countdown_fire_once(self):
        self.engine.evaluate(self.sample())
        self.advance(20)
        self.assertEqual(self.engine.status()["countdown_remaining"], 10)
        self.advance(9)
        self.assertEqual(self.actions, [])
        self.advance(10)
        self.assertEqual(self.actions, ["shutdown"])

    def test_restored_power_cancels(self):
        self.engine.evaluate(self.sample(low_battery=True))
        self.advance(5, on_battery=False)
        self.advance(20, on_battery=False)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.engine.status()["state"], "online")

    def test_manual_cancel_survives_unknown_until_power_restored(self):
        self.engine.evaluate(self.sample(low_battery=True))
        self.engine.cancel()
        self.engine.evaluate(None)
        self.advance(30, low_battery=True)
        self.assertEqual(self.actions, [])
        self.assertTrue(self.engine.status()["suppressed"])
        self.engine.evaluate(self.sample(on_battery=False))
        self.engine.evaluate(self.sample(low_battery=True))
        self.advance(10, low_battery=True)
        self.assertEqual(self.actions, ["shutdown"])

    def test_disconnect_cancels_and_resets_continuity(self):
        self.engine.evaluate(self.sample(low_battery=True))
        self.advance(9)
        self.engine.evaluate(self.sample(connected=False))
        self.clock.step(1)
        self.engine.evaluate(self.sample())
        self.assertEqual(self.actions, [])
        self.assertEqual(self.engine.status()["on_battery_seconds"], 0)

    def test_stale_future_and_unknown_samples_never_act(self):
        for sample in [None, self.sample(on_battery=None), self.sample(on_battery="true"),
                       self.sample(timestamp=iso_utc(self.clock.utc() - timedelta(seconds=30))),
                       self.sample(timestamp=iso_utc(self.clock.utc() + timedelta(seconds=60)))]:
            self.engine.evaluate(sample)
            self.clock.step(40)
            self.engine.evaluate(sample)
        self.assertEqual(self.actions, [])

    def test_long_polling_gap_restarts_countdown(self):
        self.engine.evaluate(self.sample(low_battery=True))
        self.clock.step(20)
        self.engine.evaluate(self.sample(low_battery=True))
        self.assertEqual(self.actions, [])
        self.assertEqual(self.engine.status()["countdown_remaining"], 10)

    def test_first_battery_sample_starts_now(self):
        self.clock.step(10000)
        self.engine.evaluate(self.sample())
        self.assertEqual(self.engine.status()["on_battery_seconds"], 0)
        self.assertEqual(self.engine.status()["state"], "on_battery")

    def test_only_real_battery_evidence_allows_low_threshold(self):
        self.engine.evaluate(self.sample(on_battery=False, battery_percent=1, low_battery=True))
        self.advance(50, on_battery=False, battery_percent=1, low_battery=True)
        self.assertEqual(self.actions, [])

    def test_each_low_threshold_starts_countdown(self):
        for changed in [{"low_battery": True}, {"battery_percent": 20}, {"runtime_seconds": 180}]:
            self.engine.evaluate(self.sample(on_battery=False))
            self.assertEqual(self.engine.evaluate(self.sample(**changed))["state"], "countdown")

    def test_string_false_is_not_low_battery_and_invalid_numbers_ignored(self):
        self.assertEqual(self.engine.evaluate(self.sample(low_battery="false", battery_percent=False,
                                                         runtime_seconds="0"))["state"], "on_battery")

    def test_default_disabled_and_strict_validation(self):
        self.assertFalse(DEFAULT_POLICY["enabled"])
        for bad in [{"enabled": "false"}, {"on_battery_seconds": True}, {"action": "restart"},
                    {"countdown_seconds": 0}, {"extra": 1}, {"battery_percent": 2}]:
            with self.assertRaises(ValueError):
                validate_policy(bad)

    def test_disable_and_reenable_resets_manual_cancel(self):
        self.engine.evaluate(self.sample(low_battery=True))
        self.engine.cancel()
        self.engine.update_policy({"enabled": False})
        self.engine.update_policy({"enabled": True, "countdown_seconds": 10})
        self.engine.evaluate(self.sample(low_battery=True))
        self.advance(10, low_battery=True)
        self.assertEqual(self.actions, ["shutdown"])

    def test_transition_events_do_not_repeat(self):
        self.engine.evaluate(self.sample())
        self.advance(5)
        self.assertEqual(len(self.events), 1)

    def test_action_failure_does_not_retry_repeatedly(self):
        def failing(action):
            self.actions.append(action)
            raise RuntimeError("blocked")
        self.engine._action_callback = failing
        self.engine.evaluate(self.sample(low_battery=True))
        self.advance(30, low_battery=True)
        self.assertEqual(self.actions, ["shutdown"])
        self.assertEqual(self.engine.status()["state"], "action_failed")


class StoreTests(unittest.TestCase):
    def test_persistence_bounded_history_events_and_export(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "data.sqlite"
            store = Store(path)
            store.set_setting("policy", DEFAULT_POLICY)
            for i in range(20):
                store.add_sample({"timestamp": iso_utc(), "input_voltage": i})
            store.add_event("online", "Red disponible", {"severity": "info"})
            self.assertEqual(len(store.history(max_points=3)), 3)
            self.assertEqual(len(list(store.iter_samples())), 20)
            store.close()
            store = Store(path)
            self.assertEqual(store.get_setting("policy"), DEFAULT_POLICY)
            self.assertEqual(store.events()[0]["kind"], "online")
            store.close()

    def test_concurrent_writes(self):
        store = Store(":memory:")
        errors = []
        def writer():
            try:
                for _ in range(20):
                    store.add_sample({"timestamp": iso_utc(), "on_battery": False})
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(store.history()), 80)
        store.close()


if __name__ == "__main__":
    unittest.main()
