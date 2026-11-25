"""会话归并与电量解析的业务规则测试。"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.models import MeterReading, SessionSegment
from charging_flex_trial.sessions import build_episodes, resolve_energy
from charging_flex_trial.timeutil import parse_ts


def seg(session_id, vehicle, station, start, end, kwh=None, status="complete"):
    return SessionSegment(session_id, vehicle, station, parse_ts(start), parse_ts(end), kwh, status)


class EpisodeMergeTest(unittest.TestCase):
    def test_interrupted_segments_same_vehicle_merge(self):
        segments = [
            seg("a", "v-1", "S-01", "2026-10-08T18:02", "2026-10-08T18:41", 21.4),
            seg("b", "v-1", "S-01", "2026-10-08T18:44", None, None, "interrupted"),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual(ep.session_ids, ["a", "b"])
        self.assertFalse(ep.is_closed)
        self.assertFalse(ep.energy_complete)
        self.assertAlmostEqual(ep.energy_known_kwh, 21.4)  # 已知下界，不是总量
        self.assertIn("open_segment", ep.uncertainties)
        self.assertIn("energy_incomplete", ep.uncertainties)

    def test_station_switch_merges_and_is_recorded(self):
        segments = [
            seg("a", "v-9", "S-01", "2026-10-15T12:00", "2026-10-15T12:50", 15.0),
            seg("b", "v-9", "S-02", "2026-10-15T12:55", "2026-10-15T13:40", 17.5),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual(ep.stations, ["S-01", "S-02"])
        self.assertTrue(ep.switched_station)
        self.assertTrue(ep.energy_complete)
        self.assertAlmostEqual(ep.energy_known_kwh, 32.5)

    def test_gap_beyond_threshold_stays_separate(self):
        segments = [
            seg("a", "v-1", "S-01", "2026-10-08T10:00", "2026-10-08T10:30", 5.0),
            seg("b", "v-1", "S-01", "2026-10-08T12:00", "2026-10-08T12:30", 5.0),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 2)

    def test_unlinkable_vehicle_stays_singleton(self):
        segments = [
            seg("a", None, "S-01", "2026-10-08T18:00", "2026-10-08T18:30", 5.0),
            seg("b", None, "S-01", "2026-10-08T18:35", "2026-10-08T19:00", 6.0),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 2)
        for ep in episodes:
            self.assertIn("vehicle_not_linkable", ep.uncertainties)

    def test_overlapping_same_vehicle_not_merged(self):
        segments = [
            seg("a", "v-1", "S-01", "2026-10-08T18:00", "2026-10-08T19:00", 5.0),
            seg("b", "v-1", "S-02", "2026-10-08T18:30", "2026-10-08T19:30", 6.0),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 2)
        for ep in episodes:
            self.assertIn("concurrent_segments_same_vehicle", ep.uncertainties)

    def test_open_segment_continuity_flagged_uncertain(self):
        segments = [
            seg("a", "v-1", "S-01", "2026-10-08T18:00", None, None, "interrupted"),
            seg("b", "v-1", "S-01", "2026-10-08T18:10", "2026-10-08T18:40", 8.0),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 1)
        self.assertIn("continuity_uncertain_open_segment", episodes[0].uncertainties)


class EnergyResolutionTest(unittest.TestCase):
    def test_missing_end_reading_is_unknown_not_zero(self):
        s = seg("a", "v-1", "S-01", "2026-10-08T18:00", None, None, "interrupted")
        energy = resolve_energy(s, {"a": MeterReading("a", 61.4, None)})
        self.assertIsNone(energy.kwh)
        self.assertNotEqual(energy.kwh, 0)

    def test_meter_reading_preferred_over_session_kwh(self):
        s = seg("a", "v-1", "S-01", "2026-10-08T18:00", "2026-10-08T19:00", 99.9)
        energy = resolve_energy(s, {"a": MeterReading("a", 10.0, 31.4)})
        self.assertAlmostEqual(energy.kwh, 21.4)
        self.assertEqual(energy.source, "meter")

    def test_session_kwh_fallback_when_no_reading(self):
        s = seg("a", "v-1", "S-01", "2026-10-08T18:00", "2026-10-08T19:00", 12.5)
        energy = resolve_energy(s, {})
        self.assertAlmostEqual(energy.kwh, 12.5)
        self.assertEqual(energy.source, "session_kwh")

    def test_decreasing_reading_is_unusable(self):
        s = seg("a", "v-1", "S-01", "2026-10-08T18:00", "2026-10-08T19:00", None)
        energy = resolve_energy(s, {"a": MeterReading("a", 50.0, 40.0)})
        self.assertIsNone(energy.kwh)


if __name__ == "__main__":
    unittest.main()
