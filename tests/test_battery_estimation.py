"""Observed PMV curve and display filtering without USB or vendor runtime."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from ups_monitor.battery_estimation import BATTERY_ESTIMATE_METHOD, Nt2402ProBatteryEstimator


def sample(voltage=27.4, load=6, on_battery=False, **changes):
    return {"battery_voltage": voltage, "load_percent": load, "on_battery": on_battery,
            "raw_status": "Battery mode" if on_battery else "Line mode", "fault": False, **changes}


class BatteryEstimationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=0)
        self.estimator = Nt2402ProBatteryEstimator(clock=self.clock)

    def update(self, at, **values):
        self.clock.return_value = at
        return self.estimator.update(sample(**values))["battery_percent_estimate"]

    def test_replays_observed_eight_step_oem_discharge_sequence(self):
        self.assertEqual(self.update(0), 100)
        # Anonymized real observations: pre-insert averaging explains the first
        # 100% on battery and the subsequent descending display values.
        observed = [(1, 25.6, 13, 100), (5, 25.3, 11, 98), (9, 25.3, 9, 96),
                    (13, 25.3, 8, 94), (17, 25.3, 8, 92), (21, 25.3, 11, 91),
                    (25, 25.3, 10, 89), (29, 25.2, 11, 87)]
        for at, voltage, load, expected in observed:
            with self.subTest(at=at):
                self.assertEqual(self.update(at, voltage=voltage, load=load, on_battery=True), expected)
        self.assertEqual(self.estimator.update(sample(25.2, 11, True))["battery_estimate_method"],
                         BATTERY_ESTIMATE_METHOD)

    def test_replays_complete_anonymized_manual_cut_and_recovery_fixture(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "battery-pmv-cycle.json").read_text(encoding="utf-8"))
        observations = fixture["observations"]
        self.assertEqual(fixture["reference_application"], "Forza V3.00-21021")
        self.assertEqual(fixture["profile"], BATTERY_ESTIMATE_METHOD)
        self.assertEqual(len(observations), 74)
        self.assertEqual({point["capture_label"] for point in observations}, {"network", "battery", "recovery"})
        self.assertEqual({point["sample"]["raw_status"] for point in observations}, {"Line mode", "Battery mode"})
        self.assertEqual(observations[0]["elapsed_ms"], 0)
        previous_elapsed = -1
        for index, point in enumerate(observations):
            with self.subTest(index=index, phase=point["capture_label"]):
                self.assertGreater(point["elapsed_ms"], previous_elapsed)
                previous_elapsed = point["elapsed_ms"]
                self.assertEqual(set(point["sample"]), {"battery_voltage", "load_percent", "raw_status", "on_battery", "fault"})
                self.assertIs(point["sample"]["fault"], False)
                self.clock.return_value = point["elapsed_ms"] / 1000
                result = self.estimator.update(point["sample"])
                self.assertEqual(result["battery_percent_estimate"], point["expected_vendor_percent"])
                self.assertEqual(result["battery_estimate_method"], BATTERY_ESTIMATE_METHOD)
                if point["capture_label"] == "recovery":
                    self.assertEqual(point["sample"]["raw_status"], "Line mode")
                    self.assertIs(point["sample"]["on_battery"], False)
        self.assertEqual(observations[-1]["capture_label"], "recovery")
        self.assertEqual(observations[-1]["expected_vendor_percent"], 100)
        self.assertEqual(result["battery_percent_estimate"], 100)

    def test_cold_start_uses_first_curve_value_including_zero_and_half_up_rounding(self):
        for voltage, load, on_battery, expected in ((15, 10, True, 0), (25.25, 10, True, 81),
                                                    (25.24, 10, True, 80), (25.6, 10, True, 86),
                                                    (27.4, 6, False, 100)):
            with self.subTest(voltage=voltage):
                self.estimator.reset()
                fields = self.estimator.update(sample(voltage, load, on_battery))
                self.assertEqual(fields["battery_percent_estimate"], expected)
                self.assertEqual(fields["battery_estimate_method"], BATTERY_ESTIMATE_METHOD)

    def test_load_threshold_and_float32_endpoints_follow_observed_profile(self):
        for voltage, load, expected in ((25.4, 19, 83), (25.4, 20, 99), (26.4, 19, 99),
                                        (26.5, 19, 100), (30, 200, 100), (15, 0, 0)):
            with self.subTest(voltage=voltage, load=load):
                self.estimator.reset()
                self.assertEqual(self.estimator.update(sample(voltage, load, True))["battery_percent_estimate"], expected)

    def test_raw_precision_and_float32_conversion_precede_one_decimal_rounding(self):
        self.assertEqual(self.estimator.update(sample(218 * 60 / 510, 8, True))["battery_percent_estimate"], 86)
        self.estimator.reset()
        # 25.55 becomes 25.549999... as Float before one-decimal formatting.
        self.assertEqual(self.estimator.update(sample(25.55, 8, True))["battery_percent_estimate"], 85)

    def test_online_voltage_bands_and_avr_use_same_curve(self):
        for voltage, expected in ((15, 0), (23.2, 0), (25.4, 58), (26.6, 90), (27, 100), (30, 100)):
            for mode in ("Line mode", "AVR mode"):
                with self.subTest(voltage=voltage, mode=mode):
                    self.estimator.reset()
                    self.assertEqual(self.estimator.update(sample(voltage, raw_status=mode))["battery_percent_estimate"], expected)

    def test_two_second_polling_updates_filter_every_four_seconds_but_mode_change_is_immediate(self):
        self.assertEqual(self.update(0), 100)
        self.assertEqual(self.update(1, voltage=25.3, on_battery=True), 100)
        self.assertEqual(self.update(3, voltage=25.3, on_battery=True), 100)
        self.assertEqual(self.update(5, voltage=25.3, on_battery=True), 98)
        self.assertEqual(self.update(7, voltage=25.3, on_battery=True), 98)
        self.assertEqual(self.update(9, voltage=25.3, on_battery=True), 96)
        # Returning to mains preserves history, exposing recovery gradually.
        self.assertEqual(self.update(10), 94)
        self.assertEqual(self.update(12), 94)

    def test_invalid_observation_clears_history_and_never_invents_zero_charge(self):
        invalid = (sample(voltage=None), sample(voltage=False), sample(voltage=float("nan")),
                   sample(voltage=float("inf")), sample(voltage=14.99), sample(voltage=30.01),
                   sample(load=None), sample(load=False), sample(load=-1), sample(load=201),
                   sample(load=float("nan")), sample(on_battery=None), sample(fault=True),
                   sample(raw_status="Fault mode"), sample(raw_status="Battery test mode"),
                   sample(raw_status="Shutdown mode"), sample(raw_status="unknown"),
                   sample(on_battery=True, raw_status="Line mode"))
        for invalid_sample in invalid:
            with self.subTest(sample=invalid_sample):
                self.estimator.reset()
                self.assertEqual(self.update(0), 100)
                result = self.estimator.update(invalid_sample)
                self.assertEqual(result, {"battery_percent_estimate": None, "battery_estimate_method": None})
                self.assertEqual(self.update(1, voltage=25.3, on_battery=True), 81)

    def test_unobserved_gap_and_clock_regression_reset_window(self):
        for after in (16, -1):
            with self.subTest(after=after):
                self.estimator.reset()
                self.assertEqual(self.update(0), 100)
                self.assertEqual(self.update(after, voltage=25.3, on_battery=True), 81)
        self.estimator.reset()
        self.update(0)
        self.assertEqual(self.update(15, voltage=25.3, on_battery=True), 100)

    def test_charge_band_rises_one_point_per_720_seconds_with_filter_delay(self):
        for at in range(0, 751, 3):
            result = self.update(at, voltage=26.7)
            self.assertEqual(result, 91 if at == 750 else 90)
        # Mains readings outside this band do not reset its elapsed timer.
        for at in range(753, 1440, 3):
            self.update(at, voltage=27.4)
        self.assertEqual(self.update(1440, voltage=26.7), 100)
        for at in range(1443, 1471, 3):
            result = self.update(at, voltage=26.7)
        self.assertEqual(result, 92)
        # A battery transition resets the charging clock/capacity to 90.
        self.update(1471, voltage=25.3, on_battery=True)
        self.update(1472, voltage=26.7)
        for at in range(1475, 1506, 3):
            result = self.update(at, voltage=26.7)
        self.assertEqual(result, 90)

    def test_instances_do_not_share_filter_or_charging_state(self):
        other = Nt2402ProBatteryEstimator(clock=self.clock)
        self.assertEqual(self.estimator.update(sample())["battery_percent_estimate"], 100)
        self.assertEqual(other.update(sample(25.3, 10, True))["battery_percent_estimate"], 81)
        original = sample(25.3, 10, True)
        self.estimator.update(original)
        self.assertNotIn("battery_percent_estimate", original)
        self.assertNotIn("battery_percent", original)


if __name__ == "__main__":
    unittest.main()
