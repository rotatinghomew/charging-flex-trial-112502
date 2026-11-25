"""解析层：缺失与非法值不静默填充，进入资料质量记录。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.models import (
    DataQuality,
    parse_conditions,
    parse_prices,
    parse_readings,
    parse_reservations,
    parse_sessions,
)


class ParseQualityTest(unittest.TestCase):
    def test_missing_end_stays_none(self):
        dq = DataQuality()
        segments = parse_sessions(
            [{"session_id": "a", "vehicle_key": None, "station_id": "S-01",
              "started_at": "2026-10-08T18:00", "ended_at": None, "kwh": None, "status": "interrupted"}],
            dq,
        )
        self.assertIsNone(segments[0].ended_at)
        self.assertIsNone(segments[0].kwh)
        self.assertEqual(dq.issues, [])

    def test_negative_kwh_flagged_and_cleared(self):
        dq = DataQuality()
        segments = parse_sessions(
            [{"session_id": "a", "vehicle_key": "v", "station_id": "S-01",
              "started_at": "2026-10-08T18:00", "ended_at": "2026-10-08T19:00", "kwh": -3.0, "status": "complete"}],
            dq,
        )
        self.assertIsNone(segments[0].kwh)
        self.assertEqual(dq.issues[0]["code"], "negative_kwh")

    def test_end_before_start_rejected(self):
        dq = DataQuality()
        segments = parse_sessions(
            [{"session_id": "a", "vehicle_key": "v", "station_id": "S-01",
              "started_at": "2026-10-08T19:00", "ended_at": "2026-10-08T18:00", "kwh": 1.0, "status": "complete"}],
            dq,
        )
        self.assertEqual(segments, [])
        self.assertEqual(dq.issues[0]["code"], "end_before_start")

    def test_unknown_statuses_flagged_not_rewritten(self):
        dq = DataQuality()
        parse_sessions(
            [{"session_id": "a", "vehicle_key": "v", "station_id": "S-01",
              "started_at": "2026-10-08T18:00", "ended_at": None, "kwh": None, "status": "weird"}],
            dq,
        )
        parse_reservations(
            [{"reservation_id": "r", "station_id": "S-01", "starts_at": "2026-10-08T18:00",
              "port_count": 1, "status": "mystery"}],
            dq,
        )
        codes = {i["code"] for i in dq.issues}
        self.assertIn("unknown_session_status", codes)
        self.assertIn("unknown_reservation_status", codes)

    def test_reading_decrease_flagged(self):
        dq = DataQuality()
        parse_readings([{"session_id": "a", "start_kwh": 50.0, "end_kwh": 40.0}], dq)
        self.assertEqual(dq.issues[0]["code"], "reading_decreased")

    def test_bad_price_window_rejected(self):
        dq = DataQuality()
        versions = parse_prices(
            [{"price_version": "p", "starts_at": "2026-10-15T00:00",
              "ends_at": "2026-10-01T00:00", "cny_per_kwh": 1.0}],
            dq,
        )
        self.assertEqual(versions, [])
        self.assertEqual(dq.issues[0]["code"], "bad_price_window")

    def test_condition_missing_fields_rejected(self):
        dq = DataQuality()
        conditions = parse_conditions([{"station_id": "S-01", "observed_at": None}], dq)
        self.assertEqual(conditions, [])
        self.assertEqual(dq.issues[0]["code"], "unusable_condition")


if __name__ == "__main__":
    unittest.main()
