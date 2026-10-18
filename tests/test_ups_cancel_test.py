"""Physical-test coordinator: temporary permission and fake hardware only."""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from ups_monitor.ups_cancel_test import (AUTH_FILENAME, JOURNAL_FILENAME, UpsCancellationTest,
                                        candidate_profile, validate_test_worker_request, validate_cancel_test_preflight)
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, VALIDATED_PROFILES, UpsPowerBlocked


class UpsCancellationTestTests(unittest.TestCase):
    def setUp(self):
        self.production_profiles_before = copy.deepcopy(VALIDATED_PROFILES)
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / JOURNAL_FILENAME
        self.auth_path = self.path.with_name(AUTH_FILENAME)
        self.time = datetime.now(timezone.utc)
        self.tick = 100.0
        self.auth = {"schema_version": 1, "owner": "elipro-ups-cancel-test", "authorization_id": uuid.uuid4().hex,
                     "issued_at": (self.time - timedelta(seconds=1)).isoformat(),
                     "expires_at": (self.time + timedelta(seconds=899)).isoformat()}
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")
        self.device = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#fake-test"}
        self.transport = Mock()
        self.transport.submit_test.return_value = {"state": "submitted"}
        self.transport.cancel_test.return_value = {"state": "submitted"}
        self.factory = Mock(return_value=self.transport)
        self.test = UpsCancellationTest(self.path, transport_factory=self.factory)
        self.addCleanup(patch.stopall)
        patch("ups_monitor.ups_cancel_test._now", side_effect=lambda: self.time).start()
        patch("ups_monitor.ups_cancel_test.time.monotonic", side_effect=lambda: self.tick).start()
        patch("ups_monitor.ups_test_authorization._now_utc", side_effect=lambda *_args: self.time).start()
        date = patch("ups_monitor.ups_power.datetime", wraps=datetime).start()
        date.now.side_effect = lambda *_args: self.time

    def sample(self, **changes):
        return {"timestamp": self.time.isoformat(), "on_battery": False, "low_battery": False, "fault": False,
                "output_voltage": 220, "raw_status": "Line mode", "raw_flags": {"status_hex": "09"}, **changes}

    def pending_sample(self):
        return self.sample(on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "0B"})

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)
        self.tick += seconds

    def start(self, **changes):
        return self.test.start(device=changes.pop("device", self.device), sample=changes.pop("sample", self.sample()),
                               policy_enabled=changes.pop("policy_enabled", False), blocked=changes.pop("blocked", False),
                               acknowledge_risk=changes.pop("acknowledge_risk", True), **changes)

    def observe_pending(self):
        self.advance(1)
        self.test.observe(device=self.device, sample=self.pending_sample())

    def test_no_permission_or_no_ack_never_calls_hardware(self):
        with self.assertRaises(UpsPowerBlocked):
            self.start(acknowledge_risk=False)
        self.auth_path.unlink()
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.factory.assert_not_called()

    def test_durable_record_exists_before_first_and_only_send(self):
        def sent():
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved["state"], "arming")
            self.assertEqual(saved["delay_seconds"], 180)
            profile = validate_test_worker_request(self.auth_path, self.path, saved["operation_id"], "start")
            self.assertEqual(profile, candidate_profile(self.device))
            return {"state": "submitted"}

        self.transport.submit_test.side_effect = sent
        result = self.start()
        self.assertEqual(result["state"], "submitted")
        self.assertFalse(result["can_cancel"])
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.transport.submit_test.assert_called_once()
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_only_recent_own_pending_qs_allows_one_cancel_and_later_observation(self):
        self.start()
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample=self.sample())
        self.observe_pending()
        self.assertEqual(self.test.status(device=self.device, sample=self.pending_sample())["state"], "armed")
        result = self.test.cancel(device=self.device, sample=self.pending_sample())
        self.assertEqual(result["state"], "cancel_submitted")
        self.transport.cancel_test.assert_called_once()
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample=self.pending_sample())
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertEqual(self.test.status()["state"], "observing")
        self.advance(209)
        self.test.observe(device=self.device, sample=self.sample())
        final = self.test.status(device=self.device, sample=self.sample())
        self.assertEqual(final["state"], "finished")
        self.assertFalse(final["production_validated"])
        self.assertFalse(final["available"], "One temporary permission cannot be reused")
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_partial_send_is_uncertain_and_never_retried(self):
        self.transport.submit_test.side_effect = OSError("partial write")
        self.assertEqual(self.start()["state"], "uncertain")
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.transport.submit_test.assert_called_once()
        self.transport.cancel_test.assert_not_called()

    def test_uncertain_submit_can_only_cancel_after_fresh_pending_bit(self):
        self.transport.submit_test.side_effect = OSError("partial write")
        self.start()
        self.observe_pending()
        self.test.cancel(device=self.device, sample=self.pending_sample())
        self.transport.cancel_test.assert_called_once()

    def test_cancel_failure_is_not_automatically_retried_or_claimed_cancelled(self):
        self.start()
        self.observe_pending()
        self.transport.cancel_test.side_effect = OSError("partial cancel")
        self.assertEqual(self.test.cancel(device=self.device, sample=self.pending_sample())["state"], "uncertain")
        self.assertFalse(self.test.status(device=self.device, sample=self.pending_sample())["can_cancel"])
        self.test.observe(device=self.device, sample=self.pending_sample())
        self.transport.cancel_test.assert_called_once()

    def test_expired_authorization_cannot_start(self):
        self.advance(900)
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.factory.assert_not_called()

    def test_policy_battery_low_failure_and_wrong_unit_block_start(self):
        for changes in ({"policy_enabled": True}, {"blocked": True},
                        {"sample": self.sample(on_battery=True)}, {"sample": self.sample(low_battery=True)},
                        {"sample": self.sample(fault=True)}, {"sample": self.pending_sample()},
                        {"device": {**self.device, "version_number": 4}}):
            with self.subTest(changes=changes), self.assertRaises(UpsPowerBlocked):
                self.start(**changes)
        self.factory.assert_not_called()

    def test_cancellation_rejects_old_reading_different_unit_late_or_low_battery(self):
        self.start()
        old = self.pending_sample()
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample=old)
        self.observe_pending()
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device={**self.device, "path": self.device["path"] + "other"}, sample=self.pending_sample())
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample={**self.pending_sample(), "low_battery": True})
        self.advance(170)
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample=self.pending_sample())
        self.transport.cancel_test.assert_not_called()

    def test_restart_never_replays_and_needs_review(self):
        self.start()
        restarted = UpsCancellationTest(self.path, transport_factory=self.factory)
        self.assertEqual(restarted.status()["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            restarted.start(device=self.device, sample=self.sample(), policy_enabled=False, blocked=False, acknowledge_risk=True)
        self.transport.submit_test.assert_called_once()

    def test_suspend_or_communication_loss_cannot_finish_the_observation(self):
        self.start()
        self.observe_pending()
        self.test.cancel(device=self.device, sample=self.pending_sample())
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        self.test.interrupted()
        self.advance(220)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertEqual(self.test.status()["state"], "review_required")

    def test_worker_rejects_replay_altered_operation_and_arbitrary_selector(self):
        result = self.start()
        for operation_id, selector in ((result["operation_id"], "start"), (str(uuid.uuid4()), "cancel"),
                                       (result["operation_id"], "load.on")):
            with self.subTest(selector=selector), self.assertRaises(UpsPowerBlocked):
                validate_test_worker_request(self.auth_path, self.path, operation_id, selector)

    def test_wall_clock_jump_cannot_finish_observation_early_or_extend_cancellation(self):
        self.start()
        self.observe_pending()
        self.test.cancel(device=self.device, sample=self.pending_sample())
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        self.time += timedelta(hours=2)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertEqual(self.test.status()["state"], "observing")
        self.assertEqual(self.test.status()["remaining"], 208)

    def close_first_cut(self):
        result = self.start()
        self.advance(211)
        self.test.interrupted()
        return self.test.close_observed_cut(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                           output_cut_observed=True, pending_signal_not_reported=True, manual_start_observed=True)

    def renew_permission(self):
        self.auth["authorization_id"] = uuid.uuid4().hex
        self.auth["issued_at"] = (self.time - timedelta(seconds=1)).isoformat()
        self.auth["expires_at"] = (self.time + timedelta(seconds=899)).isoformat()
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")

    def test_no_pending_signal_becomes_explicit_uncertainty_after_ten_seconds(self):
        self.start()
        self.advance(10)
        self.test.observe(device=self.device, sample=self.sample())
        status = self.test.status(device=self.device, sample=self.sample())
        self.assertEqual(status["state"], "uncertain")
        self.assertEqual(status["result"], "pending_bit_not_reported")
        self.assertIn("Hiberna Windows ahora", status["message"])
        self.assertFalse(status["can_cancel"])
        self.transport.cancel_test.assert_not_called()

    def test_review_close_is_metadata_only_and_does_not_claim_cancelled(self):
        final = self.close_first_cut()
        self.assertEqual(final["state"], "finished")
        self.assertEqual(final["result"], "cut_observed_cancel_not_tested")
        self.assertFalse(final["production_validated"])
        self.transport.submit_test.assert_called_once()
        self.transport.cancel_test.assert_not_called()
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_close_refuses_early_or_incomplete_physical_observation(self):
        result = self.start()
        for manual in (True, False):
            with self.assertRaises(UpsPowerBlocked):
                self.test.close_observed_cut(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                            output_cut_observed=True, pending_signal_not_reported=True, manual_start_observed=manual)
        self.advance(211)
        with self.assertRaises(UpsPowerBlocked):
            self.test.close_observed_cut(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                        output_cut_observed=True, pending_signal_not_reported=True, manual_start_observed=False)

    def test_known_unit_without_signal_can_cancel_only_its_new_complete_submission(self):
        self.close_first_cut()
        self.renew_permission()
        result = self.start()
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        status = self.test.status(device=self.device, sample=self.sample())
        self.assertEqual(status["state"], "armed")
        self.assertTrue(status["pending_signal_unreliable"])
        self.assertTrue(status["can_cancel"])

        def cancel_preflight():
            validate_test_worker_request(self.auth_path, self.path, result["operation_id"], "cancel")
            validate_cancel_test_preflight(self.auth_path, self.path, result["operation_id"], self.sample())
            return {"state": "submitted"}

        self.transport.cancel_test.side_effect = cancel_preflight
        self.test.cancel(device=self.device, sample=self.sample())
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertEqual(self.test.status()["state"], "observing")
        self.assertIn("no permite confirmarla", self.test.status()["message"])
        self.advance(209)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertEqual(self.test.status()["result"], "cancel_observed_after_deadline")
        self.transport.cancel_test.assert_called_once()

    def test_unreported_signal_observation_does_not_apply_to_another_unit(self):
        self.close_first_cut()
        self.renew_permission()
        self.device = {**self.device, "path": self.device["path"] + "other"}
        self.start()
        self.advance(1)
        self.test.observe(device=self.device, sample=self.sample())
        self.assertFalse(self.test.status(device=self.device, sample=self.sample())["can_cancel"])

    def test_known_signal_quirk_never_admits_partial_send_or_output_off(self):
        self.close_first_cut()
        self.renew_permission()
        self.transport.submit_test.side_effect = OSError("partial")
        self.start()
        self.advance(1)
        self.assertFalse(self.test.status(device=self.device, sample=self.sample())["can_cancel"])
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel(device=self.device, sample=self.sample(output_voltage=0))
        self.transport.cancel_test.assert_not_called()

    def test_under_mutex_preflight_rejects_off_output_or_expired_window(self):
        self.close_first_cut()
        self.renew_permission()
        result = self.start()
        self.advance(1)

        def rejected_preflight():
            with self.assertRaises(UpsPowerBlocked):
                validate_cancel_test_preflight(self.auth_path, self.path, result["operation_id"], self.sample(output_voltage=0))
            return {"state": "submitted"}

        self.transport.cancel_test.side_effect = rejected_preflight
        self.test.cancel(device=self.device, sample=self.sample())

    def test_physical_observation_after_hibernation_closes_without_replaying_control(self):
        self.close_first_cut()
        self.renew_permission()
        result = self.start()
        self.advance(1)
        self.test.cancel(device=self.device, sample=self.sample())
        self.test.interrupted()
        self.advance(210)
        with self.assertRaises(UpsPowerBlocked):
            self.test.confirm_observation(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                          outputs_stayed_on=True, independent_load_observed=False)
        observed = self.test.confirm_observation(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                               outputs_stayed_on=True, independent_load_observed=True)
        self.assertEqual(observed["result"], "cancel_observed_by_user")
        self.assertFalse(observed["production_validated"])
        self.transport.cancel_test.assert_called_once()
        self.assertFalse(observed["can_confirm_observation"])

    def test_failed_cancel_observation_is_recorded_as_failure_without_retry(self):
        self.start()
        self.observe_pending()
        result = self.test.cancel(device=self.device, sample=self.pending_sample())
        self.test.interrupted()
        self.advance(210)
        observed = self.test.confirm_observation(device=self.device, sample=self.sample(), operation_id=result["operation_id"],
                                               outputs_stayed_on=False, independent_load_observed=True)
        self.assertEqual(observed["result"], "cut_observed_after_cancel")
        self.assertFalse(observed["production_validated"])
        self.transport.cancel_test.assert_called_once()


if __name__ == "__main__":
    unittest.main()
