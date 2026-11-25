"""端到端：在合成资料包上运行管线，验证关键业务结论。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.input_contract import load_bundle
from charging_flex_trial.pipeline import build_report, run

DATA_DIR = Path(__file__).parents[1] / "data"


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report(load_bundle(DATA_DIR))

    def test_interrupted_vehicle_merged_into_single_open_episode(self):
        episodes = {e["episode_id"]: e for e in self.report["episodes"]}
        ep = episodes["ep-ch-001a"]
        self.assertEqual(ep["session_ids"], ["ch-001a", "ch-001b"])
        self.assertFalse(ep["is_closed"])
        self.assertFalse(ep["energy_complete"])
        self.assertAlmostEqual(ep["energy_known_kwh"], 21.4)  # 缺结束读数不算零

    def test_cross_station_episode_recorded(self):
        episodes = {e["episode_id"]: e for e in self.report["episodes"]}
        ep = episodes["ep-ch-003a"]
        self.assertEqual(ep["stations"], ["S-01", "S-02"])
        self.assertIn("station_switch", ep["uncertainties"])

    def test_station_without_valid_observations_has_no_price_effect(self):
        effects = {e["station_id"]: e for e in self.report["load_analysis"]["price_effects"]}
        self.assertIsNone(effects["S-03"]["effect"])
        self.assertEqual(effects["S-03"]["reason"], "no_valid_observations")

    def test_price_effect_decomposition_adds_up(self):
        for eff in self.report["load_analysis"]["price_effects"]:
            if eff.get("effect") is None:
                continue
            e = eff["effect"]
            self.assertAlmostEqual(
                e["raw_change"], e["composition_effect"] + e["adjusted_change"], places=4
            )

    def test_started_sessions_keep_original_price(self):
        assignments = self.report["price_assignments"]
        ep = assignments["ep-ch-001a"]  # 2026-10-08 开始，p-early 生效期
        self.assertEqual(ep["price_version"], "p-early")
        self.assertEqual(ep["cny_per_kwh"], 1.2)

    def test_maintenance_not_bookable_derated_bookable(self):
        buckets = {
            (b["station_id"], b["bucket"]): b for b in self.report["capacity"]["buckets"]
        }
        at_1830 = buckets[("S-01", "2026-10-16T18:30")]
        self.assertEqual(at_1830["maintenance"], 2)
        self.assertEqual(at_1830["comms_lost"], 1)
        self.assertEqual(at_1830["bookable"], 1)  # 仅降功率的 P2 可预约

    def test_capacity_alert_names_affected_reservations(self):
        alerts = self.report["reservations"]["alerts"]
        self.assertTrue(alerts)
        at_1830 = next(a for a in alerts if a["bucket"] == "2026-10-16T18:30")
        affected = {a["reservation_id"]: a["impact"] for a in at_1830["affected_reservations"]}
        self.assertEqual(affected["r-004"], "commitment_at_risk")
        self.assertEqual(affected["r-003"], "reschedule_candidate")

    def test_proposed_rescheduled_confirmed_keeps_basis(self):
        replan = {r["reservation_id"]: r for r in self.report["reservations"]["replan"]}
        self.assertEqual(replan["r-003"]["disposition"], "suggest_move")
        self.assertIsNotNone(replan["r-003"]["suggested_start"])
        commitments = {c["reservation_id"]: c for c in self.report["reservations"]["commitments"]}
        r4 = commitments["r-004"]
        self.assertFalse(r4["still_valid"])  # 新容量状态下不足
        self.assertEqual(r4["basis"]["price_version"], "p-flex")  # 当时依据保留
        self.assertEqual(r4["basis"]["cny_per_kwh"], 0.8)
        r0 = commitments["r-000"]
        self.assertEqual(r0["status"], "completed")
        self.assertTrue(r0["still_valid"])

    def test_forecast_compared_with_actual(self):
        fva = self.report["forecast_vs_actual"]
        self.assertGreaterEqual(fva["changed_buckets"], 1)
        statuses = {b["status"] for b in fva["buckets"]}
        self.assertIn("below_range", statuses)

    def test_conclusions_carry_scope_versions_and_fingerprint(self):
        conclusions = self.report["conclusions"]
        self.assertTrue(conclusions)
        for c in conclusions:
            self.assertIn("included", c["session_scope"])
            self.assertIn("excluded", c["session_scope"])
            self.assertEqual(c["inputs_fingerprint"], self.report["inputs_fingerprint"])
        kinds = {c["kind"] for c in conclusions}
        self.assertIn("price_effect", kinds)
        self.assertIn("capacity_risk", kinds)
        self.assertIn("commitment", kinds)
        self.assertIn("forecast_change", kinds)

    def test_excluded_buckets_have_reasons(self):
        excluded = self.report["load_analysis"]["excluded_buckets"]
        self.assertTrue(excluded)
        self.assertTrue(all(e["reason"] for e in excluded))
        reasons = {e["reason"] for e in excluded}
        self.assertIn("capacity_event", reasons)
        self.assertIn("no_valid_condition", reasons)

    def test_run_writes_report_files(self):
        out_dir = Path(tempfile.mkdtemp())
        try:
            run(DATA_DIR, out_dir)
            report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["inputs_fingerprint"], self.report["inputs_fingerprint"])
            markdown = (out_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("价格效果归因", markdown)
            self.assertIn("结论回看", markdown)
        finally:
            shutil.rmtree(out_dir)


if __name__ == "__main__":
    unittest.main()
