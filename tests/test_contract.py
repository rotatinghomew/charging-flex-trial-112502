"""输入资料的基线检查。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from charging_flex_trial.input_contract import load_bundle


class BundleContractTest(unittest.TestCase):
    def test_synthetic_bundle_has_required_records(self):
        bundle = load_bundle(Path(__file__).parents[1] / "data")
        self.assertEqual(bundle["source_info.json"]["source_kind"], "synthetic")
        for name in ('stations.json', 'sessions.json', 'prices.json', 'reservations.json', 'conditions.json'):
            self.assertIsInstance(bundle[name], list)

    def test_optional_files_loaded_when_present(self):
        bundle = load_bundle(Path(__file__).parents[1] / "data")
        self.assertIsInstance(bundle["charger_status.json"], list)
        self.assertIsInstance(bundle["readings.json"], list)

    def test_optional_files_absent_are_not_invented(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(__file__).parents[1] / "data"
            for name in ('source_info.json', 'stations.json', 'sessions.json', 'prices.json', 'reservations.json', 'conditions.json'):
                (Path(tmp) / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
            bundle = load_bundle(tmp)
            self.assertNotIn("charger_status.json", bundle)
            self.assertNotIn("readings.json", bundle)


if __name__ == "__main__":
    unittest.main()
