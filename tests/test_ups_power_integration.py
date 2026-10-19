"""Monitor integration with fake USB and fake Windows actions only."""
import copy
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc, validate_policy
from ups_monitor.server import Handler, Monitor, WindowsActionUnconfirmed
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerCoordinator, device_fingerprint


DEVICE = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#isolated-test"}


def sample(*, after=None, **changes):
    # An observed post-submission sample needs an explicitly later timestamp;
    # adjacent UTC reads can share one clock tick on Windows runners.
    timestamp = ((datetime.fromisoformat(after.replace("Z", "+00:00")) + timedelta(milliseconds=1))
                 .isoformat().replace("+00:00", "Z") if after is not None else iso_utc())
    return {"timestamp": timestamp, "connected": True, "on_battery": True, "low_battery": False,
            "fault": False, "raw_status": "Battery mode", "output_voltage": 220,
            "raw_flags": {"status_hex": "89"}, "runtime_seconds": 600, **changes}


def profile():
    return {**NT2402PRO_CANDIDATE_PROFILE, "device_fingerprint": device_fingerprint(DEVICE),
            "manual_restart_verified": True, "cancel_verified": True, "low_battery_cutoff_behavior": "immediate",
            "action_validation": {action: {"completion_seconds": [30, 40, 50], "reserve_seconds": 600}
                                  for action in ("shutdown", "hibernate")}}


class UpsPowerIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Keep every ordinary clock read on the same tick so these tests also
        # exercise fast/coarse-clock CI runners without relying on sleeps.
        self.utc_tick = iso_utc()
        for target in (__name__ + ".iso_utc", "ups_monitor.ups_power._stamp"):
            clock_patch = patch(target, return_value=self.utc_tick)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store = Store(":memory:")
        self.transport = Mock()
        self.transport.submit_shutdown.return_value = {"state": "submitted"}
        self.transport.cancel_shutdown.return_value = {"state": "submitted"}
        self.factory = Mock(return_value=self.transport)
        self.coordinator = UpsPowerCoordinator(Path(self.directory.name) / "operation.json",
            validated_profiles={profile()["profile_id"]: profile()}, transport_factory=self.factory)
        self.adapter = SimpleNamespace(source="native-test", bridge_required=False, device_identity=DEVICE, read=Mock())
        self.action = Mock()
        self.monitor = Monitor(self.store, self.adapter, allow_ups_poweroff=True,
                               ups_power=self.coordinator, action_callback=self.action)
        self.monitor.sample = sample()
        self.monitor.connection = "connected"

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_legacy_defaults_and_boolean_validation(self):
        self.assertIs(validate_policy({})["ups_poweroff"], False)
        for value in ("true", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy({"ups_poweroff": value})

    def test_three_independent_gates_reject_without_changing_saved_policy(self):
        for gate in ("authorization", "profile", "native", "identity"):
            with self.subTest(gate=gate):
                monitor = Monitor(self.store, self.adapter, action_callback=self.action,
                    allow_ups_poweroff=gate != "authorization", ups_power=self.coordinator if gate != "profile" else None)
                monitor.sample, monitor.connection = sample(), "connected"
                if gate == "native":
                    monitor.adapter = SimpleNamespace(source="bridge", bridge_required=True, device_identity=DEVICE)
                if gate == "identity":
                    monitor.adapter = SimpleNamespace(source="native", bridge_required=False, device_identity=None)
                before = copy.deepcopy(monitor.engine.policy)
                with self.assertRaises(ValueError):
                    monitor.set_policy({"enabled": True, "ups_poweroff": True})
                self.assertEqual(monitor.engine.policy, before)
                self.assertIsNone(self.store.get_setting("policy"))
        self.transport.submit_shutdown.assert_not_called()
        self.action.assert_not_called()

    def test_arm_is_once_before_windows_and_never_claims_windows_completed(self):
        order = []
        self.transport.submit_shutdown.side_effect = lambda delay: order.append(("ups", delay)) or {"state": "submitted"}
        self.action.side_effect = lambda action: order.append(("windows", action))
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True, "countdown_seconds": 10})
        now = [0]
        self.monitor.engine._clock = lambda: now[0]
        self.monitor.engine.evaluate(sample(low_battery=False))
        self.monitor.engine.evaluate(sample(low_battery=True))
        self.monitor.sample = sample()
        now[0] = 10
        self.monitor.engine.evaluate(sample())
        self.monitor.engine.evaluate(sample())
        self.assertEqual(order, [("ups", 180), ("windows", "shutdown")])
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "requested")
        self.assertEqual(self.coordinator.status()["state"], "submitted")
        self.assertEqual(self.coordinator.status()["confirmation"], "unconfirmed")

    def test_cancel_before_countdown_finishes_has_no_hardware_writes(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor.engine.evaluate(sample(low_battery=True))
        self.monitor.cancel()
        self.monitor.engine.evaluate(sample())
        self.factory.assert_not_called()
        self.action.assert_not_called()

    def test_revoked_ups_capability_cannot_prevent_disabling_windows_protection(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor.allow_ups_poweroff = False
        self.monitor.connection = "disconnected"
        result = self.monitor.set_policy({"enabled": False, "ups_poweroff": True})
        self.assertFalse(result["policy"]["enabled"])
        self.assertFalse(self.store.get_setting("policy")["enabled"])
        with self.assertRaises(ValueError):
            self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.factory.assert_not_called()
        self.action.assert_not_called()

    def test_low_battery_degraded_capability_keeps_windows_action(self):
        self.monitor.engine.update_policy({"enabled": True, "ups_poweroff": True})
        self.monitor.sample = sample(low_battery=True)
        self.monitor._act("shutdown")
        self.action.assert_called_once_with("shutdown")
        self.factory.assert_not_called()

    def test_missing_remaining_runtime_never_uses_the_resettable_policy_clock(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor.sample = sample(runtime_seconds=None)
        self.monitor.engine._clock = lambda: 500.1
        self.monitor.engine._battery_since = 500.1
        self.monitor._act("hibernate")
        self.transport.submit_shutdown.assert_not_called()
        self.action.assert_called_once_with("hibernate")
        self.assertEqual(self.coordinator.status()["state"], "idle")

    def test_insufficient_hardware_runtime_keeps_windows_protection_only(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor.sample = sample(runtime_seconds=100)
        self.monitor._act("hibernate")
        self.transport.submit_shutdown.assert_not_called()
        self.action.assert_called_once_with("hibernate")

    def test_suspend_prevents_replay_of_owned_production_operation(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor._act("hibernate")
        self.monitor.handle_power_event("suspend")
        self.monitor.snapshot()
        self.assertEqual(self.coordinator.status()["state"], "review_required")
        self.monitor.handle_power_event("resume")
        self.monitor._process_sample(sample(on_battery=False, raw_status="Line mode"))
        self.transport.submit_shutdown.assert_called_once()
        self.transport.cancel_shutdown.assert_not_called()
        self.action.assert_called_once()

    def test_unvalidated_or_bridge_never_control_ups_but_keep_os(self):
        for adapter in (self.adapter, SimpleNamespace(source="bridge", bridge_required=True)):
            monitor = Monitor(self.store, adapter, allow_ups_poweroff=True, action_callback=self.action)
            monitor.sample, monitor.connection = sample(), "connected"
            monitor.engine.update_policy({"enabled": True, "ups_poweroff": True})
            monitor._act("hibernate")
        self.assertEqual(self.action.call_count, 2)
        self.factory.assert_not_called()

    def test_failure_to_query_ups_capability_never_blocks_windows_protection(self):
        self.monitor.engine.update_policy({"enabled": True, "ups_poweroff": True})
        with patch.object(self.coordinator, "capabilities", side_effect=OSError("profile unavailable")):
            result = self.monitor.snapshot()
            self.assertFalse(result["ups_poweroff"]["available"])
            self.monitor._act("hibernate")
        self.action.assert_called_once_with("hibernate")
        self.factory.assert_not_called()

    def test_uncertain_usb_is_not_retried_and_windows_is_still_requested(self):
        self.transport.submit_shutdown.side_effect = TimeoutError("partial report")
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor._act("shutdown")
        self.assertEqual(self.coordinator.status()["state"], "uncertain")
        self.action.assert_called_once_with("shutdown")
        self.transport.submit_shutdown.assert_called_once_with(180)

    def test_windows_rejection_cancels_only_after_correlated_fresh_shutdown(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.action.side_effect = RuntimeError("Windows rejected")
        self.adapter.read.side_effect = lambda: sample(after=self.coordinator.status()["submitted_at"],
            on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"})
        with self.assertRaises(RuntimeError):
            self.monitor._act("shutdown")
        self.transport.cancel_shutdown.assert_called_once_with()
        self.assertEqual(self.coordinator.status()["state"], "cancel_submitted")
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "failed")

    def test_windows_rejection_without_shutdown_evidence_never_cancels(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.action.side_effect = RuntimeError("Windows rejected")
        self.adapter.read.return_value = sample()
        with self.assertRaises(RuntimeError):
            self.monitor._act("shutdown")
        self.transport.cancel_shutdown.assert_not_called()
        self.assertEqual(self.coordinator.status()["state"], "submitted")

    def test_windows_timeout_stays_unconfirmed_and_uses_only_safe_owned_emergency_cancel(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.action.side_effect = WindowsActionUnconfirmed("No se confirmó la solicitud")
        self.adapter.read.side_effect = lambda: sample(after=self.coordinator.status()["submitted_at"],
            on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"})
        with self.assertRaises(WindowsActionUnconfirmed):
            self.monitor._act("hibernate")
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "unconfirmed")
        self.transport.cancel_shutdown.assert_called_once_with()
        self.transport.submit_shutdown.assert_called_once_with(180)
        self.action.assert_called_once_with("hibernate")
        self.assertIn("windows_action_unconfirmed", [event["kind"] for event in self.store.events()])

    def test_windows_timeout_never_cancels_with_shutdown_evidence_at_submission_timestamp(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.action.side_effect = WindowsActionUnconfirmed("No se confirmó la solicitud")
        self.adapter.read.side_effect = lambda: sample(timestamp=self.coordinator.status()["submitted_at"],
            on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"})
        with self.assertRaises(WindowsActionUnconfirmed):
            self.monitor._act("hibernate")
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "unconfirmed")
        self.assertEqual(self.coordinator.status()["state"], "submitted")
        self.transport.cancel_shutdown.assert_not_called()
        self.transport.submit_shutdown.assert_called_once_with(180)
        self.action.assert_called_once_with("hibernate")

    def test_windows_timeout_after_suspend_resume_never_sends_cancel_or_repeats_request(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})

        def crossed_transition(action):
            self.monitor.handle_power_event("suspend")
            self.monitor.handle_power_event("resume")
            raise WindowsActionUnconfirmed("La espera atravesó S4")

        self.action.side_effect = crossed_transition
        self.monitor._act("hibernate")
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "unconfirmed")
        self.assertEqual(self.monitor.system_power_status()["generation"], 2)
        self.transport.cancel_shutdown.assert_not_called()
        self.transport.submit_shutdown.assert_called_once_with(180)
        self.action.assert_called_once_with("hibernate")
        with self.assertRaises(RuntimeError):
            self.monitor._act("hibernate")
        self.action.assert_called_once()

    def test_late_process_success_after_suspend_does_not_claim_an_accepted_new_request(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.action.side_effect = lambda _action: self.monitor.handle_power_event("suspend")
        self.monitor._act("hibernate")
        self.assertEqual(self.monitor.ups_power_status()["windows_request"], "unconfirmed")
        self.transport.cancel_shutdown.assert_not_called()
        self.action.assert_called_once_with("hibernate")

    def test_expected_owned_shutdown_does_not_turn_confirmed_os_request_into_failure(self):
        self.monitor.set_policy({"enabled": True, "ups_poweroff": True})
        self.monitor._act("shutdown")
        previous = self.monitor.engine.status()["state"]
        self.monitor._process_sample(sample(after=self.coordinator.status()["submitted_at"],
            on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"}))
        self.assertEqual(self.monitor.engine.status()["state"], previous)
        self.assertEqual(self.coordinator.status()["state"], "armed")
        self.action.assert_called_once()

    def test_simulated_complete_sequence_and_cancel_are_never_real_actions(self):
        self.monitor.set_policy({"enabled": False, "ups_poweroff": True})
        self.monitor.set_policy({"enabled": False, "countdown_seconds": 30,
            "ups_poweroff": True, "ups_poweroff_delay_seconds": 240})
        with patch("ups_monitor.server.time.monotonic", return_value=100):
            first = self.monitor.test(include_ups=True)
        self.assertEqual(first["protection"]["countdown_remaining"], 30)
        self.assertEqual(first["protection"]["phase"], "windows_countdown")
        with patch("ups_monitor.server.time.monotonic", return_value=130):
            second = self.monitor.snapshot()
        self.assertEqual(second["protection"]["phase"], "ups_delay")
        self.assertEqual(second["protection"]["countdown_remaining"], 240)
        self.assertFalse(second["protection"]["cancelable"])
        self.assertTrue(second["ups_poweroff"]["simulation"]["windows_requested"])
        with patch("ups_monitor.server.time.monotonic", return_value=370):
            finished = self.monitor.snapshot()
        self.assertEqual(finished["protection"]["phase"], "complete")
        self.assertTrue(finished["ups_poweroff"]["simulation"]["ups_cut_simulated"])
        self.action.assert_not_called()
        self.factory.assert_not_called()
        with patch("ups_monitor.server.time.monotonic", return_value=400):
            self.monitor.test(include_ups=True)
        with patch("ups_monitor.server.time.monotonic", return_value=430):
            self.monitor.snapshot()
            with self.assertRaises(ValueError):
                self.monitor.cancel()
        with patch("ups_monitor.server.time.monotonic", return_value=500):
            restarted = self.monitor.test(include_ups=True)
        self.assertTrue(restarted["protection"]["cancelable"])
        with patch("ups_monitor.server.time.monotonic", return_value=510):
            cancelled = self.monitor.cancel()
        self.assertEqual(cancelled["ups_poweroff"]["simulation"]["phase"], "cancelled")
        self.assertEqual(cancelled["protection"]["state"], "idle")
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
