"""Reserve tracking with virtual clocks and synthetic readings; no power IO."""
import copy
from datetime import datetime, timezone
import unittest

from ups_monitor.ups_power import UpsPowerBlocked, UpsPowerCoordinator
from ups_monitor.ups_reserve import UpsReserveTracker
from tests.test_ups_power import DEVICE, validated_profile


class UpsReserveTrackerTests(unittest.TestCase):
    def setUp(self):
        self.time = 100.0
        self.tracker = UpsReserveTracker(clock=lambda: self.time)
        self.capability = {"profile_id": "test-profile", "action": "hibernate", "reserve_seconds": 600,
                           "device_fingerprint": "a" * 64,
                           "reserve_baseline": {"recharge_seconds": 20, "min_battery_voltage": 27},
                           "validated_max_load_percent": 25}

    def sample(self, *, battery=True, **changes):
        return {"timestamp": datetime.now(timezone.utc).isoformat(), "on_battery": battery,
                "low_battery": False, "fault": False, "raw_status": "Battery mode" if battery else "Line mode",
                "runtime_seconds": None, "battery_percent": 100, "battery_voltage": 27.5,
                "load_percent": 10, **changes}

    def observe(self, time, *, battery=True, **changes):
        self.time = time
        reading = self.sample(battery=battery, **changes)
        self.tracker.observe(reading, self.capability)
        return self.tracker.remaining(self.capability, reading)

    def charge(self):
        for time in (100, 110, 120):
            result = self.observe(time, battery=False)
            if time < 120:
                self.assertIsNone(result)
            else:
                self.assertEqual(result, 600)

    def test_starting_in_battery_without_a_baseline_never_grants_free_reserve(self):
        self.assertIsNone(self.observe(100))
        self.assertIsNone(self.observe(110))
        self.assertIsNone(self.tracker.remaining(self.capability, self.sample(battery_percent=100)))

    def test_requires_complete_continuous_recharge_then_debits_transition_and_elapsed_time(self):
        self.charge()
        self.assertEqual(self.observe(130), 590)
        self.time = 135
        self.assertEqual(self.tracker.remaining(self.capability), 585)
        self.assertEqual(self.observe(140), 580, "Reading reserve must not debit it twice")
        self.assertEqual(self.observe(150), 570, "Policy resets do not exist in this independent tracker")

    def test_brief_mains_return_does_not_replenish_spent_reserve(self):
        self.charge()
        self.assertEqual(self.observe(130), 590)
        self.assertEqual(self.observe(140, battery=False), 580)
        self.assertEqual(self.observe(150, battery=False), 580)
        self.assertEqual(self.observe(160), 570)

    def test_only_a_new_complete_baseline_can_replenish_reserve(self):
        self.charge()
        self.observe(130)
        for time in (140, 150, 160):
            self.observe(time, battery=False)
        self.assertEqual(self.observe(170), 590)

    def test_gap_sleep_explicit_invalidation_and_backwards_clock_lose_baseline(self):
        for kind in ("gap", "invalidate", "backwards"):
            with self.subTest(kind=kind):
                self.setUp()
                self.charge()
                self.observe(130)
                if kind == "invalidate":
                    self.tracker.invalidate()
                    self.assertIsNone(self.observe(140))
                else:
                    self.assertIsNone(self.observe(146 if kind == "gap" else 129))
                self.assertIsNone(self.observe(150))

    def test_unknown_or_excessive_load_low_battery_fault_and_bad_modes_invalidate(self):
        for changes in ({"load_percent": None}, {"load_percent": 26}, {"load_percent": True},
                        {"low_battery": True}, {"fault": True}, {"connected": False},
                        {"raw_status": "Shutdown mode"}, {"timestamp": "2000-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.charge()
                self.observe(130, **changes)
                self.assertIsNone(self.observe(140))

    def test_voltage_below_baseline_or_missing_load_prevents_recharge(self):
        for changes in ({"battery_voltage": 26.9}, {"battery_voltage": None}, {"load_percent": None}):
            with self.subTest(changes=changes):
                self.setUp()
                for time in (100, 110, 120, 130):
                    self.observe(time, battery=False, **changes)
                self.assertIsNone(self.observe(140))

    def test_missing_configuration_cannot_use_percent_or_previous_reserve(self):
        self.capability.pop("reserve_baseline")
        for time in (100, 110, 120):
            self.observe(time, battery=False)
        self.assertIsNone(self.observe(130))
        self.capability["reserve_baseline"] = {"recharge_seconds": 20, "min_battery_voltage": 27}
        self.capability.pop("validated_max_load_percent")
        self.assertIsNone(self.observe(140))

    def test_missing_unit_binding_cannot_build_a_measured_baseline(self):
        self.capability.pop("device_fingerprint")
        for time in (100, 110, 120):
            self.assertIsNone(self.observe(time, battery=False))
        self.assertIsNone(self.observe(130))

    def test_real_fresh_runtime_is_independent_and_capped_by_measured_reserve(self):
        self.capability.pop("reserve_baseline")
        self.assertIsNone(self.tracker.remaining(self.capability, self.sample(battery=False, runtime_seconds=600)))
        for runtime, expected in ((700, 600), (200.5, 200.5), (0, 0)):
            with self.subTest(runtime=runtime):
                reading = self.sample(runtime_seconds=runtime)
                self.tracker.invalidate()
                self.assertEqual(self.tracker.remaining(self.capability, reading), expected)
        for runtime in (None, True, -1, float("nan"), float("inf"), "600"):
            with self.subTest(runtime=runtime):
                self.assertIsNone(self.tracker.remaining(self.capability, self.sample(runtime_seconds=runtime)))
        for changes in ({"low_battery": True}, {"fault": True}, {"load_percent": 26},
                        {"timestamp": "2000-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.tracker.remaining(self.capability, self.sample(runtime_seconds=600, **changes)))

    def test_changed_profile_conditions_or_stale_tracker_cannot_adopt_old_budget(self):
        self.charge()
        self.observe(130)
        changed = copy.deepcopy(self.capability)
        changed["reserve_seconds"] = 900
        self.assertIsNone(self.tracker.remaining(changed, self.sample()))
        self.time = 146
        self.assertIsNone(self.tracker.remaining(self.capability, self.sample()))

    def test_charged_online_baseline_is_available_but_low_online_voltage_invalidates_it(self):
        self.charge()
        self.assertEqual(self.tracker.remaining(self.capability), 600)
        self.observe(130, battery=False, battery_voltage=26)
        self.assertIsNone(self.observe(140))

    def test_another_unit_cannot_inherit_the_same_profile_name_baseline(self):
        self.charge()
        another = copy.deepcopy(self.capability)
        another["device_fingerprint"] = "b" * 64
        self.assertIsNone(self.tracker.remaining(another, self.sample()))
        self.tracker.observe(self.sample(), another)
        self.assertIsNone(self.tracker.remaining(another))

    def test_profile_baseline_configuration_is_validated_and_exported_without_mutation(self):
        profile = validated_profile()
        validation = profile["action_validation"]["hibernate"]
        validation.update(reserve_baseline={"recharge_seconds": 3600, "min_battery_voltage": 27}, validated_max_load_percent=25)
        coordinator = UpsPowerCoordinator(validated_profiles={profile["profile_id"]: profile})
        capability = coordinator.capabilities(DEVICE, "hibernate")
        self.assertTrue(capability["available"])
        self.assertEqual(capability["device_fingerprint"], profile["device_fingerprint"])
        self.assertEqual(capability["reserve_baseline"], validation["reserve_baseline"])
        capability["reserve_baseline"]["recharge_seconds"] = 1
        self.assertEqual(coordinator.capabilities(DEVICE, "hibernate")["reserve_baseline"]["recharge_seconds"], 3600)
        for changes in ({"recharge_seconds": 0, "min_battery_voltage": 27},
                        {"recharge_seconds": True, "min_battery_voltage": 27},
                        {"recharge_seconds": 3600, "min_battery_voltage": float("nan")},
                        {"recharge_seconds": 3600}):
            invalid = copy.deepcopy(profile)
            invalid["action_validation"]["hibernate"]["reserve_baseline"] = changes
            self.assertFalse(UpsPowerCoordinator(validated_profiles={invalid["profile_id"]: invalid}).capabilities(DEVICE, "hibernate")["available"])
        invalid = copy.deepcopy(profile)
        invalid["action_validation"]["hibernate"].pop("validated_max_load_percent")
        self.assertFalse(UpsPowerCoordinator(validated_profiles={invalid["profile_id"]: invalid}).capabilities(DEVICE, "hibernate")["available"])


if __name__ == "__main__":
    unittest.main()
