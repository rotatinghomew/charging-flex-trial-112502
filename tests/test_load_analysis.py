"""负荷分析：可比桶选择、价格效果分解、预测的测试。"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.availability import BucketCapacity
from charging_flex_trial.load_analysis import (
    ComparableBucket,
    bucket_loads,
    decompose_price_effect,
    forecast_buckets,
    select_comparable_buckets,
)
from charging_flex_trial.models import Condition, PriceVersion, SessionSegment
from charging_flex_trial.sessions import build_episodes
from charging_flex_trial.timeutil import parse_ts


def seg(session_id, vehicle, station, start, end, kwh=10.0):
    return SessionSegment(session_id, vehicle, station, parse_ts(start), parse_ts(end), kwh, "complete")


def cap(station, bucket, maintenance=0, comms=0, total=4):
    return BucketCapacity(station, parse_ts(bucket), total, total - maintenance - comms, 0, maintenance, comms, False)


class BucketLoadTest(unittest.TestCase):
    def test_session_spanning_buckets_counts_once_per_bucket(self):
        episodes = build_episodes([seg("a", "v-1", "S-01", "2026-10-08T18:10", "2026-10-08T19:20")], {})
        loads = bucket_loads(episodes, ["S-01"], parse_ts("2026-10-08T18:00"), parse_ts("2026-10-08T20:00"), 30)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T18:00"))].ports_busy, 1)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T18:30"))].ports_busy, 1)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T19:00"))].ports_busy, 1)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T19:30"))].ports_busy, 0)

    def test_session_ending_at_bucket_boundary_not_counted_next_bucket(self):
        episodes = build_episodes([seg("a", "v-1", "S-01", "2026-10-08T18:15", "2026-10-08T19:00")], {})
        loads = bucket_loads(episodes, ["S-01"], parse_ts("2026-10-08T18:00"), parse_ts("2026-10-08T20:00"), 30)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T18:30"))].ports_busy, 1)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T19:00"))].ports_busy, 0)

    def test_open_segment_only_start_bucket_and_caveat(self):
        episodes = build_episodes(
            [SessionSegment("a", "v-1", "S-01", parse_ts("2026-10-08T18:44"), None, None, "interrupted")],
            {},
        )
        loads = bucket_loads(episodes, ["S-01"], parse_ts("2026-10-08T18:00"), parse_ts("2026-10-08T20:00"), 30)
        self.assertEqual(loads[("S-01", parse_ts("2026-10-08T18:30"))].ports_busy, 1)
        later = loads[("S-01", parse_ts("2026-10-08T19:00"))]
        self.assertEqual(later.ports_busy, 0)
        self.assertTrue(later.open_segment_caveat)  # 不假设结束时刻，后续桶标记可能低估


class ComparableSelectionTest(unittest.TestCase):
    def _inputs(self):
        episodes = build_episodes([seg("a", "v-1", "S-01", "2026-10-08T18:10", "2026-10-08T18:50")], {})
        loads = bucket_loads(episodes, ["S-01"], parse_ts("2026-10-08T18:00"), parse_ts("2026-10-08T19:30"), 30)
        prices = [PriceVersion("p-early", parse_ts("2026-10-01T00:00"), None, 1.2)]
        return loads, prices

    def test_capacity_event_excludes_bucket(self):
        loads, prices = self._inputs()
        capacity = {
            ("S-01", parse_ts("2026-10-08T18:00")): cap("S-01", "2026-10-08T18:00"),
            ("S-01", parse_ts("2026-10-08T18:30")): cap("S-01", "2026-10-08T18:30", maintenance=1),
            ("S-01", parse_ts("2026-10-08T19:00")): cap("S-01", "2026-10-08T19:00"),
        }
        conditions = [Condition("S-01", parse_ts("2026-10-08T18:00"), 19.0, False)]
        comparable, excluded = select_comparable_buckets(loads, capacity, conditions, prices)
        reasons = {e["bucket"]: e["reason"] for e in excluded}
        self.assertEqual(reasons["2026-10-08T18:30"], "capacity_event")
        self.assertTrue(all(c.bucket != parse_ts("2026-10-08T18:30") for c in comparable))

    def test_missing_condition_excludes_bucket(self):
        loads, prices = self._inputs()
        capacity = {}
        comparable, excluded = select_comparable_buckets(loads, capacity, [], prices)
        self.assertEqual(len(comparable), 0)
        self.assertTrue(all(e["reason"] == "no_valid_condition" for e in excluded))

    def test_bucket_without_price_version_excluded(self):
        loads, _ = self._inputs()
        conditions = [Condition("S-01", parse_ts("2026-10-08T18:00"), 19.0, False)]
        prices = [PriceVersion("p-late", parse_ts("2026-11-01T00:00"), None, 1.0)]
        comparable, excluded = select_comparable_buckets(loads, {}, conditions, prices)
        self.assertEqual(len(comparable), 0)
        self.assertTrue(all(e["reason"] == "no_price_version" for e in excluded))


class DecompositionTest(unittest.TestCase):
    def test_raw_change_splits_into_composition_and_adjusted(self):
        def row(period, stratum, busy):
            return ComparableBucket("S-01", parse_ts("2026-10-08T18:00"), period, stratum, busy, busy, 0)

        a = ("S-01", "False", "mild", "peak")
        b = ("S-01", "True", "mild", "peak")
        rows = (
            [row("p0", a, 1), row("p0", a, 1), row("p0", b, 3), row("p0", b, 3)]
            + [row("p1", a, 2)] * 4
            + [row("p1", b, 4)] * 2
        )
        result = decompose_price_effect(rows, "S-01", ("p0", "p1"))
        eff = result["effect"]
        # raw_before=2.0, raw_after=16/6≈2.6667
        self.assertAlmostEqual(eff["raw_before"], 2.0)
        self.assertAlmostEqual(eff["raw_after"], 16 / 6, places=4)
        self.assertAlmostEqual(eff["composition_effect"], -2 / 6, places=4)
        self.assertAlmostEqual(eff["adjusted_change"], 1.0, places=4)
        self.assertAlmostEqual(
            eff["raw_change"], eff["composition_effect"] + eff["adjusted_change"], places=4
        )

    def test_daypart_mix_shift_is_composition_not_price_effect(self):
        """后期观测集中在平段造成的占位下降必须归入结构效应。"""
        def row(period, stratum, busy):
            return ComparableBucket("S-01", parse_ts("2026-10-08T18:00"), period, stratum, busy, busy, 0)

        peak = ("S-01", "False", "mild", "peak")
        off = ("S-01", "False", "mild", "offpeak")
        rows = (
            [row("p0", peak, 2), row("p0", peak, 2), row("p0", off, 0), row("p0", off, 0)]
            + [row("p1", peak, 2), row("p1", peak, 2)]
            + [row("p1", off, 0)] * 6
        )
        result = decompose_price_effect(rows, "S-01", ("p0", "p1"))
        eff = result["effect"]
        self.assertAlmostEqual(eff["raw_change"], -0.5, places=4)
        self.assertAlmostEqual(eff["composition_effect"], -0.5, places=4)
        self.assertAlmostEqual(eff["adjusted_change"], 0.0, places=4)

    def test_station_without_valid_observations_outputs_no_effect(self):
        result = decompose_price_effect([], "S-03", ("p0", "p1"))
        self.assertIsNone(result["effect"])
        self.assertEqual(result["reason"], "no_valid_observations")

    def test_no_shared_strata_outputs_no_effect(self):
        def row(period, stratum):
            return ComparableBucket("S-01", parse_ts("2026-10-08T18:00"), period, stratum, 1, 1, 0)

        rows = [
            row("p0", ("S-01", "False", "mild", "peak")),
            row("p1", ("S-01", "True", "cold", "offpeak")),
        ]
        result = decompose_price_effect(rows, "S-01", ("p0", "p1"))
        self.assertIsNone(result["effect"])
        self.assertEqual(result["reason"], "no_shared_strata")


class ForecastTest(unittest.TestCase):
    def test_percentile_ranges_from_history(self):
        stratum = ("S-01", "False", "mild", "peak")
        rows = [
            ComparableBucket("S-01", parse_ts(f"2026-10-0{d}T18:00"), "p0", stratum, busy, busy, 0)
            for d, busy in [(1, 1), (2, 2), (3, 3), (4, 4)]
        ]
        forecast = forecast_buckets(rows, "S-01", [parse_ts("2026-10-16T18:00")])
        pred = forecast[0]["prediction"]
        self.assertEqual(pred["n_history"], 4)
        self.assertEqual(pred["p50"], 2.0)
        self.assertEqual(pred["p90"], 4.0)
        self.assertFalse(forecast[0]["low_confidence"])

    def test_no_history_gives_no_prediction(self):
        forecast = forecast_buckets([], "S-01", [parse_ts("2026-10-16T18:00")])
        self.assertIsNone(forecast[0]["prediction"])
        self.assertEqual(forecast[0]["reason"], "no_history")


if __name__ == "__main__":
    unittest.main()
