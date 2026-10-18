"""Observed-sequence tracker is memory-only and never estimates autonomy."""
import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from ups_monitor.ups_sequence import UpsSequenceTracker


class SequenceTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tick = 100.0
        self.now = datetime.now(timezone.utc)
        self.capability = {"available": True, "reserve_mode": "observed_sequence", "profile_id": "test-profile",
            "device_fingerprint": "a" * 64, "action": "hibernate", "sequence_validation": {
                "max_seconds_since_mains": 15, "max_load_percent": 11, "min_mains_battery_voltage": 27.41,
                "min_battery_voltage": 25.41, "max_countdown_seconds": 10}}
        self.policy = {"enabled": True, "on_battery_seconds": 0, "countdown_seconds": 10}
        self.tracker = UpsSequenceTracker(clock=lambda: self.tick)
        self.addCleanup(patch.stopall)
        date = patch("ups_monitor.ups_power.datetime", wraps=datetime).start()
        date.now.side_effect = lambda *_args: self.now

    def sample(self, battery=False, **changes):
        return {"timestamp": self.now.isoformat(), "on_battery": battery, "low_battery": False, "fault": False,
            "output_voltage": 220, "battery_voltage": 25.41 if battery else 27.41, "load_percent": 11,
            "raw_status": "Battery mode" if battery else "Line mode", "runtime_seconds": None, **changes}

    def advance(self, seconds):
        self.tick += seconds
        self.now += timedelta(seconds=seconds)

    def observe(self, battery=False, **changes):
        sample = self.sample(battery, **changes)
        self.tracker.observe(sample, self.capability)
        return sample

    def status(self, sample=None, policy=None, capability=None):
        return self.tracker.status(capability or self.capability, sample=sample, policy=self.policy if policy is None else policy)

    def test_mains_baseline_allows_configuration_without_fabricating_runtime(self):
        sample = self.observe()
        result = self.tracker.status(self.capability, sample=sample, policy=self.policy)
        self.assertTrue(result["available"])
        self.assertIsNone(result["evidence"]["battery_started_monotonic"])
        self.assertEqual(result["evidence"]["last_mains_sample"], sample)
        self.assertNotIn("reserve_seconds", result)
        self.assertNotIn("runtime_seconds", result["evidence"])
        result["evidence"]["last_mains_sample"]["load_percent"] = 99
        self.assertEqual(self.status()["evidence"]["last_mains_sample"]["load_percent"], 11)

    def test_opening_on_battery_cannot_borrow_an_unobserved_mains_baseline(self):
        sample = self.observe(True)
        self.assertFalse(self.status(sample)["available"])
        self.advance(2)
        self.assertFalse(self.status(self.observe(True))["available"])

    def test_absolute_window_runs_from_last_mains_and_not_first_battery_or_policy(self):
        self.observe()
        self.advance(2)
        self.observe(True)
        self.advance(12)
        self.assertTrue(self.status(self.observe(True))["available"])
        self.advance(1)
        self.assertTrue(self.status(self.observe(True))["available"])
        self.advance(0.001)
        result = self.status(self.observe(True))
        self.assertFalse(result["available"])
        self.assertIsNone(result["evidence"])

    def test_any_gap_or_suspend_requires_a_new_mains_sample(self):
        self.observe()
        self.advance(2)
        self.observe(True)
        self.tracker.invalidate()
        self.assertFalse(self.status(self.observe(True))["available"])
        self.observe()
        self.advance(16)
        self.assertFalse(self.status(self.observe(True))["available"])
        self.observe()
        self.advance(1)
        self.assertTrue(self.status(self.observe(True))["available"])

    def test_bad_battery_reading_cannot_be_forgotten_before_return_to_mains(self):
        bad_values = ({"load_percent": 11.01}, {"load_percent": True}, {"low_battery": True},
            {"fault": True}, {"on_battery": None}, {"raw_status": "Unknown"},
            {"battery_voltage": 25.40}, {"output_voltage": 0}, {"timestamp": "2000-01-01T00:00:00Z"})
        for changes in bad_values:
            self.tracker.invalidate()
            self.observe()
            self.advance(1)
            self.observe(True, **changes)
            self.advance(1)
            with self.subTest(changes=changes):
                self.assertFalse(self.status(self.observe(True))["available"])
                self.observe()
                self.advance(1)
                self.assertTrue(self.status(self.observe(True))["available"])

    def test_bad_mains_voltage_or_load_does_not_create_a_baseline(self):
        for changes in ({"battery_voltage": 27.40}, {"load_percent": 12}, {"low_battery": True}):
            self.tracker.invalidate()
            self.observe(False, **changes)
            self.advance(1)
            with self.subTest(changes=changes):
                self.assertFalse(self.status(self.observe(True))["available"])

    def test_policy_can_be_saved_on_mains_but_longer_battery_wait_cannot_arm_ups(self):
        self.observe()
        longer = {"on_battery_seconds": 60, "countdown_seconds": 30}
        self.assertFalse(self.status(policy=longer)["available"])
        self.advance(1)
        sample = self.observe(True)
        result = self.status(sample, longer)
        self.assertFalse(result["available"])
        self.assertIn("0 segundos", result["reason"])
        self.assertTrue(self.status(sample)["available"])
        self.advance(15)
        self.assertFalse(self.status(self.observe(True))["available"])

    def test_policy_fields_are_strict_and_absent_policy_cannot_arm_on_battery(self):
        self.observe()
        self.advance(1)
        sample = self.observe(True)
        for policy in ({}, {"on_battery_seconds": False, "countdown_seconds": 10},
                       {"on_battery_seconds": 0, "countdown_seconds": 30},
                       {"on_battery_seconds": 0, "countdown_seconds": True}):
            with self.subTest(policy=policy):
                self.assertFalse(self.status(sample, policy)["available"])
        self.assertFalse(self.tracker.status(self.capability, sample=sample)["available"])

    def test_profile_or_unit_change_cannot_inherit_the_old_baseline(self):
        self.observe()
        self.advance(1)
        sample = self.observe(True)
        other = {**self.capability, "device_fingerprint": "b" * 64}
        self.assertFalse(self.status(sample, capability=other)["available"])
        self.tracker.observe(sample, other)
        self.assertFalse(self.status(sample, capability=other)["available"])
        self.tracker.observe(self.sample(), other)
        self.advance(1)
        sample = self.sample(True)
        self.tracker.observe(sample, other)
        self.assertTrue(self.status(sample, capability=other)["available"])

    def test_envelope_cannot_exceed_observed_parameters_or_use_untyped_values(self):
        for changes in ({"max_seconds_since_mains": 16}, {"max_load_percent": 12},
                        {"min_battery_voltage": 25}, {"min_mains_battery_voltage": 27},
                        {"max_countdown_seconds": 11}, {"max_load_percent": True}):
            capability = copy.deepcopy(self.capability)
            capability["sequence_validation"].update(changes)
            self.tracker.observe(self.sample(), capability)
            with self.subTest(changes=changes):
                self.assertFalse(self.tracker.status(capability, sample=self.sample())["available"])

    def test_evidence_preserves_profile_binding_original_mains_and_policy(self):
        baseline = self.observe(False, load_percent=6)
        self.advance(2)
        self.observe(True, load_percent=10)
        self.advance(2)
        result = self.status(self.observe(True, load_percent=11))
        evidence = result["evidence"]
        self.assertEqual(evidence["profile_id"], self.capability["profile_id"])
        self.assertEqual(evidence["device_fingerprint"], self.capability["device_fingerprint"])
        self.assertEqual(evidence["last_mains_monotonic"], 100)
        self.assertEqual(evidence["battery_started_monotonic"], 102)
        self.assertEqual(evidence["max_load_percent"], 11)
        self.assertEqual(evidence["min_battery_voltage"], 25.41)
        self.assertEqual(evidence["last_mains_sample"], baseline)
        self.assertEqual((evidence["on_battery_seconds"], evidence["countdown_seconds"]), (0, 10))

    def test_status_detected_stale_or_bad_sample_invalidates_future_battery_use(self):
        self.observe()
        self.advance(1)
        sample = self.observe(True)
        self.assertFalse(self.status({**sample, "fault": True})["available"])
        self.assertFalse(self.status(self.observe(True))["available"])

    def test_delayed_delivery_does_not_extend_the_last_mains_deadline(self):
        delayed = self.sample(timestamp=(self.now - timedelta(seconds=8)).isoformat())
        self.tracker.observe(delayed, self.capability)
        self.assertEqual(self.status()["evidence"]["last_mains_monotonic"], 92)
        self.advance(2)
        self.assertTrue(self.status(self.observe(True))["available"])
        self.advance(6)
        self.assertFalse(self.status(self.observe(True))["available"])

    def test_future_timestamp_cannot_create_an_observed_baseline(self):
        future = self.sample(timestamp=(self.now + timedelta(seconds=1)).isoformat())
        self.tracker.observe(future, self.capability)
        self.advance(1)
        self.assertFalse(self.status(self.observe(True))["available"])

    def test_shutdown_action_and_missing_policy_cannot_claim_execution_readiness(self):
        self.observe()
        self.assertFalse(self.tracker.status(self.capability, sample=self.sample())["available"])
        wrong_action = {**self.capability, "action": "shutdown"}
        self.assertFalse(self.status(capability=wrong_action)["available"])

    def enable_user_configuration(self):
        self.capability["operation_configuration"] = {"source": "user_configurable", "min_delay_seconds": 60,
            "max_delay_seconds": 540, "max_countdown_seconds": 120, "poll_margin_seconds": 10,
            "requires_on_battery_seconds": 0}

    def test_user_default_15_seconds_has_its_own_operating_window_without_changing_observations(self):
        physical = copy.deepcopy(self.capability["sequence_validation"])
        self.enable_user_configuration()
        self.policy["countdown_seconds"] = 15
        self.assertTrue(self.status(self.observe())["available"])
        for _ in range(12):
            self.advance(2)
            self.assertTrue(self.status(self.observe(True))["available"])
        self.advance(1.001)
        self.assertFalse(self.status(self.observe(True))["available"])
        self.assertEqual(self.capability["sequence_validation"], physical)

    def test_120_second_operating_policy_keeps_live_bounds_and_does_not_create_runtime(self):
        self.enable_user_configuration()
        self.policy["countdown_seconds"] = 120
        self.observe()
        for _ in range(61):
            self.advance(2)
            self.assertTrue(self.status(self.observe(True))["available"])
        evidence = self.status()["evidence"]
        self.assertEqual(evidence["last_mains_monotonic"], 100)
        self.assertEqual(evidence["countdown_seconds"], 120)
        self.assertNotIn("reserve_seconds", evidence)
        self.advance(2)
        self.observe(True, low_battery=True)
        self.advance(2)
        self.assertFalse(self.status(self.observe(True))["available"])


if __name__ == "__main__":
    unittest.main()
