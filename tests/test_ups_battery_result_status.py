"""Closed physical observations govern this unit's requested manual restart mode."""
from types import SimpleNamespace
import unittest
import uuid

from ups_monitor.core import Store, iso_utc
from ups_monitor.server import Monitor
from ups_monitor.ups_battery_test import UpsBatteryTest
from ups_monitor.ups_cancel_test import candidate_profile
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, VALIDATED_PROFILES


class PhysicalResultStatusTests(unittest.TestCase):
    def monitor(self):
        device = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#observed-result"}
        test = UpsBatteryTest()
        test._operation = {"state": "finished", "operation_id": str(uuid.uuid4()), "profile": candidate_profile(device),
            "result": "automatic_return_observed", "windows_request": "failed", "windows_error": "old command timeout",
            "physical_observation": {"source": "user_physical_observation", "recorded_at": iso_utc(),
                "cut_observed": True, "ac_restored": True, "automatic_return_observed": True,
                "manual_restart": False, "hibernate_resumed": True, "off_after_ac_seconds": 0}}
        store = Store(":memory:")
        self.addCleanup(store.close)
        monitor = Monitor(store, SimpleNamespace(source="fake-native", bridge_required=False, device_identity=device),
                          ups_battery_test=test)
        monitor.connection = "connected"
        monitor.sample = {"timestamp": iso_utc(), "on_battery": False, "low_battery": False,
            "fault": False, "raw_status": "Line mode", "raw_flags": {"status_hex": "09"}, "output_voltage": 220}
        return monitor

    def test_same_unit_records_accepted_automatic_return_without_promoting_a_profile(self):
        monitor = self.monitor()
        before = dict(VALIDATED_PROFILES)
        snapshot = monitor.snapshot()
        result = snapshot["ups_poweroff"]
        self.assertEqual(result["validation"], "pending")
        self.assertEqual(result["reason_code"], "profile_unvalidated")
        self.assertEqual(result["restart_behavior"], "automatic_on_mains")
        self.assertTrue(result["automatic_restart_accepted"])
        self.assertTrue(result["restart_behavior_observed"])
        self.assertFalse(result["available"])
        self.assertFalse(result["manual_restart"])
        self.assertFalse(result["manual_restart_verified"])
        self.assertNotIn("incompatible", result["reason"])
        self.assertFalse(snapshot["policy"]["ups_poweroff"])
        self.assertEqual(VALIDATED_PROFILES, before)
        physical = snapshot["ups_battery_test"]
        self.assertEqual(physical["state"], "finished")
        self.assertNotIn("podría seguir pendiente", physical["message"])
        self.assertTrue(physical["physical_observation"]["hibernate_resumed"])

    def test_observation_is_not_transferred_to_another_usb_unit(self):
        monitor = self.monitor()
        monitor.adapter.device_identity = {**monitor.adapter.device_identity, "path": monitor.adapter.device_identity["path"] + "-other"}
        result = monitor.ups_power_status()
        self.assertEqual(result["validation"], "pending")
        self.assertNotEqual(result["reason_code"], "automatic_output_return_observed")
        self.assertNotIn("automatic_restart_accepted", result)


if __name__ == "__main__":
    unittest.main()
