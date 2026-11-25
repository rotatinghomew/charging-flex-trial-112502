"""预约台账、容量冲突提示与重排的业务规则测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.availability import BucketCapacity
from charging_flex_trial.models import PriceVersion, Reservation
from charging_flex_trial.reservations import (
    build_ledger,
    capacity_alerts,
    commitments_report,
    replan,
)
from charging_flex_trial.timeutil import parse_ts

PRICES = [PriceVersion("p-early", parse_ts("2026-10-01T00:00"), None, 1.2)]


def res(rid, station, start, count, status):
    return Reservation(rid, station, parse_ts(start), count, status)


def cap(station, bucket, bookable, total=4):
    return BucketCapacity(station, parse_ts(bucket), total, bookable, 0, total - bookable, 0, False)


def capacity_for(station, buckets_bookable):
    return {(station, parse_ts(b)): cap(station, b, bookable) for b, bookable in buckets_bookable.items()}


class LedgerBasisTest(unittest.TestCase):
    def test_confirmed_keeps_price_and_capacity_basis(self):
        capacity = capacity_for("S-01", {"2026-10-08T19:00": 3})
        ledger = build_ledger([res("r-1", "S-01", "2026-10-08T19:00", 2, "confirmed")], PRICES, capacity, 30)
        entry = ledger[0]
        self.assertEqual(entry.basis.price_version, "p-early")
        self.assertEqual(entry.basis.cny_per_kwh, 1.2)
        self.assertEqual(entry.basis.bookable_ports_at_start, 3)
        self.assertTrue(entry.basis.reconstructed)

    def test_proposed_has_no_basis(self):
        ledger = build_ledger([res("r-2", "S-01", "2026-10-08T19:00", 2, "proposed")], PRICES, {}, 30)
        self.assertIsNone(ledger[0].basis)


class CapacityAlertTest(unittest.TestCase):
    def test_alert_lists_affected_reservations(self):
        capacity = capacity_for("S-01", {"2026-10-16T18:00": 1, "2026-10-16T18:30": 1})
        entries = build_ledger(
            [
                res("r-c", "S-01", "2026-10-16T18:30", 2, "confirmed"),
                res("r-p", "S-01", "2026-10-16T18:00", 3, "proposed"),
                res("r-x", "S-01", "2026-10-16T18:00", 1, "cancelled"),
            ],
            PRICES,
            capacity,
            30,
        )
        forecast = [
            {"station_id": "S-01", "bucket": "2026-10-16T18:00", "prediction": {"p50": 2.0, "p90": 2.0, "n_history": 3}},
        ]
        alerts = capacity_alerts(capacity, forecast, entries, 60, 30)
        self.assertTrue(alerts)
        at_1830 = next(a for a in alerts if a["bucket"] == "2026-10-16T18:30")
        affected = {a["reservation_id"]: a["impact"] for a in at_1830["affected_reservations"]}
        self.assertEqual(affected["r-c"], "commitment_at_risk")
        self.assertEqual(affected["r-p"], "reschedule_candidate")
        self.assertNotIn("r-x", affected)  # 已取消不受影响

    def test_no_alert_when_capacity_sufficient(self):
        capacity = capacity_for("S-01", {"2026-10-16T18:00": 4})
        entries = build_ledger([res("r-c", "S-01", "2026-10-16T18:00", 2, "confirmed")], PRICES, capacity, 30)
        alerts = capacity_alerts(capacity, [], entries, 60, 30)
        self.assertEqual(alerts, [])


class ReplanTest(unittest.TestCase):
    def test_proposed_moved_when_original_slot_full(self):
        capacity = capacity_for(
            "S-01",
            {"2026-10-08T19:00": 2, "2026-10-08T19:30": 2, "2026-10-08T20:00": 4, "2026-10-08T20:30": 4},
        )
        entries = build_ledger(
            [
                res("r-c", "S-01", "2026-10-08T19:00", 2, "confirmed"),
                res("r-p", "S-01", "2026-10-08T19:00", 3, "proposed"),
            ],
            PRICES,
            capacity,
            30,
        )
        replan(entries, capacity, 30, reservation_minutes=60)
        by_id = {e.reservation.reservation_id: e for e in entries}
        self.assertEqual(by_id["r-p"].disposition, "suggest_move")
        self.assertEqual(by_id["r-p"].suggested_start, parse_ts("2026-10-08T20:00"))
        # 已确认的不被重排，依据保留
        self.assertEqual(by_id["r-c"].disposition, "confirmed")
        self.assertEqual(by_id["r-c"].basis.price_version, "p-early")

    def test_proposed_confirmable_when_fits(self):
        capacity = capacity_for("S-01", {"2026-10-08T19:00": 4, "2026-10-08T19:30": 4})
        entries = build_ledger([res("r-p", "S-01", "2026-10-08T19:00", 2, "proposed")], PRICES, capacity, 30)
        replan(entries, capacity, 30, reservation_minutes=60)
        self.assertEqual(entries[0].disposition, "confirmable")

    def test_proposed_unfulfillable_when_no_capacity_in_window(self):
        capacity = capacity_for("S-01", {"2026-10-08T19:00": 1})
        entries = build_ledger([res("r-p", "S-01", "2026-10-08T19:00", 4, "proposed")], PRICES, capacity, 30)
        replan(entries, capacity, 30, reservation_minutes=60)
        self.assertEqual(entries[0].disposition, "unfulfillable")

    def test_confirmed_marked_at_risk_but_basis_kept(self):
        capacity = capacity_for("S-01", {"2026-10-08T19:00": 1, "2026-10-08T19:30": 1})
        entries = build_ledger([res("r-c", "S-01", "2026-10-08T19:00", 2, "confirmed")], PRICES, capacity, 30)
        replan(entries, capacity, 30, reservation_minutes=60)
        entry = entries[0]
        self.assertEqual(entry.disposition, "at_risk")
        self.assertEqual(entry.basis.price_version, "p-early")  # 依据不被改写

    def test_completed_never_rescheduled(self):
        capacity = capacity_for("S-01", {"2026-10-08T18:00": 0, "2026-10-08T18:30": 0})
        entries = build_ledger([res("r-d", "S-01", "2026-10-08T18:00", 1, "completed")], PRICES, capacity, 30)
        replan(entries, capacity, 30, reservation_minutes=60)
        self.assertIn(entries[0].disposition, ("completed", "at_risk"))
        self.assertIsNone(entries[0].suggested_start)


class CommitmentsTest(unittest.TestCase):
    def test_commitments_report_validity(self):
        capacity = capacity_for(
            "S-01",
            {"2026-10-08T19:00": 1, "2026-10-08T19:30": 1, "2026-10-08T20:00": 4, "2026-10-08T20:30": 4},
        )
        entries = build_ledger(
            [
                res("r-a", "S-01", "2026-10-08T19:00", 2, "confirmed"),
                res("r-b", "S-01", "2026-10-08T20:00", 1, "completed"),
            ],
            PRICES,
            capacity,
            30,
        )
        replan(entries, capacity, 30, reservation_minutes=60)
        report = {c["reservation_id"]: c for c in commitments_report(entries)}
        self.assertFalse(report["r-a"]["still_valid"])
        self.assertTrue(report["r-b"]["still_valid"])
        self.assertEqual(report["r-a"]["basis"]["price_version"], "p-early")


if __name__ == "__main__":
    unittest.main()
