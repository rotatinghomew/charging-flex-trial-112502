"""价格版本归属的业务规则测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.models import DataQuality, PriceVersion, SessionSegment
from charging_flex_trial.pricing import (
    assign_episode_prices,
    assign_segment_price,
    effective_version,
    validate_timeline,
)
from charging_flex_trial.sessions import build_episodes
from charging_flex_trial.timeutil import parse_ts

VERSIONS = [
    PriceVersion("p-early", parse_ts("2026-10-01T00:00"), parse_ts("2026-10-15T00:00"), 1.2),
    PriceVersion("p-flex", parse_ts("2026-10-15T00:00"), None, 0.8),
]


def seg(session_id, vehicle, start, end=None):
    return SessionSegment(session_id, vehicle, "S-01", parse_ts(start), parse_ts(end), None, "complete")


class PriceAssignmentTest(unittest.TestCase):
    def test_version_effective_at_start_applies(self):
        a = assign_segment_price(VERSIONS, "s1", parse_ts("2026-10-08T18:00"))
        self.assertEqual(a.price_version, "p-early")
        self.assertEqual(a.cny_per_kwh, 1.2)
        b = assign_segment_price(VERSIONS, "s2", parse_ts("2026-10-16T18:00"))
        self.assertEqual(b.price_version, "p-flex")

    def test_started_session_not_repriced_by_later_version(self):
        started = parse_ts("2026-10-08T18:00")
        before = assign_segment_price(VERSIONS, "s1", started)
        # 新状态到达：新增一个更晚生效的版本
        extended = VERSIONS + [
            PriceVersion("p-new", parse_ts("2026-11-01T00:00"), None, 0.5)
        ]
        after = assign_segment_price(extended, "s1", started)
        self.assertEqual(before.price_version, after.price_version)
        self.assertEqual(before.cny_per_kwh, after.cny_per_kwh)

    def test_no_covering_version_is_explicit(self):
        a = assign_segment_price(VERSIONS, "s0", parse_ts("2026-09-01T10:00"))
        self.assertIsNone(a.price_version)
        self.assertEqual(a.basis, "no_version")

    def test_mid_episode_price_change_flagged_not_recomputed(self):
        segments = [
            seg("a", "v-1", "2026-10-14T23:30", "2026-10-14T23:55"),
            seg("b", "v-1", "2026-10-15T00:10", "2026-10-15T00:40"),
        ]
        episodes = build_episodes(segments, {})
        self.assertEqual(len(episodes), 1)
        result = assign_episode_prices(VERSIONS, episodes)
        ep_result = result[episodes[0].episode_id]
        self.assertEqual(ep_result["price_version"], "p-early")  # 事件级归属取开始时刻
        self.assertTrue(ep_result["mid_episode_price_change"])
        per_segment = {s["session_id"]: s["price_version"] for s in ep_result["segment_assignments"]}
        self.assertEqual(per_segment, {"a": "p-early", "b": "p-flex"})

    def test_overlapping_versions_resolved_latest_first_and_warned(self):
        dq = DataQuality()
        overlapping = VERSIONS + [
            PriceVersion("p-patch", parse_ts("2026-10-10T00:00"), parse_ts("2026-10-12T00:00"), 9.9)
        ]
        validate_timeline(overlapping, dq)
        self.assertTrue(any(i["code"] == "price_window_overlap" for i in dq.issues))
        v = effective_version(overlapping, parse_ts("2026-10-11T12:00"))
        self.assertEqual(v.price_version, "p-patch")


if __name__ == "__main__":
    unittest.main()
