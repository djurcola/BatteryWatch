"""Tests for strict reviewed battery asset configuration."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from batterywatch_api.battery_assets import load_battery_assets


CONFIG = Path(__file__).resolve().parents[2] / "config" / "battery_assets.json"


class BatteryAssetTests(unittest.TestCase):
    def test_reviewed_config_loads_64_unique_current_batteries(self) -> None:
        assets = load_battery_assets(CONFIG)
        duids = {asset.duid for asset in assets}

        self.assertEqual(
            (len(assets), len(duids), "HPR1" in duids),
            (64, 64, True),
        )
        self.assertTrue(
            {
                "ERB01",
                "ERB02",
                "LDBESS1",
                "MREHA1",
                "MREHA2",
                "MREHA3",
                "SNB01",
                "SNB02",
                "STABESS1",
                "WTAHB1",
            }.issubset(duids)
        )

    def test_reviewed_config_excludes_only_unobservable_or_incomplete_registry_rows(self) -> None:
        payload = json.loads(CONFIG.read_text(encoding="utf-8"))

        self.assertEqual(
            {item["duid"] for item in payload["excluded"]},
            {"KEPBL1", "MOORABS1", "NESBESS1", "NESBESS2", "WILLBES1"},
        )

    def test_invalid_or_duplicate_assets_fail_closed(self) -> None:
        valid = json.loads(CONFIG.read_text(encoding="utf-8"))
        cases: list[dict[str, object]] = []

        duplicate = json.loads(json.dumps(valid))
        duplicate["assets"].append(dict(duplicate["assets"][0]))
        cases.append(duplicate)

        unknown_key = json.loads(json.dumps(valid))
        unknown_key["assets"][0]["unexpected"] = True
        cases.append(unknown_key)

        bad_timestamp = json.loads(json.dumps(valid))
        bad_timestamp["assets"][0]["source_timestamp"] = "2025-03-14"
        cases.append(bad_timestamp)

        bad_capacity = json.loads(json.dumps(valid))
        bad_capacity["assets"][0]["capacity_mw"] = 0
        cases.append(bad_capacity)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "assets.json"
            for index, payload in enumerate(cases):
                with self.subTest(index=index):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_battery_assets(path)


if __name__ == "__main__":
    unittest.main()
