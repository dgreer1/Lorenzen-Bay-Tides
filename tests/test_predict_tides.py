import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.predict_tides import find_windows, load_observations, select_time_basis


class ParseObservationsTests(unittest.TestCase):
    def test_blank_and_sentinel_values_are_excluded(self):
        content = (
            "Date and Time\tRaglan Wharf\n"
            "01/07/2008 @ 13:05:00\t-1.166\n"
            "01/07/2008 @ 13:06:00\t\n"
            "01/07/2008 @ 13:07:00\t2.01166e-007\n"
            "01/07/2008 @ 13:08:00\t-1.2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text(content, encoding="utf-8")
            times, levels, stats = load_observations(path)

        self.assertEqual(len(times), 2)
        self.assertEqual(levels, [-1.166, -1.2])
        self.assertEqual(stats, {"rows": 4, "missing": 1, "sentinel": 1})
        self.assertEqual(times[0].tzinfo, timezone.utc)


class SailWindowTests(unittest.TestCase):
    def test_interpolates_both_threshold_crossings(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        times = [start, start.replace(minute=1), start.replace(minute=2), start.replace(minute=3)]
        windows = find_windows(times, [0.0, 1.0, 2.0, 0.0], threshold=1.0)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], times[1].isoformat())
        self.assertEqual(windows[0]["end"], (start.replace(minute=2) + (times[3] - times[2]) / 2).isoformat())


class TimeBasisSelectionTests(unittest.TestCase):
    def test_prefers_metservice_extrema_time_match_over_ambiguous_tide_direction(self):
        scores = {
            "UTC": {"mean_absolute_error_minutes": 33.0},
            "NZST": {"mean_absolute_error_minutes": 5.0},
        }
        directions = {
            "UTC": {"falling_tide": True},
            "NZST": {"falling_tide": True},
        }

        basis, reason = select_time_basis(scores, directions)

        self.assertEqual(basis, "NZST")
        self.assertIn("MetService event times match NZST", reason)


if __name__ == "__main__":
    unittest.main()
