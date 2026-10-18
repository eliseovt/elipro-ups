"""UPS output control uses fake profiles and transports; never opens USB."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ups_monitor.ups_power import (NT2402PRO_CANDIDATE_PROFILE, VALIDATED_PROFILES,
    OBSERVED_UNIT_FINGERPRINT, _profile_evidence, validated_control_profile,
    OperationJournal, UpsPowerBlocked, UpsPowerCoordinator, control_reports,
    device_fingerprint, minimum_delay_seconds, shutdown_command, validate_production_cancel_preflight,
    validate_production_start_preflight, validate_observed_sequence_preflight, configured_delay_seconds)


DEVICE = {**NT2402PRO_CANDIDATE_PROFILE, "path": r"\\?\hid#vid_0665&pid_5161#test-only-unit"}


def validated_profile():
    return {**NT2402PRO_CANDIDATE_PROFILE, "device_fingerprint": device_fingerprint(DEVICE),
            "manual_restart_verified": True, "cancel_verified": True,
            "low_battery_cutoff_behavior": "immediate",
            "action_validation": {"shutdown": {"completion_seconds": [30, 39, 40], "reserve_seconds": 600},
                                  "hibernate": {"completion_seconds": [61, 70, 80], "reserve_seconds": 600}}}


def unreliable_profile():
    profile = validated_profile()
    profile.update(pending_signal_unreliable=True, pending_signal_evidence={
        "device_fingerprint": device_fingerprint(DEVICE), "output_cut_observed": True,
        "pending_signal_not_reported": True, "cancel_observed_without_cut": True, "manual_start_observed": True})
    return profile


def sequence_profile():
    """Simulate the approved conditions on a different, explicitly fake unit."""
    profile = copy.deepcopy(VALIDATED_PROFILES[NT2402PRO_CANDIDATE_PROFILE["profile_id"]])
    profile["device_fingerprint"] = device_fingerprint(DEVICE)
    profile["pending_signal_evidence"]["device_fingerprint"] = profile["device_fingerprint"]
    return profile


def sample(**changes):
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "on_battery": True,
            "low_battery": False, "fault": False, "output_voltage": 220,
            "raw_status": "Battery mode", "raw_flags": {"status_hex": "89"}, **changes}


def pending_sample():
    return sample(on_battery=None, raw_status="Shutdown mode", raw_flags={"status_hex": "8B"})


class UpsPowerCodecTests(unittest.TestCase):
    def test_stayoff_codec_uses_two_reports_and_keeps_cr_in_second(self):
        self.assertEqual(shutdown_command(180), b"S03R0000\r")
        self.assertEqual(control_reports(shutdown_command(180)), (b"\x00S03R0000", b"\x00\r\x00\x00\x00\x00\x00\x00\x00"))
        self.assertEqual(control_reports(b"C\r"), (b"\x00C\r\x00\x00\x00\x00\x00\x00",))
        for command in (b"S03R0001\r", b"S00R0000\r", b"S.5R0000\r", b"C\rS01\r", b"QS\r", "C\r"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                control_reports(command)
        for delay in (True, 0, 59, 61, 541, 600, 180.0, "180"):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                shutdown_command(delay)

    def test_delay_uses_at_least_two_measurements_or_honest_range_plus_margin(self):
        for timings, expected in (([1, 20, 60], 180), ([30, 40], 180), ([59, 60, 61], 240), ([200, 300, 420], 540),
                                 ({"trial_count": 2, "observed_range_seconds": [10, 15]}, 180)):
            self.assertEqual(minimum_delay_seconds(timings), expected)
        for timings in (None, [], [30], [30, True, 40], [30, float("nan"), 40], [0, 2, 3], [420.01, 1, 2],
                        {"trial_count": 1, "observed_range_seconds": [10, 15]},
                        {"trial_count": True, "observed_range_seconds": [10, 15]},
                        {"trial_count": 2, "observed_range_seconds": [15, 10]},
                        {"trial_count": 2, "observed_range_seconds": [10, float("nan")]},
                        {"trial_count": 2, "observed_range_seconds": [10, 15], "invented_trial": 15}):
            with self.subTest(timings=timings), self.assertRaises(UpsPowerBlocked):
                minimum_delay_seconds(timings)


class UpsPowerCoordinatorTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "power-operation.json"
        self.profile = validated_profile()
        self.profiles = {self.profile["profile_id"]: self.profile}
        self.transport = Mock()
        self.transport.submit_shutdown.return_value = {"state": "submitted", "confirmation": "unconfirmed"}
        self.transport.cancel_shutdown.return_value = {"state": "submitted", "confirmation": "unconfirmed"}
        self.factory = Mock(return_value=self.transport)
        self.clock = Mock(return_value=100)
        self.coordinator = self.make()

    def make(self, **changes):
        return UpsPowerCoordinator(changes.pop("path", self.path), validated_profiles=changes.pop("profiles", self.profiles),
                                   transport_factory=self.factory, clock=self.clock, **changes)

    def arm(self, coordinator=None, **changes):
        return (coordinator or self.coordinator).arm("shutdown", device=changes.pop("device", DEVICE),
                sample=changes.pop("sample", sample()), reserve_seconds=changes.pop("reserve_seconds", 600), **changes)

    def test_production_profile_never_allows_an_unknown_unit_or_calls_its_transport(self):
        self.assertEqual(set(VALIDATED_PROFILES), {NT2402PRO_CANDIDATE_PROFILE["profile_id"]})
        coordinator = UpsPowerCoordinator(self.path, transport_factory=self.factory)
        self.assertFalse(coordinator.capabilities(DEVICE)["available"])
        with self.assertRaises(UpsPowerBlocked):
            self.arm(coordinator)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_no_device_or_missing_durable_journal_cannot_arm(self):
        self.assertFalse(self.coordinator.capabilities()["available"])
        coordinator = self.make(path=None)
        self.assertEqual(coordinator.status()["state"], "idle")
        with self.assertRaisesRegex(UpsPowerBlocked, "durable"):
            self.arm(coordinator)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_validation_is_per_action_and_bound_to_one_device(self):
        self.assertEqual(self.coordinator.capabilities(DEVICE, "shutdown")["delay_seconds"], 180)
        self.assertEqual(self.coordinator.capabilities(DEVICE, "hibernate")["delay_seconds"], 240)
        for change in ({"version_number": 4}, {"path": DEVICE["path"] + "-other"}, {"protocol": "P"},
                       {"input_report_bytes": 8}, {"vendor_id": True}):
            with self.subTest(change=change):
                self.assertFalse(self.coordinator.capabilities({**DEVICE, **change})["available"])
        for missing in ("manual_restart_verified", "cancel_verified", "low_battery_cutoff_behavior", "device_fingerprint"):
            profile = copy.deepcopy(self.profile)
            profile.pop(missing)
            coordinator = self.make(profiles={profile["profile_id"]: profile})
            self.assertFalse(coordinator.capabilities(DEVICE)["available"])
        profile = copy.deepcopy(self.profile)
        profile["action_validation"].pop("hibernate")
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.assertTrue(coordinator.capabilities(DEVICE, "shutdown")["available"])
        self.assertFalse(coordinator.capabilities(DEVICE, "hibernate")["available"])

    def test_automatic_return_is_a_valid_explicit_behavior_without_manual_requirement(self):
        profile = validated_profile()
        profile.pop("manual_restart_verified")
        profile.update(restart_behavior="automatic_on_mains", restart_behavior_verified=True)
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        capability = coordinator.capabilities(DEVICE)
        self.assertTrue(capability["available"])
        self.assertEqual(capability["restart_behavior"], "automatic_on_mains")
        self.assertIs(capability["manual_restart"], False)
        operation = self.arm(coordinator)
        self.assertEqual(operation["restart_behavior"], "automatic_on_mains")
        self.assertIs(operation["manual_restart"], False)
        self.assertTrue(operation["restart_behavior_verified"])
        self.assertEqual(OperationJournal(self.path).load(), operation)
        self.transport.submit_shutdown.assert_called_once_with(180)
        self.assertEqual(shutdown_command(180), b"S03R0000\r")
        self.assertEqual(VALIDATED_PROFILES[profile["profile_id"]]["device_fingerprint"], OBSERVED_UNIT_FINGERPRINT)

    def test_automatic_return_profile_requires_verified_behavior_and_unit_binding(self):
        for changes in ({"restart_behavior": "automatic_on_mains"},
                        {"restart_behavior": "automatic_on_mains", "restart_behavior_verified": False},
                        {"restart_behavior": "automatic_on_mains", "restart_behavior_verified": 1},
                        {"restart_behavior": "unknown", "restart_behavior_verified": True}):
            profile = {**validated_profile(), **changes}
            with self.subTest(changes=changes):
                self.assertFalse(self.make(profiles={profile["profile_id"]: profile}).capabilities(DEVICE)["available"])
        profile = validated_profile()
        profile.update(restart_behavior="automatic_on_mains", restart_behavior_verified=True,
                       device_fingerprint="0" * 64)
        self.assertFalse(self.make(profiles={profile["profile_id"]: profile}).capabilities(DEVICE)["available"])

    def test_unreliable_signal_auto_profile_uses_automatic_return_evidence_not_manual_start(self):
        profile = unreliable_profile()
        profile.pop("manual_restart_verified")
        profile.update(restart_behavior="automatic_on_mains", restart_behavior_verified=True)
        evidence = profile["pending_signal_evidence"]
        evidence.pop("manual_start_observed")
        evidence["automatic_return_observed"] = True
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.assertTrue(coordinator.capabilities(DEVICE)["available"])
        self.arm(coordinator)
        self.assertEqual(coordinator.cancel(device=DEVICE, sample=sample())["state"], "cancel_submitted")
        self.transport.cancel_shutdown.assert_called_once_with()
        for field in ("output_cut_observed", "pending_signal_not_reported", "cancel_observed_without_cut", "automatic_return_observed", "device_fingerprint"):
            invalid = copy.deepcopy(profile)
            invalid["pending_signal_evidence"].pop(field)
            with self.subTest(field=field):
                self.assertFalse(self.make(profiles={invalid["profile_id"]: invalid}).capabilities(DEVICE)["available"])

    def test_journal_accepts_legacy_manual_but_auto_requires_explicit_verified_matching_fields(self):
        operation = self.arm()
        operation.pop("restart_behavior")
        operation.pop("restart_behavior_verified")
        self.coordinator._journal.write(operation)
        self.assertEqual(OperationJournal(self.path).load(), operation)
        automatic = {**operation, "restart_behavior": "automatic_on_mains", "restart_behavior_verified": True, "manual_restart": False}
        self.coordinator._journal.write(automatic)
        self.assertEqual(OperationJournal(self.path).load(), automatic)
        for changes in ({"restart_behavior": None}, {"restart_behavior_verified": False},
                        {"manual_restart": True}, {"manual_restart": None}, {"manual_restart": 0}):
            self.coordinator._journal.write({**automatic, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OperationJournal(self.path).load()

    def test_default_capability_is_neutral_and_legacy_manual_is_reported_honestly(self):
        blocked = UpsPowerCoordinator().capabilities(DEVICE)
        self.assertEqual(blocked["validation"], "profile_unvalidated")
        self.assertIsNone(blocked["restart_behavior"])
        self.assertNotIn("manual", blocked["reason"].lower())
        legacy = self.coordinator.capabilities(DEVICE)
        self.assertEqual(legacy["restart_behavior"], "manual_only")
        self.assertIs(legacy["manual_restart"], True)

    def test_missing_reserve_or_low_battery_blocks_before_journal_and_usb(self):
        for values in ({"reserve_seconds": 179}, {"reserve_seconds": True},
                       {"sample": sample(low_battery=True)}, {"sample": sample(low_battery=None)},
                       {"sample": sample(on_battery=False)}, {"sample": sample(fault=True)},
                       {"sample": sample(timestamp="2000-01-01T00:00:00Z")}):
            with self.subTest(values=values), self.assertRaises(UpsPowerBlocked):
                self.arm(**values)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_journal_is_durable_before_transport_and_submission_is_not_ack(self):
        def submitted(delay):
            record = OperationJournal(self.path).load()
            self.assertEqual(record["state"], "arming")
            self.assertEqual(record["delay_seconds"], delay)
            return {"state": "submitted", "confirmation": "unconfirmed"}
        self.transport.submit_shutdown.side_effect = submitted
        result = self.arm()
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(result["confirmation"], "unconfirmed")
        self.transport.submit_shutdown.assert_called_once_with(180)
        with self.assertRaises(UpsPowerBlocked):
            self.arm()
        self.transport.submit_shutdown.assert_called_once()

    def test_failed_prewrite_journal_never_opens_transport(self):
        with patch.object(self.coordinator._journal, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.arm()
        self.factory.assert_not_called()

    def test_uncertain_control_is_never_replayed_after_failure_or_restart(self):
        self.transport.submit_shutdown.side_effect = TimeoutError("uncertain write")
        self.assertEqual(self.arm()["state"], "uncertain")
        restarted = self.make()
        self.assertEqual(restarted.status()["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            self.arm(restarted)
        self.transport.submit_shutdown.assert_called_once()

    def test_observation_requires_own_same_device_and_real_shutdown_bit(self):
        self.arm()
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=sample())["state"], "submitted")
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=sample(raw_status="Shutdown mode"))["state"], "submitted")
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=pending_sample())["state"], "armed")
        result = self.coordinator.mark_windows_requested()
        self.assertTrue(result["windows_requested"])
        self.assertEqual(result["confirmation"], "observed_in_qs")

    def test_qs_captured_before_or_at_shutdown_submission_cannot_confirm_or_cancel_it(self):
        before = pending_sample()
        submitted = self.arm()
        at_submission = {**pending_sample(), "timestamp": submitted["submitted_at"]}
        for evidence in (before, at_submission):
            with self.subTest(timestamp=evidence["timestamp"]):
                self.assertEqual(self.coordinator.observe(device=DEVICE, sample=evidence)["state"], "submitted")
                with self.assertRaises(UpsPowerBlocked):
                    self.coordinator.cancel(device=DEVICE, sample=evidence)
        self.transport.cancel_shutdown.assert_not_called()
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=pending_sample())["state"], "armed")

    def test_qs_captured_before_or_at_cancel_submission_cannot_confirm_cancellation(self):
        self.arm()
        before_cancel = sample()
        cancelled = self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        at_cancel = {**sample(), "timestamp": cancelled["cancel_submitted_at"]}
        for evidence in (before_cancel, at_cancel):
            with self.subTest(timestamp=evidence["timestamp"]):
                self.assertEqual(self.coordinator.observe(device=DEVICE, sample=evidence)["state"], "cancel_submitted")
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=sample())["state"], "cancelled")

    def test_uncertain_partial_submission_never_authorizes_cancel_even_with_later_shutdown_bit(self):
        before = pending_sample()
        self.transport.submit_shutdown.side_effect = TimeoutError("uncertain write")
        uncertain = self.arm()
        self.assertIn("submission_finished_at", uncertain)
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=before)
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.transport.cancel_shutdown.assert_not_called()

    def test_cancel_requires_owned_pending_shutdown_and_confirmed_powered_output(self):
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.arm()
        for evidence in (sample(), sample(raw_status="Shutdown mode"), pending_sample() | {"output_voltage": 0},
                         pending_sample() | {"low_battery": True}):
            with self.subTest(evidence=evidence), self.assertRaises(UpsPowerBlocked):
                self.coordinator.cancel(device=DEVICE, sample=evidence)
        self.transport.cancel_shutdown.assert_not_called()
        result = self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.assertEqual(result["state"], "cancel_submitted")
        self.assertEqual(result["confirmation"], "unconfirmed")
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.transport.cancel_shutdown.assert_called_once()
        incomplete = sample(raw_flags={})
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=incomplete)["state"], "cancel_submitted")
        self.assertEqual(self.coordinator.observe(device=DEVICE, sample=sample())["state"], "cancelled")

    def test_cancel_cannot_run_at_cutoff_or_repeat_after_uncertain_attempt(self):
        self.arm()
        self.clock.return_value = 276
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.clock.return_value = 110
        self.transport.cancel_shutdown.side_effect = TimeoutError("uncertain cancel")
        self.assertEqual(self.coordinator.cancel(device=DEVICE, sample=pending_sample())["state"], "uncertain")
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.transport.cancel_shutdown.assert_called_once()

    def test_restart_pending_needs_review_and_never_replays_even_corroborated_cancel(self):
        self.arm()
        restarted = self.make()
        self.assertEqual(restarted.status()["state"], "review_required")
        self.assertEqual(restarted.observe(device=DEVICE, sample=pending_sample())["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            restarted.cancel(device=DEVICE, sample=sample())
        with self.assertRaises(UpsPowerBlocked):
            restarted.cancel(device=DEVICE, sample=pending_sample())
        self.transport.cancel_shutdown.assert_not_called()

    def test_range_evidence_is_preserved_and_unreliable_signal_requires_same_unit_physical_facts(self):
        profile = unreliable_profile()
        evidence = {"trial_count": 2, "observed_range_seconds": [10, 15]}
        profile["action_validation"]["hibernate"] = {"completion_evidence": evidence, "reserve_seconds": 600}
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.assertEqual(coordinator.capabilities(DEVICE, "hibernate")["delay_seconds"], 180)
        self.assertEqual(coordinator._profiles[profile["profile_id"]]["action_validation"]["hibernate"]["completion_evidence"], evidence)
        for key in ("output_cut_observed", "pending_signal_not_reported", "cancel_observed_without_cut", "manual_start_observed", "device_fingerprint"):
            invalid = copy.deepcopy(profile)
            invalid["pending_signal_evidence"].pop(key)
            self.assertFalse(self.make(profiles={invalid["profile_id"]: invalid}).capabilities(DEVICE)["available"])
        profile["pending_signal_unreliable"] = 1
        self.assertFalse(self.make(profiles={profile["profile_id"]: profile}).capabilities(DEVICE)["available"])

    def test_validated_load_limit_and_remaining_reserve_are_checked_before_any_write(self):
        profile = validated_profile()
        profile["action_validation"]["shutdown"].update(reserve_asof="battery_start", validated_max_load_percent=25)
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        capability = coordinator.capabilities(DEVICE)
        self.assertEqual((capability["reserve_asof"], capability["validated_max_load_percent"]), ("battery_start", 25))
        for evidence in (sample(), sample(load_percent=26), sample(load_percent=True), sample(load_percent=-1)):
            with self.subTest(evidence=evidence), self.assertRaises(UpsPowerBlocked):
                self.arm(coordinator, sample=evidence)
        with self.assertRaises(UpsPowerBlocked):
            self.arm(coordinator, sample=sample(load_percent=20), reserve_seconds=179)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.arm(coordinator, sample=sample(load_percent=25), reserve_seconds=180)["state"], "submitted")

    def test_unreliable_profile_can_cancel_only_own_full_submission_and_never_confirms_from_constant_zero_bit(self):
        profile = unreliable_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.arm(coordinator)
        self.clock.return_value = 110
        self.assertEqual(coordinator.cancel(device=DEVICE, sample=sample())["state"], "cancel_submitted")
        result = coordinator.observe(device=DEVICE, sample=sample())
        self.assertEqual((result["state"], result["confirmation"]), ("cancel_submitted", "unconfirmed"))
        for current in range(120, 311, 10):
            self.clock.return_value = current
            result = coordinator.observe(device=DEVICE, sample=sample())
        self.assertEqual((result["state"], result["confirmation"]), ("cancelled", "output_active_after_deadline"))
        self.transport.cancel_shutdown.assert_called_once_with()
        self.assertEqual(VALIDATED_PROFILES[profile["profile_id"]]["device_fingerprint"], OBSERVED_UNIT_FINGERPRINT)

    def sequence_evidence(self):
        return {"profile_id": self.profile["profile_id"], "device_fingerprint": device_fingerprint(DEVICE),
                "last_mains_monotonic": 88, "battery_started_monotonic": 90, "last_sample_monotonic": 100,
                "last_mains_sample": sample(on_battery=False, raw_status="Line mode", raw_flags={"status_hex": "09"},
                                            battery_voltage=27.41, load_percent=11),
                "on_battery_seconds": 0, "countdown_seconds": 10, "max_load_percent": 11, "min_battery_voltage": 25.41}

    def sequence_sample(self, **changes):
        return sample(**{"battery_voltage": 25.41, "load_percent": 11, "runtime_seconds": None, **changes})

    def test_real_profile_is_hibernation_sequence_only_with_traceability_not_runtime(self):
        profile = validated_control_profile(NT2402PRO_CANDIDATE_PROFILE["profile_id"])
        self.assertEqual(profile["device_fingerprint"], OBSERVED_UNIT_FINGERPRINT)
        self.assertEqual(set(profile["action_validation"]), {"hibernate"})
        self.assertEqual(_profile_evidence(profile, "hibernate"), (180, None))
        with self.assertRaises(UpsPowerBlocked):
            _profile_evidence(profile, "shutdown")
        validation = profile["action_validation"]["hibernate"]
        self.assertNotIn("reserve_seconds", validation)
        self.assertEqual(validation["completion_evidence"], {"trial_count": 2, "observed_range_seconds": [10, 15]})
        self.assertEqual(validation["sequence_validation"]["max_seconds_since_mains"], 15)
        self.assertEqual(validation["sequence_validation"]["measured_active_battery_seconds"], 12.656)
        self.assertEqual(profile["low_battery_evidence"], "protocol_worst_case_not_physically_tested")
        self.assertEqual(profile["cancel_source_operation_id"], "d211088e-9e60-4a93-91fa-987e0c960b68")

    def test_observed_sequence_arms_without_inventing_reserve_and_is_journaled_before_usb(self):
        profile = sequence_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        evidence = self.sequence_evidence()

        def submit(delay):
            operation = OperationJournal(self.path).load()
            self.assertEqual(operation["sequence_evidence"], evidence)
            self.assertIsNone(operation["reserve_seconds"])
            self.assertEqual(operation["reserve_mode"], "observed_sequence")
            self.assertEqual(operation["state"], "arming")
            self.assertIs(validate_production_start_preflight(profile, operation, self.sequence_sample(), now_monotonic=101), True)
            with self.assertRaises(UpsPowerBlocked):
                validate_production_start_preflight(profile, operation, self.sequence_sample(), now_monotonic=108.001)
            return {"state": "submitted"}

        self.transport.submit_shutdown.side_effect = submit
        result = coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(), sequence_evidence=evidence)
        self.assertEqual(result["state"], "submitted")
        self.assertTrue(result["submission_complete"])
        capability = coordinator.capabilities(DEVICE, "hibernate")
        self.assertEqual(capability["reserve_mode"], "observed_sequence")
        self.assertIsNone(capability["reserve_seconds"])
        self.assertIsNone(capability["reserve_baseline"])
        self.transport.submit_shutdown.assert_called_once_with(180)

    def test_sequence_rejects_long_countdown_wait_missing_history_load_voltage_fault_and_late_arm(self):
        profile = sequence_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        original = self.sequence_evidence()
        for changes in ({"countdown_seconds": 120}, {"countdown_seconds": 9}, {"on_battery_seconds": 60},
                        {"device_fingerprint": "0" * 64}, {"last_mains_monotonic": 79.999},
                        {"battery_started_monotonic": None}, {"max_load_percent": 12}, {"min_battery_voltage": 25.40}):
            with self.subTest(changes=changes), self.assertRaises(UpsPowerBlocked):
                coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(), sequence_evidence={**original, **changes})
        for changes in ({"load_percent": 12}, {"load_percent": None}, {"battery_voltage": 25.40},
                        {"battery_voltage": None}, {"low_battery": True}, {"fault": True}):
            with self.subTest(changes=changes), self.assertRaises(UpsPowerBlocked):
                coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(**changes), sequence_evidence=original)
        bad_mains = {**original, "last_mains_sample": {**original["last_mains_sample"], "battery_voltage": 27.40}}
        with self.assertRaises(UpsPowerBlocked):
            coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(), sequence_evidence=bad_mains)
        with self.assertRaises(UpsPowerBlocked):
            coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample())
        with self.assertRaises(UpsPowerBlocked):
            coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(), reserve_seconds=600, sequence_evidence=original)
        with self.assertRaises(UpsPowerBlocked):
            coordinator.arm("shutdown", device=DEVICE, sample=self.sequence_sample(), sequence_evidence=original)
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_full_submission_is_in_memory_before_windows_callback_and_survives_suspend(self):
        profile = sequence_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})

        def windows():
            self.assertTrue(coordinator._operation["submission_complete"])
            self.assertIn("submitted_at", coordinator._operation)
            self.assertIs(OperationJournal(self.path).load()["submission_complete"], False)
            coordinator.signal_interruption()

        result = coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(),
                                 sequence_evidence=self.sequence_evidence(), on_submission=windows)
        self.assertEqual(result["state"], "review_required")
        self.assertTrue(OperationJournal(self.path).load()["submission_complete"])
        with self.assertRaises(UpsPowerBlocked):
            coordinator.cancel(device=DEVICE, sample=self.sequence_sample())

    def test_configured_one_three_and_nine_minutes_are_distinct_from_physical_evidence(self):
        profile = sequence_profile()
        physical = copy.deepcopy(profile["action_validation"]["hibernate"])
        for selected in (60, 180, 540):
            coordinator = self.make(path=self.path.with_name(f"selected-{selected}.json"), profiles={profile["profile_id"]: profile})
            evidence = {**self.sequence_evidence(), "last_mains_monotonic": 83,
                "battery_started_monotonic": 85, "countdown_seconds": 15}
            with self.subTest(selected=selected):
                capability = coordinator.capabilities(DEVICE, "hibernate", delay_seconds=selected)
                self.assertTrue(capability["available"])
                self.assertEqual(capability["delay_seconds"], selected)
                self.assertEqual(capability["observed_delay_seconds"], 180)
                self.assertEqual(capability["delay_hardware_validated"], selected == 180)
                result = coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(),
                    sequence_evidence=evidence, delay_seconds=selected)
                self.assertEqual(result["delay_seconds"], selected)
                self.assertIsNone(result["reserve_seconds"])
                self.transport.submit_shutdown.assert_called_with(selected)
        self.assertEqual(profile["action_validation"]["hibernate"], physical)

    def test_ten_minutes_and_invalid_timers_never_create_an_operation_or_send(self):
        profile = sequence_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        for selected in (600, 59, 61, True, 0, 180.0):
            with self.subTest(selected=selected):
                capability = coordinator.capabilities(DEVICE, "hibernate", delay_seconds=selected)
                self.assertFalse(capability["available"])
                self.assertFalse(capability["delay_hardware_validated"])
                with self.assertRaises(UpsPowerBlocked):
                    coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(),
                        sequence_evidence=self.sequence_evidence(), delay_seconds=selected)
        self.assertIn("1 a 9", coordinator.capabilities(DEVICE, "hibernate", delay_seconds=600)["reason"])
        self.factory.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_120_second_countdown_requires_its_whole_elapsed_period_and_continuous_live_qs(self):
        profile = sequence_profile()
        self.clock.return_value = 200
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        evidence = {**self.sequence_evidence(), "last_mains_monotonic": 78,
            "battery_started_monotonic": 80, "last_sample_monotonic": 200, "countdown_seconds": 120}
        evidence["last_mains_sample"]["timestamp"] = (datetime.now(timezone.utc) - timedelta(seconds=122)).isoformat()
        self.assertTrue(validate_observed_sequence_preflight(profile, "hibernate", self.sequence_sample(), evidence, now_monotonic=200))
        for change in ({"last_sample_monotonic": 184.99}, {"battery_started_monotonic": 81},
                       {"max_load_percent": 12}, {"min_battery_voltage": 25.40}):
            with self.subTest(change=change), self.assertRaises(UpsPowerBlocked):
                validate_observed_sequence_preflight(profile, "hibernate", self.sequence_sample(),
                    {**evidence, **change}, now_monotonic=200)
        result = coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(),
            sequence_evidence=evidence, delay_seconds=60)
        self.assertEqual(result["delay_seconds"], 60)
        self.assertEqual(result["sequence_evidence"]["countdown_seconds"], 120)

    def test_configured_delay_owns_its_cancellation_deadline_and_survives_journal_reload(self):
        profile = sequence_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        operation = coordinator.arm("hibernate", device=DEVICE, sample=self.sequence_sample(),
            sequence_evidence=self.sequence_evidence(), delay_seconds=60)
        loaded = OperationJournal(self.path).load()
        self.assertEqual(loaded["delay_seconds"], 60)
        self.clock.return_value = 151
        with self.assertRaises(UpsPowerBlocked):
            coordinator.cancel(device=DEVICE, sample=self.sequence_sample())
        self.transport.cancel_shutdown.assert_not_called()

    def test_user_timer_keeps_numeric_headroom_above_the_profile_hibernation_measurement(self):
        profile = sequence_profile()
        self.assertEqual(configured_delay_seconds(profile, "hibernate", 60), 60)
        future = profile["action_validation"]["hibernate"]
        future["completion_evidence"]["observed_range_seconds"] = [60, 65]
        future["sequence_validation"]["approximate_cut_delay_seconds"] = 240
        with self.assertRaises(UpsPowerBlocked):
            configured_delay_seconds(profile, "hibernate", 60)
        self.assertEqual(configured_delay_seconds(profile, "hibernate", 120), 120)
        self.transport.cancel_shutdown.assert_not_called()

    def test_unreliable_profile_cannot_cancel_partial_submission_or_without_safe_output_and_time(self):
        profile = unreliable_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.transport.submit_shutdown.side_effect = TimeoutError("partial")
        self.arm(coordinator)
        for evidence in (sample(), pending_sample()):
            with self.assertRaises(UpsPowerBlocked):
                coordinator.cancel(device=DEVICE, sample=evidence)
        self.transport.cancel_shutdown.assert_not_called()

    def test_final_worker_preflight_requires_matching_current_process_full_submission_and_fresh_safe_qs(self):
        profile = unreliable_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        operation = self.arm(coordinator)
        operation.update(state="cancelling", cancel_attempted=True)
        self.assertIs(validate_production_cancel_preflight(profile, operation, sample(), now_monotonic=110), True)
        for changes in ({"owner_pid": -1}, {"owner_pid": True}, {"state": "submitted"}, {"interrupted": True},
                        {"submission_complete": False}, {"profile_id": "other"}, {"device_fingerprint": "0" * 64},
                        {"started_monotonic": float("nan")}, {"cancel_attempted": False}):
            with self.subTest(changes=changes), self.assertRaises(UpsPowerBlocked):
                validate_production_cancel_preflight(profile, {**operation, **changes}, sample(), now_monotonic=110)
        for evidence in (sample(output_voltage=0), sample(low_battery=True), sample(fault=True),
                         sample(timestamp=operation["submitted_at"]), sample(raw_flags={})):
            with self.subTest(evidence=evidence), self.assertRaises(UpsPowerBlocked):
                validate_production_cancel_preflight(profile, operation, evidence, now_monotonic=110)
        with self.assertRaises(UpsPowerBlocked):
            validate_production_cancel_preflight(profile, operation, sample(), now_monotonic=270)

    def test_start_worker_requires_immediate_battery_sample_remaining_reserve_and_original_arming_phase(self):
        operation = self.arm()
        operation.update(state="arming", submission_complete=False)
        self.assertIs(validate_production_start_preflight(self.profile, operation, sample(), now_monotonic=101), True)
        for changes in ({"state": "submitted"}, {"interrupted": True}, {"submission_complete": True},
                        {"owner_pid": -1}, {"reserve_seconds": 180}, {"started_monotonic": float("inf")}):
            with self.subTest(changes=changes), self.assertRaises(UpsPowerBlocked):
                validate_production_start_preflight(self.profile, {**operation, **changes}, sample(), now_monotonic=101)
        for evidence in (sample(on_battery=False, raw_status="Line mode"), sample(low_battery=True), sample(fault=True), sample(output_voltage=0)):
            with self.subTest(evidence=evidence), self.assertRaises(UpsPowerBlocked):
                validate_production_start_preflight(self.profile, operation, evidence, now_monotonic=101)

    def test_windows_callback_runs_once_before_outcome_flush_even_after_uncertain_usb(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                self.path.unlink(missing_ok=True)
                coordinator = self.make()
                sequence = []
                original_write = coordinator._journal.write

                def record(value):
                    sequence.append("journal")
                    return original_write(value)

                def submit(delay):
                    sequence.append("usb")
                    if uncertain:
                        raise OSError("partial USB write")
                    return {"state": "submitted"}

                self.transport.submit_shutdown.side_effect = submit
                callback = Mock(side_effect=lambda: sequence.append("windows"))
                with patch.object(coordinator._journal, "write", side_effect=record):
                    self.arm(coordinator, on_submission=callback)
                self.assertEqual(sequence, ["journal", "usb", "windows", "journal"])
                callback.assert_called_once_with()

    def test_signal_interruption_never_waits_for_coordinator_lock_or_writes_journal(self):
        self.arm()
        with self.coordinator._lock, patch.object(self.coordinator._journal, "write") as write:
            thread = threading.Thread(target=self.coordinator.signal_interruption)
            thread.start()
            thread.join(timeout=0.5)
            self.assertFalse(thread.is_alive(), "SCM callback must not wait for the decision/USB lock")
        write.assert_not_called()
        self.transport.signal_interruption.assert_called_once_with()
        self.assertTrue(self.coordinator._interrupted.is_set())
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())

    def test_native_transport_is_bound_to_one_journal_operation_and_retained_for_cancel(self):
        coordinator = UpsPowerCoordinator(self.path, validated_profiles=self.profiles, clock=self.clock)
        with patch("ups_monitor.hid_windows.ControlTransport", return_value=self.transport) as native:
            armed = self.arm(coordinator)
            coordinator.cancel(device=DEVICE, sample=pending_sample())
        native.assert_called_once_with(DEVICE["path"], self.profile["profile_id"], journal_path=self.path,
                                       operation_id=armed["operation_id"])

    def test_suspend_and_missing_continuous_observation_require_review_without_replaying_control(self):
        profile = unreliable_profile()
        coordinator = self.make(profiles={profile["profile_id"]: profile})
        self.arm(coordinator)
        self.clock.return_value = 110
        coordinator.cancel(device=DEVICE, sample=sample())
        self.clock.return_value = 310
        self.assertEqual(coordinator.observe(device=DEVICE, sample=sample())["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.assertTrue(OperationJournal(self.path).load()["interrupted"])
        self.transport.cancel_shutdown.assert_called_once()

    def test_explicit_interruption_invalidates_current_operation_before_any_later_cancel(self):
        self.arm()
        self.assertEqual(self.coordinator.interrupted()["state"], "review_required")
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.transport.cancel_shutdown.assert_not_called()

    def test_corrupt_or_foreign_journal_needs_review_and_cannot_arm_or_cancel(self):
        for value in ("broken", json.dumps({"schema_version": 1, "owner": "another-app"})):
            self.path.write_text(value, encoding="utf-8")
            coordinator = self.make()
            self.assertEqual(coordinator.status()["state"], "review_required")
            with self.assertRaises(UpsPowerBlocked):
                self.arm(coordinator)
            with self.assertRaises(UpsPowerBlocked):
                coordinator.cancel(device=DEVICE, sample=pending_sample())
        self.factory.assert_not_called()

    def review_sample(self, **changes):
        return sample(**{"on_battery": False, "raw_status": "Line mode", "raw_flags": {"status_hex": "09"}, **changes})

    def test_review_after_own_sleep_cycle_is_metadata_only_and_next_outage_arms_once(self):
        operation = self.arm()
        self.coordinator.interrupted()
        self.clock.return_value = 310
        self.assertIs(self.coordinator.can_review(device=DEVICE, sample=self.review_sample()), True)
        self.transport.reset_mock()
        self.factory.reset_mock()
        result = self.coordinator.complete_review(operation_id=operation["operation_id"], acknowledge_review=True,
                                                  device=DEVICE, sample=self.review_sample())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["confirmation"], "unconfirmed")
        self.assertIs(result["physical_confirmation"], False)
        self.assertEqual(result["review_source"], "user")
        self.assertFalse(self.coordinator._orphaned)
        self.factory.assert_not_called()
        self.transport.submit_shutdown.assert_not_called()
        self.transport.cancel_shutdown.assert_not_called()
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.complete_review(operation_id=operation["operation_id"], acknowledge_review=True,
                                              device=DEVICE, sample=self.review_sample())
        next_operation = self.arm()
        self.assertNotEqual(next_operation["operation_id"], operation["operation_id"])
        self.transport.submit_shutdown.assert_called_once_with(180)
        with self.assertRaises(UpsPowerBlocked):
            self.arm()
        self.transport.submit_shutdown.assert_called_once()

    def test_review_cannot_close_before_timer_or_without_exact_ack_and_owned_identifier(self):
        operation = self.arm()
        self.clock.return_value = 309.999
        self.assertFalse(self.coordinator.can_review(device=DEVICE, sample=self.review_sample()))
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.complete_review(operation_id=operation["operation_id"], acknowledge_review=True,
                                              device=DEVICE, sample=self.review_sample())
        self.clock.return_value = 310
        for acknowledgment in (False, 1, "true", None):
            with self.subTest(acknowledgment=acknowledgment), self.assertRaises(UpsPowerBlocked):
                self.coordinator.complete_review(operation_id=operation["operation_id"], acknowledge_review=acknowledgment,
                                                  device=DEVICE, sample=self.review_sample())
        for identity in ("not-a-uuid", "00000000-0000-0000-0000-000000000000", None):
            with self.subTest(identity=identity), self.assertRaises(UpsPowerBlocked):
                self.coordinator.complete_review(operation_id=identity, acknowledge_review=True,
                                                  device=DEVICE, sample=self.review_sample())
        self.transport.cancel_shutdown.assert_not_called()

    def test_review_rejects_bad_current_qs_foreign_unit_and_earlier_process(self):
        self.arm()
        self.clock.return_value = 310
        for evidence in (sample(), self.review_sample(output_voltage=0), self.review_sample(low_battery=True),
                         self.review_sample(fault=True), self.review_sample(raw_flags={}),
                         self.review_sample(raw_flags={"status_hex": "0B"}), self.review_sample(timestamp="2000-01-01T00:00:00Z")):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.coordinator.can_review(device=DEVICE, sample=evidence))
        self.assertFalse(self.coordinator.can_review(device={**DEVICE, "path": DEVICE["path"] + "other"}, sample=self.review_sample()))
        restarted = self.make()
        self.assertFalse(restarted.can_review(device=DEVICE, sample=self.review_sample()))
        self.coordinator._operation["owner_pid"] = -1
        self.assertFalse(self.coordinator.can_review(device=DEVICE, sample=self.review_sample()))
        self.transport.cancel_shutdown.assert_not_called()

    def test_partial_submission_cannot_be_closed_as_normal_cycle(self):
        self.transport.submit_shutdown.side_effect = TimeoutError("partial")
        operation = self.arm()
        self.clock.return_value = 310
        self.assertFalse(self.coordinator.can_review(device=DEVICE, sample=self.review_sample()))
        with self.assertRaises(UpsPowerBlocked):
            self.coordinator.complete_review(operation_id=operation["operation_id"], acknowledge_review=True,
                                              device=DEVICE, sample=self.review_sample())
        self.transport.submit_shutdown.assert_called_once()
        self.transport.cancel_shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
