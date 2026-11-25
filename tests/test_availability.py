"""桩状态对负荷分析与可预约容量不同含义的测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.availability import (
    port_available_time,
    station_capacity_timeline,
)
from charging_flex_trial.models import ChargerStatusEvent, DataQuality, Station
from charging_flex_trial.timeutil import parse_ts

STATION = Station("S-01", "north", 4, "Asia/Shanghai")
W0 = parse_ts("2026-10-08T18:00")
W1 = parse_ts("2026-10-08T21:00")


def ev(port, state, start, end=None):
    return ChargerStatusEvent("S-01", port, parse_ts(start), parse_ts(end), state)


class CapacitySemanticsTest(unittest.TestCase):
    def test_maintenance_removes_bookable_and_load_capacity(self):
        timeline = station_capacity_timeline(
            STATION, [ev("S-01-P3", "maintenance", "2026-10-08T18:30", "2026-10-08T20:00")],
            W0, W1, 30, DataQuality(),
        )
        at_1830 = next(c for c in timeline if c.bucket == parse_ts("2026-10-08T18:30"))
        self.assertEqual(at_1830.maintenance, 1)
        self.assertEqual(at_1830.bookable, 3)
        self.assertEqual(at_1830.load_capable, 3)

    def test_derated_still_bookable_but_discounted(self):
        timeline = station_capacity_timeline(
            STATION, [ev("S-01-P2", "derated", "2026-10-08T18:00")],
            W0, W1, 30, DataQuality(),
        )
        c = timeline[0]
        self.assertEqual(c.derated, 1)
        self.assertEqual(c.bookable, 4)  # 降功率仍可预约
        self.assertEqual(c.load_capable, 4)
        self.assertAlmostEqual(c.effective_ports(0.5), 3.5)  # 功率能力打折

    def test_comms_lost_not_bookable_and_unknown_for_load(self):
        timeline = station_capacity_timeline(
            STATION, [ev("S-01-P1", "comms_lost", "2026-10-08T19:00", "2026-10-08T19:30")],
            W0, W1, 30, DataQuality(),
        )
        at_1900 = next(c for c in timeline if c.bucket == parse_ts("2026-10-08T19:00"))
        self.assertEqual(at_1900.comms_lost, 1)
        self.assertEqual(at_1900.bookable, 3)  # 通讯中断保守不可预约
        self.assertEqual(at_1900.load_capable, 3)  # 负荷口径记为未知，不计入能力

    def test_open_ended_event_persists(self):
        timeline = station_capacity_timeline(
            STATION, [ev("S-01-P2", "derated", "2026-10-08T18:00")],
            W0, W1, 30, DataQuality(),
        )
        self.assertTrue(all(c.derated == 1 for c in timeline))

    def test_unknown_port_id_is_data_quality_issue(self):
        dq = DataQuality()
        station_capacity_timeline(
            STATION, [ev("S-01-P9", "maintenance", "2026-10-08T18:00")],
            W0, W1, 30, dq,
        )
        self.assertTrue(any(i["code"] == "unknown_port" for i in dq.issues))

    def test_bucket_with_state_boundary_is_marked_mixed(self):
        timeline = station_capacity_timeline(
            STATION, [ev("S-01-P3", "maintenance", "2026-10-08T18:45", "2026-10-08T20:00")],
            W0, W1, 30, DataQuality(),
        )
        at_1830 = next(c for c in timeline if c.bucket == parse_ts("2026-10-08T18:30"))
        self.assertTrue(at_1830.mixed_state)


class PortAvailableTimeTest(unittest.TestCase):
    def test_real_usable_time_excludes_maintenance_not_derated(self):
        events = [
            ev("S-01-P1", "maintenance", "2026-10-08T18:00", "2026-10-08T19:00"),
            ev("S-01-P2", "derated", "2026-10-08T18:00", "2026-10-08T19:00"),
            ev("S-01-P3", "comms_lost", "2026-10-08T18:00", "2026-10-08T19:00"),
        ]
        rows = {p.port_id: p for p in port_available_time(STATION, events, W0, W1, DataQuality())}
        self.assertEqual(rows["S-01-P1"].maintenance_minutes, 60)
        self.assertEqual(rows["S-01-P1"].usable_minutes, 120)  # 3 小时窗口减去 1 小时维护
        self.assertEqual(rows["S-01-P2"].usable_minutes, 180)  # 降功率仍算可用
        self.assertEqual(rows["S-01-P3"].unknown_minutes, 60)  # 通讯中断单列未知
        self.assertEqual(rows["S-01-P4"].available_minutes, 180)


if __name__ == "__main__":
    unittest.main()
