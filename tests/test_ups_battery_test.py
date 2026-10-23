"""Physical battery tests use journal fixtures and fake transports only."""
from datetime import datetime, timedelta, timezone
import copy
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from ups_monitor.ups_battery_test import (AUTH_FILENAME, JOURNAL_FILENAME, OWNER, UpsBatteryTest,
    load_battery_test_operation, validate_battery_test_preflight, validate_battery_test_worker_request)
from ups_monitor.ups_cancel_test import candidate_profile
from ups_monitor.ups_power import NT2402PRO_CANDIDATE_PROFILE, UpsPowerBlocked, VALIDATED_PROFILES
from ups_monitor.ups_test_authorization import BATTERY_AUTHORIZATION_OWNER
from ups_monitor import ups_test_authorization as authorization


class BatteryTestTests(unittest.TestCase):
    def setUp(self):
        self.production_profiles_before = copy.deepcopy(VALIDATED_PROFILES)
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / JOURNAL_FILENAME
        self.auth_path = self.path.with_name(AUTH_FILENAME)
        self.cancel_path = self.path.with_name("ups-cancel-test-operation.json")
        self.now, self.tick = datetime.now(timezone.utc), 1000.0
        self.device = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#fake-battery-test"}
        self.profile = candidate_profile(self.device)
        self.auth = {"schema_version": 1, "owner": BATTERY_AUTHORIZATION_OWNER, "authorization_id": uuid.uuid4().hex,
            "issued_at": (self.now - timedelta(seconds=1)).isoformat(),
            "expires_at": (self.now + timedelta(seconds=899)).isoformat()}
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")
        self.cancel = {"schema_version": 1, "owner": "elipro-ups-cancel-test-operation", "state": "finished",
            "operation_id": str(uuid.uuid4()), "authorization_id": uuid.uuid4().hex,
            "profile": self.profile, "delay_seconds": 180, "service_pid": os.getpid(), "started_monotonic": 1,
            "created_at": (self.now - timedelta(seconds=900)).isoformat(), "cancel_attempted": True,
            "submission_complete": True, "result": "cancel_observed_by_user",
            "physical_observation": {"source": "user_independent_load", "outputs_stayed_on": True,
                "recorded_at": (self.now - timedelta(seconds=600)).isoformat()},
            "compatibility_evidence": {"source_operation_id": str(uuid.uuid4()),
                "device_fingerprint": self.profile["device_fingerprint"], "output_cut_observed": True,
                "pending_signal_not_reported": True, "manual_start_observed": True}}
        self.write_cancel()
        self.transport = Mock()
        self.transport.submit_test.return_value = {"state": "submitted", "confirmation": "unconfirmed", "reports_written": 2}
        self.transport.cancel_after_windows_failure.return_value = {"state": "submitted", "confirmation": "unconfirmed", "reports_written": 1}
        self.factory = Mock(return_value=self.transport)
        self.test = UpsBatteryTest(self.path, transport_factory=self.factory)
        self.addCleanup(patch.stopall)
        patch("ups_monitor.ups_cancel_test._now", side_effect=lambda: self.now).start()
        patch("ups_monitor.ups_test_authorization._now_utc", side_effect=lambda *_args: self.now).start()
        patch("ups_monitor.ups_battery_test.time.monotonic", side_effect=lambda: self.tick).start()
        date = patch("ups_monitor.ups_power.datetime", wraps=datetime).start()
        date.now.side_effect = lambda *_args: self.now
        self.test.observe(device=self.device, sample=self.sample())
        self.advance(1)
        self.test.observe(device=self.device, sample=self.battery())

    def write_cancel(self):
        self.cancel_path.write_text(json.dumps(self.cancel), encoding="utf-8")

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds

    def sample(self, **changes):
        return {"timestamp": self.now.isoformat(), "on_battery": False, "low_battery": False, "fault": False,
            "output_voltage": 220, "load_percent": 6, "raw_status": "Line mode", "raw_flags": {"status_hex": "09"}, **changes}

    def battery(self, **changes):
        return self.sample(on_battery=True, raw_status="Battery mode", raw_flags={"status_hex": "89"}, **changes)

    def start(self, **changes):
        return self.test.start(device=changes.pop("device", self.device), sample=changes.pop("sample", self.battery()),
            policy_enabled=changes.pop("policy_enabled", False), blocked=changes.pop("blocked", False),
            acknowledge_risk=changes.pop("acknowledge_risk", True), **changes)

    def status(self):
        return self.test.status(device=self.device, sample=self.battery())

    def finish(self, **changes):
        operation_id = self.test.status()["operation_id"]
        values = {"operation_id": operation_id, "cut_observed": True, "ac_restored": True,
            "off_after_ac_seconds": 60, "manual_restart": True, "hibernate_resumed": True,
            "automatic_return_observed": False, "acknowledge_observation": True}
        values.update(changes)
        return self.test.confirm_observation(device=self.device, sample=self.sample(), **values)

    def test_requires_distinct_authorization_and_risk_ack_before_any_usb(self):
        self.assertTrue(self.status()["available"])
        with self.assertRaises(UpsPowerBlocked):
            self.start(acknowledge_risk=False)
        self.auth["owner"] = "elipro-ups-cancel-test"
        self.auth_path.write_text(json.dumps(self.auth), encoding="utf-8")
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_requires_physical_successful_cancellation_bound_to_same_unit(self):
        variants = (("result", "cancel_observed_in_qs"), ("submission_complete", False), ("cancel_attempted", False),
                    ("physical_observation", {"source": "user_independent_load", "outputs_stayed_on": False}),
                    ("profile", {**self.profile, "device_fingerprint": "f" * 64}))
        original = dict(self.cancel)
        for key, value in variants:
            self.cancel = {**original, key: value}
            self.write_cancel()
            with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                self.start()
        self.factory.assert_not_called()

    def test_opening_already_on_battery_or_missing_fresh_online_baseline_blocks(self):
        self.test = UpsBatteryTest(self.path, transport_factory=self.factory)
        self.test.observe(device=self.device, sample=self.battery())
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.test.observe(device=self.device, sample=self.sample())
        self.advance(16)
        self.test.observe(device=self.device, sample=self.battery())
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.factory.assert_not_called()

    def test_old_battery_episode_gap_high_load_and_fault_block_before_journal(self):
        for evidence in (self.battery(load_percent=41), self.battery(load_percent=True),
                         self.battery(low_battery=True), self.battery(fault=True), self.sample(),
                         self.battery(output_voltage=0), self.battery(timestamp="2000-01-01T00:00:00Z")):
            with self.subTest(evidence=evidence), self.assertRaises(UpsPowerBlocked):
                self.start(sample=evidence)
        self.advance(31)
        self.test.observe(device=self.device, sample=self.battery())
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_policy_block_or_foreign_descriptor_never_opens_transport(self):
        for values in ({"policy_enabled": True}, {"blocked": True}, {"device": {**self.device, "version_number": 4}}):
            with self.subTest(values=values), self.assertRaises(UpsPowerBlocked):
                self.start(**values)
        self.factory.assert_not_called()

    def test_records_before_one_fixed_submission_and_worker_checks_it(self):
        def sent():
            operation = load_battery_test_operation(self.path)
            self.assertEqual(operation["state"], "arming")
            self.assertEqual(operation["delay_seconds"], 180)
            self.assertEqual(validate_battery_test_worker_request(self.auth_path, self.path, operation["operation_id"], "start"), self.profile)
            self.assertTrue(validate_battery_test_preflight(self.auth_path, self.path, operation["operation_id"], "start", sample=self.battery()))
            return {"state": "submitted"}

        self.transport.submit_test.side_effect = sent
        result = self.start()
        self.assertEqual(result["windows_request"], "pending")
        self.assertEqual(result["state"], "submitted")
        self.transport.submit_test.assert_called_once_with()
        with self.assertRaises(UpsPowerBlocked):
            self.start()
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_partial_write_does_not_throw_after_arming_or_retry(self):
        self.transport.submit_test.side_effect = TimeoutError("partial")
        result = self.start()
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNotNone(result["operation_id"])
        self.assertEqual(result["windows_request"], "pending")
        self.test.record_windows_request(True)
        self.assertEqual(self.test.status()["windows_request"], "requested")
        self.transport.submit_test.assert_called_once()
        self.transport.cancel_after_windows_failure.assert_not_called()

    def test_failed_prewrite_journal_never_calls_usb(self):
        with patch.object(self.test._journal, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.start()
        self.factory.assert_not_called()

    def test_immediate_windows_callback_precedes_every_post_usb_write(self):
        original = self.test._journal.write
        order = []

        def write(value):
            order.append("journal:" + value["state"])
            original(value)

        def request_windows():
            self.transport.submit_test.assert_called_once()
            self.assertEqual(load_battery_test_operation(self.path)["state"], "arming")
            order.append("windows")

        with patch.object(self.test._journal, "write", side_effect=write):
            self.start(on_submission=request_windows)
        self.assertEqual(order, ["journal:arming", "windows", "journal:submitted"])

    def test_partial_submission_also_requests_windows_once_before_result_flush(self):
        self.transport.submit_test.side_effect = OSError("partial write")
        callback = Mock(side_effect=lambda: self.assertEqual(load_battery_test_operation(self.path)["state"], "arming"))
        result = self.start(on_submission=callback)
        callback.assert_called_once_with()
        self.assertEqual(result["state"], "uncertain")
        self.transport.submit_test.assert_called_once()

    def test_windows_callback_failure_does_not_erase_complete_usb_submission(self):
        callback = Mock(side_effect=OSError("Windows rejected"))
        result = self.start(on_submission=callback)
        callback.assert_called_once()
        self.assertEqual(result["state"], "submitted")
        operation = load_battery_test_operation(self.path)
        self.assertTrue(operation["submission_complete"])
        self.assertTrue(operation["windows_callback_failed"])

    def test_failed_postwrite_journal_retains_operation_for_immediate_windows_request(self):
        original = self.test._journal.write
        count = [0]

        def write(value):
            count[0] += 1
            if count[0] > 1:
                raise OSError("disk full after USB")
            original(value)

        with patch.object(self.test._journal, "write", side_effect=write):
            result = self.start()
            self.assertIsNotNone(result["operation_id"])
            self.test.record_windows_request(False, error="Windows rejected")
            self.assertEqual(self.test.status()["windows_request"], "failed")
        self.transport.submit_test.assert_called_once()

    def test_guard_failure_never_sends_and_keeps_own_armed_intent(self):
        result = self.start(guard=lambda: False)
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNotNone(result["operation_id"])
        self.transport.submit_test.assert_not_called()

    def test_suspend_signal_before_transport_binding_blocks_every_report(self):
        def factory(*args):
            self.test.signal_interruption()
            return self.transport

        self.factory.side_effect = factory
        result = self.start()
        self.assertEqual(result["state"], "uncertain")
        self.transport.signal_interruption.assert_called_once()
        self.transport.submit_test.assert_not_called()

    def test_failed_windows_cancel_can_close_as_incomplete_after_observation(self):
        self.start()
        self.test.record_windows_request(False)
        self.advance(1)
        self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.advance(220)
        result = self.finish()
        self.assertEqual(result["result"], "physical_test_incomplete")
        self.assertFalse(self.test.pending())

    def test_cancel_is_only_for_windows_rejection_and_is_single_owned_attempt(self):
        self.start()
        self.advance(1)
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.test.record_windows_request(False, error="rejected")
        result = self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.assertEqual(result["state"], "cancel_submitted")
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.transport.cancel_after_windows_failure.assert_called_once()

    def test_uncertain_submission_cannot_be_adopted_even_with_pending_bit(self):
        self.transport.submit_test.side_effect = OSError("partial")
        self.start()
        self.test.record_windows_request(False)
        self.advance(1)
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        pending = self.sample(on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"})
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel_after_windows_failure(device=self.device, sample=pending)
        self.transport.cancel_after_windows_failure.assert_not_called()

    def test_worker_preflight_and_late_failure_cannot_cancel(self):
        self.start()
        self.test.record_windows_request(False)
        self.advance(171)
        with self.assertRaises(UpsPowerBlocked):
            self.test.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.transport.cancel_after_windows_failure.assert_not_called()

    def test_restart_and_suspend_never_replay_but_allow_physical_observation(self):
        self.start()
        self.test.record_windows_request(True)
        self.test.interrupted()
        restarted = UpsBatteryTest(self.path, transport_factory=self.factory)
        self.assertEqual(restarted.status()["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            restarted.start(device=self.device, sample=self.battery(), policy_enabled=False, blocked=False, acknowledge_risk=True)
        self.advance(220)
        self.test = restarted
        self.test.observe(device=self.device, sample=self.sample())
        result = self.finish()
        self.assertEqual(result["result"], "battery_cut_manual_restart_observed")
        self.assertFalse(result["production_validated"])
        self.transport.submit_test.assert_called_once()
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_finished_observation_and_cancel_evidence_survive_reboot_without_rewriting(self):
        self.start()
        self.test.record_windows_request(True)
        self.advance(220)
        finished = self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        saved = self.path.read_bytes()
        operation = json.loads(saved)
        self.tick = 0.5
        self.factory.reset_mock()

        restarted = UpsBatteryTest(self.path, transport_factory=self.factory)
        status = restarted.status(device=self.device, sample=self.sample())
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["operation_id"], finished["operation_id"])
        self.assertEqual(status["result"], "automatic_return_observed")
        self.assertEqual(status["physical_observation"], finished["physical_observation"])
        self.assertEqual(restarted._operation, operation)
        self.assertEqual(restarted._previous_cancel(self.profile), self.cancel)
        self.assertEqual(restarted.compatibility(self.device)["source_operation_id"], finished["operation_id"])
        self.assertFalse(restarted.pending())
        self.assertFalse(status["production_validated"])
        self.assertIsNone(status["remaining"])
        self.assertEqual(load_battery_test_operation(self.path), operation)
        self.assertEqual(self.path.read_bytes(), saved)
        self.factory.assert_not_called()
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)

    def test_pending_operation_survives_clock_reset_as_review_without_replay_or_cancel(self):
        started = self.start()
        self.test.record_windows_request(False)
        saved = self.path.read_bytes()
        self.tick = 1
        self.factory.reset_mock()

        restarted = UpsBatteryTest(self.path, transport_factory=self.factory)
        restarted.observe(device=self.device, sample=self.sample())
        status = restarted.status(device=self.device, sample=self.sample())
        self.assertEqual(status["state"], "review_required")
        self.assertEqual(status["operation_id"], started["operation_id"])
        self.assertEqual(status["windows_request"], "failed")
        self.assertTrue(restarted.pending())
        self.assertIsNone(status["remaining"])
        self.assertFalse(status["can_confirm_observation"])
        with self.assertRaises(UpsPowerBlocked):
            restarted.start(device=self.device, sample=self.battery(), policy_enabled=False, blocked=False, acknowledge_risk=True)
        with self.assertRaises(UpsPowerBlocked):
            restarted.cancel_after_windows_failure(device=self.device, sample=self.battery())
        self.assertEqual(self.path.read_bytes(), saved)
        self.factory.assert_not_called()

    def test_loader_still_rejects_malformed_persisted_ticks(self):
        self.start()
        operation = json.loads(self.path.read_text(encoding="utf-8"))
        for state in ("submitted", "finished"):
            for tick in (None, True, "1000", -1, float("nan"), float("inf")):
                with self.subTest(state=state, tick=tick):
                    self.path.write_text(json.dumps({**operation, "state": state, "started_monotonic": tick}), encoding="utf-8")
                    with self.assertRaises(UpsPowerBlocked):
                        load_battery_test_operation(self.path)

    def test_workers_still_reject_a_reset_clock_for_start_and_cancel(self):
        self.start()
        operation = json.loads(self.path.read_text(encoding="utf-8"))
        self.tick = 1
        for selector, state in (("start", "arming"), ("cancel", "cancelling")):
            with self.subTest(selector=selector):
                self.path.write_text(json.dumps({**operation, "state": state}), encoding="utf-8")
                self.assertEqual(load_battery_test_operation(self.path)["operation_id"], operation["operation_id"])
                with self.assertRaisesRegex(UpsPowerBlocked, "El reloj de la operación cambió"):
                    validate_battery_test_worker_request(self.auth_path, self.path, operation["operation_id"], selector)

    def test_observation_records_failure_without_inventing_automatic_return(self):
        self.start()
        self.test.record_windows_request(True)
        self.advance(220)
        result = self.finish(off_after_ac_seconds=10, manual_restart=False)
        self.assertEqual(result["result"], "physical_test_incomplete")
        self.assertFalse(self.test.pending())
        self.assertFalse(self.test.status(device=self.device, sample=self.battery())["available"])

    def test_automatic_return_requires_explicit_observation(self):
        self.start()
        self.test.record_windows_request(True)
        self.advance(220)
        result = self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        self.assertEqual(result["result"], "automatic_return_observed")

    def test_finished_physical_result_precedes_an_earlier_failed_windows_request(self):
        self.start()
        self.test.record_windows_request(False, error="Earlier Windows request failed")
        self.assertIn("Windows rechazó", self.status()["message"])
        self.advance(220)
        result = self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["windows_request"], "failed")
        self.assertIn("Este comportamiento está aceptado", result["message"])
        self.assertIn("Prueba cerrada", result["message"])
        self.assertNotIn("corte podría seguir pendiente", result["message"])
        self.assertFalse(result["production_validated"])
        self.assertFalse(self.test.pending())

    def test_status_exposes_only_eight_known_physical_observation_fields_as_a_copy(self):
        self.start()
        self.test.record_windows_request(True)
        self.advance(220)
        self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        self.test._operation["physical_observation"].update(authorization_id="not-public", device_path="not-public", command="not-public")
        public = self.status()["physical_observation"]
        self.assertEqual(set(public), {"source", "recorded_at", "cut_observed", "ac_restored", "off_after_ac_seconds",
                                       "manual_restart", "hibernate_resumed", "automatic_return_observed"})
        self.assertEqual(public["source"], "user_physical_observation")
        self.assertTrue(public["automatic_return_observed"])
        public["cut_observed"] = False
        self.assertTrue(self.test._operation["physical_observation"]["cut_observed"])
        self.test._operation["physical_observation"]["manual_restart"] = {"secret": "not-a-boolean"}
        self.test._operation["physical_observation"]["off_after_ac_seconds"] = float("nan")
        sanitized = self.status()["physical_observation"]
        self.assertNotIn("manual_restart", sanitized)
        self.assertNotIn("off_after_ac_seconds", sanitized)

    def test_accepted_automatic_restart_is_bound_to_the_observed_unit_and_does_not_validate_a_profile(self):
        self.assertIsNone(self.test.compatibility(self.device))
        self.start()
        self.test.record_windows_request(False)
        self.advance(220)
        result = self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        compatibility = self.test.compatibility(self.device)
        self.assertEqual(set(compatibility), {"restart_behavior", "restart_behavior_observed", "automatic_restart_accepted", "manual_restart", "source_operation_id"})
        self.assertEqual(compatibility["restart_behavior"], "automatic_on_mains")
        self.assertTrue(compatibility["restart_behavior_observed"])
        self.assertTrue(compatibility["automatic_restart_accepted"])
        self.assertEqual(compatibility["source_operation_id"], result["operation_id"])
        self.assertFalse(compatibility["manual_restart"])
        self.assertIsNone(self.test.compatibility())
        self.assertIsNone(self.test.compatibility({**self.device, "path": self.device["path"] + "-another-unit"}))
        self.assertIsNone(self.test.compatibility({**self.device, "version_number": 99}))
        self.assertEqual(VALIDATED_PROFILES, self.production_profiles_before)
        self.transport.submit_test.assert_called_once()

    def test_restart_behavior_requires_finished_result_and_explicit_typed_physical_evidence(self):
        self.start()
        self.test.record_windows_request(True)
        self.advance(220)
        self.finish(off_after_ac_seconds=0, manual_restart=False, automatic_return_observed=True)
        original = self.test._operation
        for updates in ({"state": "uncertain"}, {"result": "physical_test_incomplete"},
                        {"physical_observation": {**original["physical_observation"], "source": "inferred"}},
                        {"physical_observation": {**original["physical_observation"], "cut_observed": False}},
                        {"physical_observation": {**original["physical_observation"], "ac_restored": 1}},
                        {"physical_observation": {**original["physical_observation"], "automatic_return_observed": False}}):
            with self.subTest(updates=updates):
                self.test._operation = {**original, **updates}
                self.assertIsNone(self.test.compatibility(self.device))
        self.test._operation = original

    def test_confirmation_rejects_wrong_operation_early_unhealthy_or_untyped_answers(self):
        self.start()
        with self.assertRaises(UpsPowerBlocked):
            self.finish()
        self.advance(220)
        for values in ({"operation_id": str(uuid.uuid4())}, {"cut_observed": 1}, {"off_after_ac_seconds": True},
                       {"off_after_ac_seconds": float("nan")}, {"acknowledge_observation": False}):
            with self.subTest(values=values), self.assertRaises(UpsPowerBlocked):
                self.finish(**values)


class BatteryAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".test-data")
        self.addCleanup(self.directory.cleanup)
        self.directory_path = Path(self.directory.name).resolve()
        self.path = self.directory_path / AUTH_FILENAME
        self.journal_path = self.directory_path / JOURNAL_FILENAME
        self.now = datetime.now(timezone.utc)
        self.value = {"schema_version": 1, "owner": BATTERY_AUTHORIZATION_OWNER,
            "authorization_id": uuid.uuid4().hex, "issued_at": (self.now - timedelta(seconds=1)).isoformat(),
            "expires_at": (self.now + timedelta(seconds=899)).isoformat()}
        self.path.write_text(json.dumps(self.value), encoding="utf-8")

    def test_battery_and_cancel_authorizations_cannot_be_interchanged(self):
        self.assertEqual(authorization.read_battery_test_authorization(self.path), self.value)
        with self.assertRaises(ValueError):
            authorization.read_test_authorization(self.path)
        self.value["owner"] = authorization.AUTHORIZATION_OWNER
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        with self.assertRaises(ValueError):
            authorization.read_battery_test_authorization(self.path)

    def test_battery_worker_requires_actual_system_and_exact_registered_namespace(self):
        with patch.object(authorization.service, "_is_local_system", return_value=False), \
             patch.object(authorization, "_registered_data_dir") as registered:
            with self.assertRaises(ValueError):
                authorization.authorize_battery_test_worker(self.path, self.journal_path)
            registered.assert_not_called()
        with patch.object(authorization.service, "_is_local_system", return_value=True), \
             patch.object(authorization, "_registered_data_dir", return_value=self.directory_path):
            self.assertIs(authorization.authorize_battery_test_worker(self.path, self.journal_path), True)
            for path, journal in ((self.path.with_name(authorization.AUTHORIZATION_FILENAME), self.journal_path),
                                  (self.path, self.journal_path.with_name(authorization.JOURNAL_FILENAME)),
                                  (self.path, self.directory_path.parent / JOURNAL_FILENAME)):
                with self.subTest(path=path, journal=journal), self.assertRaises(ValueError):
                    authorization.authorize_battery_test_worker(path, journal)

    def test_cli_uses_own_namespace_and_no_usb_or_service_mutation(self):
        stdout = io.StringIO()
        with patch.object(authorization.os, "name", "nt"), patch.object(authorization, "_is_admin", return_value=True), \
             patch.object(authorization, "_registered_data_dir", return_value=self.directory_path), \
             patch("ups_monitor.hid_windows._apis") as apis, patch("sys.stdout", stdout):
            self.assertEqual(authorization.battery_main(["--minutes", "5"]), 0)
        self.assertEqual(authorization.read_battery_test_authorization(self.path)["owner"], BATTERY_AUTHORIZATION_OWNER)
        self.assertFalse(self.path.with_name(authorization.AUTHORIZATION_FILENAME).exists())
        self.assertIn("expires_at", json.loads(stdout.getvalue()))
        apis.assert_not_called()

    def test_cli_cannot_renew_pending_or_foreign_journal(self):
        before = self.path.read_bytes()
        for value in ({"owner": OWNER, "state": "uncertain"}, {"owner": "foreign", "state": "finished"}):
            self.journal_path.write_text(json.dumps(value), encoding="utf-8")
            with patch.object(authorization.os, "name", "nt"), patch.object(authorization, "_is_admin", return_value=True), \
                 patch.object(authorization, "_registered_data_dir", return_value=self.directory_path), patch("sys.stderr"):
                self.assertEqual(authorization.battery_main([]), 1)
            self.assertEqual(self.path.read_bytes(), before)

    def test_cli_admin_and_duration_are_mandatory(self):
        with patch.object(authorization.os, "name", "nt"), patch.object(authorization, "_is_admin", return_value=False), \
             patch.object(authorization, "_registered_data_dir") as registered, patch("sys.stderr"):
            self.assertEqual(authorization.battery_main([]), 1)
            registered.assert_not_called()
        for minutes in ("4", "16"):
            with self.subTest(minutes=minutes), patch("sys.stderr"), self.assertRaises(SystemExit):
                authorization.battery_main(["--minutes", minutes])


if __name__ == "__main__":
    unittest.main()
