import unittest
from datetime import timedelta

from ups_monitor.core import Store, iso_utc, utc_now


class HistoryGapTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.start = utc_now() - timedelta(hours=2)

    def tearDown(self):
        self.store.close()

    def add_at(self, seconds):
        self.store.add_sample({"timestamp": iso_utc(self.start + timedelta(seconds=seconds)),
                               "input_voltage": 220})

    def test_regular_collection_is_continuous_after_decimation(self):
        for seconds in range(0, 1000, 10):
            self.add_at(seconds)
        history = self.store.history(hours=3, max_points=4)
        self.assertLessEqual(len(history), 4)
        self.assertGreaterEqual(len(history), 2)
        self.assertFalse(any(sample["gap_before"] for sample in history))
        self.assertEqual(history[0]["timestamp"], iso_utc(self.start))
        self.assertEqual(history[-1]["timestamp"], iso_utc(self.start + timedelta(seconds=990)))

    def test_missing_raw_samples_remain_visible_after_decimation(self):
        for seconds in list(range(0, 400, 10)) + list(range(500, 1000, 10)):
            self.add_at(seconds)
        history = self.store.history(hours=3, max_points=4)
        self.assertFalse(history[0]["gap_before"])
        self.assertEqual(sum(sample["gap_before"] for sample in history), 1)

    def test_full_history_marks_only_the_interrupted_interval(self):
        for seconds in (0, 10, 20, 80, 90):
            self.add_at(seconds)
        history = self.store.history(hours=3)
        self.assertEqual([sample["gap_before"] for sample in history],
                         [False, False, False, True, False])

    def test_empty_range_and_short_period_are_supported(self):
        self.add_at(0)
        self.assertEqual(self.store.history(hours=0.25), [])


if __name__ == "__main__":
    unittest.main()
